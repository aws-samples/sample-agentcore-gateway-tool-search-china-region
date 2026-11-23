"""Local service round trips, not a claim about live Gateway result wrapping.

The HTTP stub sends machine requests through the real REQUEST interceptor and
serves a paged catalog. User requests pass the transformed arguments directly to
the real Lambda handler, preserving the documented Lambda input contract.
"""

from __future__ import annotations

import base64
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric import rsa
from test_service_storage import DynamoStub, S3Stub

from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord
from semantic_core.providers import RequestContext
from semantic_service.auth import (
    AuthenticationError,
    AuthorizationError,
    DDBAuthorizer,
    EnvelopeCodec,
    FixedWindowLimiter,
    InputError,
    OIDCConfig,
    OIDCVerifier,
    ServiceError,
)
from semantic_service.catalog import (
    MCPClient,
    OAuthTokenSource,
    RegisteredTool,
    TargetRegistry,
    TargetStateSource,
)
from semantic_service.indexer import CatalogIndexer
from semantic_service.interceptor import InterceptorConfig, RequestInterceptor
from semantic_service.search import SearchHandler
from semantic_service.secrets import SecretsCache, SigningSecretsKeys
from semantic_service.storage import PublishConflict, SnapshotReader, SnapshotStore

GATEWAY = GatewayIdentity("000000000000", "cn-north-1", "roundtrip-fixture")
ISSUER = "https://idp.example.invalid"
ENDPOINT = "https://gateway.example.invalid/mcp"
SEARCH = ToolRecord.from_descriptor(
    ToolIdentity(GATEWAY, "search-target", "search-incarnation", "discovery___search_tools"),
    {
        "name": "discovery___search_tools",
        "description": "Find permitted business tools",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 2048},
                "_auth_context": {"type": "string", "maxLength": 8192},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
)
BUSINESS = ToolRecord.from_descriptor(
    ToolIdentity(GATEWAY, "orders-target", "orders-incarnation", "orders___cancel_order"),
    {
        "name": "orders___cancel_order",
        "description": "Cancel an order",
        "inputSchema": {"type": "object", "properties": {"order_id": {"type": "string"}}},
        "annotations": {"destructiveHint": True},
    },
)
HIDDEN = ToolRecord.from_descriptor(
    ToolIdentity(GATEWAY, "orders-target", "orders-incarnation", "orders___cancel_all_orders"),
    {
        "name": "orders___cancel_all_orders",
        "description": "Cancel all orders",
        "inputSchema": {"type": "object"},
    },
)


class CounterDynamo(DynamoStub):
    def __init__(self):
        super().__init__()
        self.counters = {}

    def update_item(self, **kwargs):
        self.calls.append(("update", kwargs))
        assert kwargs["ConditionExpression"] == "attribute_not_exists(#count) OR #count < :limit"
        key = kwargs["Key"]["pk"]["S"]
        limit = int(kwargs["ExpressionAttributeValues"][":limit"]["N"])
        count = self.counters.get(key, 0)
        if count >= limit:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        self.counters[key] = count + 1
        return {}


class SecretsStub:
    """Only explicitly named synthetic secrets; no environment or AWS discovery."""

    def __init__(self, now):
        self.now = now
        self.calls = []

    def get_secret_value(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["SecretId"] == "fixture/oauth":
            value = {"client_id": "machine-client", "client_secret": "synthetic-oauth-secret"}
            version = "oauth-version"
        elif kwargs["SecretId"] == "fixture/signing":
            value = {
                "key_b64": base64.b64encode(b"synthetic-signing-material-32-bytes").decode(),
                "activated_at": self.now - 10,
            }
            version = "signing-version"
        else:
            pytest.fail("unexpected secret scope")
        assert kwargs.get("VersionStage", "AWSCURRENT") == "AWSCURRENT"
        return {"VersionId": version, "SecretString": json.dumps(value)}


class TargetsStub:
    def list_gateway_targets(self, **kwargs):
        assert kwargs["gatewayIdentifier"] == GATEWAY.gateway_id
        return {"items": [{"targetId": "search-target"}, {"targetId": "orders-target"}]}

    def get_gateway_target(self, **kwargs):
        return {
            "targetId": kwargs["targetId"],
            "name": "discovery" if kwargs["targetId"] == "search-target" else "orders",
            "status": "READY",
            "targetConfiguration": {"mcp": {"lambda": {}}},
        }


def principal_entry(subject, allowed=()):
    return {
        "issuer": ISSUER,
        "subject": subject,
        "scopes": [],
        "groups": [],
        "allowed_tools": list(allowed),
        "allow_query_egress": False,
        "not_before": 0,
        "disabled": False,
    }


class LocalService:
    def __init__(self):
        self.now = int(time.time())
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key()))
        self.jwk.update(kid="oidc-fixture-key", alg="RS256", use="sig")
        self.business = BUSINESS
        self.dynamo = CounterDynamo()
        self.s3 = S3Stub()
        self.store = SnapshotStore(
            self.s3, self.dynamo, "fixture-bucket", "fixture-table", "fixture-protected", GATEWAY
        )
        self.store.initialize(
            {
                "version": 1,
                "epoch": 1,
                "principals": [principal_entry("indexer")],
                "tools": {},
            }
        )
        self.secret_client = SecretsStub(self.now)
        cache = SecretsCache(self.secret_client)
        self.envelope = EnvelopeCodec(SigningSecretsKeys(cache, "fixture/signing"))
        self.authorizer = DDBAuthorizer(self.store)
        self.gateway_calls = []
        self.after_first_page = None
        self.http = httpx.Client(transport=httpx.MockTransport(self.http_request))
        verifier = OIDCVerifier(
            OIDCConfig(
                ISSUER,
                ISSUER + "/jwks",
                "gateway-audience",
                frozenset({"idp.example.invalid"}),
                discovery_uri=ISSUER + "/.well-known/openid-configuration",
            ),
            self.http,
        )
        self.interceptor = RequestInterceptor(
            InterceptorConfig(SEARCH, frozenset({(ISSUER, "indexer")})),
            verifier,
            self.envelope,
            self.authorizer,
            FixedWindowLimiter(self.dynamo, "fixture-table"),
        )
        self.search = SearchHandler(
            SEARCH, self.envelope, self.authorizer, SnapshotReader(self.store, clock=lambda: 0)
        )
        tokens = OAuthTokenSource(
            cache,
            "fixture/oauth",
            ISSUER + "/token",
            ("idp.example.invalid",),
            ("catalog.read",),
            self.http,
        )
        mcp = MCPClient(self.http, ENDPOINT, ("gateway.example.invalid",), tokens)
        registry = TargetRegistry(
            GATEWAY,
            tuple(
                RegisteredTool(
                    record.name,
                    record.identity.target_id,
                    record.identity.incarnation,
                    "discovery" if record == SEARCH else "business",
                )
                for record in (SEARCH, BUSINESS, HIDDEN)
            ),
        )
        self.indexer = CatalogIndexer(
            self.store, registry, mcp, TargetStateSource(TargetsStub(), GATEWAY)
        )

    def token(self, subject):
        return jwt.encode(
            {
                "iss": ISSUER,
                "sub": subject,
                "aud": "gateway-audience",
                "iat": self.now,
                "exp": self.now + 300,
                "token_use": "access",
                "scope": "catalog.read" if subject == "indexer" else "",
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": "oidc-fixture-key"},
        )

    def input(self, body, headers):
        return {
            "interceptorInputVersion": "1.0",
            "mcp": {
                "gatewayRequest": {
                    "path": "/mcp",
                    "httpMethod": "POST",
                    "headers": headers,
                    "body": body,
                }
            },
        }

    def http_request(self, request):
        if str(request.url) == ISSUER + "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": ISSUER + "/jwks"})
        if str(request.url) == ISSUER + "/jwks":
            return httpx.Response(200, json={"keys": [self.jwk]})
        if str(request.url) == ISSUER + "/token":
            expected = base64.b64encode(b"machine-client:synthetic-oauth-secret").decode()
            assert request.headers["Authorization"] == "Basic " + expected
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(
                200,
                json={
                    "access_token": self.token("indexer"),
                    "token_type": "Bearer",
                    "expires_in": 300,
                },
            )
        assert str(request.url) == ENDPOINT
        body = json.loads(request.content)
        transformed = self.interceptor(self.input(body, dict(request.headers)), None)
        forwarded = transformed["mcp"]["transformedGatewayRequest"]["body"]
        assert forwarded == body
        self.gateway_calls.append(copy.deepcopy(body))
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}
        else:
            assert body["method"] == "tools/list"
            if body["params"].get("cursor") is None:
                result = {
                    "tools": [SEARCH.descriptor, HIDDEN.descriptor],
                    "nextCursor": "opaque-second",
                }
                if self.after_first_page is not None:
                    callback, self.after_first_page = self.after_first_page, None
                    callback()
            else:
                assert body["params"]["cursor"] == "opaque-second"
                result = {"tools": [self.business.descriptor]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def intercept(self, method, params=None, subject="user", request_id="call-fixture"):
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        return self.interceptor(
            self.input(body, {"Authorization": "Bearer " + self.token(subject)}), None
        )

    def policy(self, *, allow_business=True):
        control = self.store.read_control()
        records = (SEARCH, self.business, HIDDEN)
        return {
            "version": 1,
            "epoch": control.policy_epoch + 1,
            "principals": [
                principal_entry("indexer"),
                principal_entry(
                    "user",
                    (SEARCH.tool_id, self.business.tool_id)
                    if allow_business
                    else (SEARCH.tool_id,),
                ),
            ],
            "tools": {
                record.name: {"id": record.tool_id, "schema_digest": record.schema_digest}
                for record in records
            },
        }

    def update_policy(self, policy):
        return self.store.update_policy(self.store.read_control(), policy)

    def run_indexer(self):
        return self.indexer.run(RequestContext("indexer-fixture", time.monotonic() + 10))

    def activate(self):
        assert self.run_indexer() == "published"
        self.update_policy(self.policy())

    def arguments(self):
        rewritten = self.intercept(
            "tools/call",
            {
                "name": SEARCH.name,
                "arguments": {"query": "cancel order", "_auth_context": "forged"},
            },
        )
        return rewritten["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]

    def search_context(self):
        return SimpleNamespace(
            client_context=SimpleNamespace(
                custom={
                    "bedrockAgentCoreMessageVersion": "1.0",
                    "bedrockAgentCoreAwsRequestId": "aws-request-fixture",
                    "bedrockAgentCoreMcpMessageId": "call-fixture",
                    "bedrockAgentCoreGatewayId": GATEWAY.gateway_id,
                    "bedrockAgentCoreTargetId": SEARCH.identity.target_id,
                    "bedrockAgentCoreToolName": SEARCH.name,
                }
            )
        )

    def discover(self, arguments=None):
        return self.search(
            self.arguments() if arguments is None else arguments, self.search_context()
        )


@pytest.fixture(params=("constrained", "gateway"))
def local_service(request, monkeypatch):
    if request.param == "gateway":
        capture = json.loads(
            (Path(__file__).parent / "fixtures/service/search-schema.synthetic.json").read_text()
        )
        descriptor = capture["tool"] | {"name": SEARCH.name}
        monkeypatch.setitem(
            globals(), "SEARCH", ToolRecord.from_descriptor(SEARCH.identity, descriptor)
        )
    service = LocalService()
    try:
        yield service
    finally:
        service.http.close()


def test_empty_control_machine_bootstrap_publish_and_authorized_search(local_service):
    service = local_service
    control = service.store.read_control()
    assert control.safety is None and control.catalog_revision == 0
    assert service.store.load_policy(control)["tools"] == {}
    with pytest.raises(AuthorizationError):
        service.intercept("tools/list")
    with pytest.raises(AuthorizationError):
        service.intercept(
            "tools/call", {"name": SEARCH.name, "arguments": {"query": "cancel"}}, subject="indexer"
        )
    assert service.run_indexer() == "published"
    active = service.store.read_control()
    assert active.catalog_revision == 1
    snapshot = service.store.load_snapshot(active)
    assert {tool.name for tool in snapshot.tools} == {BUSINESS.name, HIDDEN.name}
    assert SEARCH.tool_id in service.store.load_safety(active)
    listings = [call for call in service.gateway_calls if call["method"] == "tools/list"]
    assert [call["params"] for call in listings] == [{}, {"cursor": "opaque-second"}] * 2
    service.update_policy(service.policy())
    bootstrap = service.intercept("tools/list")
    assert bootstrap["mcp"]["transformedGatewayResponse"]["body"]["result"] == {
        "tools": [SEARCH.descriptor]
    }
    arguments = service.arguments()
    assert arguments["_auth_context"] != "forged"
    assert service.discover(arguments) == {"tools": [BUSINESS.descriptor]}
    assert all(
        kwargs["ConsistentRead"] for method, kwargs in service.dynamo.calls if method == "get"
    )
    assert {call["SecretId"] for call in service.secret_client.calls} == {
        "fixture/oauth",
        "fixture/signing",
    }


def test_schema_metadata_does_not_relax_runtime_arguments(local_service, monkeypatch):
    service = local_service
    service.activate()
    boundary = service.intercept(
        "tools/call", {"name": SEARCH.name, "arguments": {"query": "字" * 2048}}
    )["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    assert "tools" in service.discover(boundary)

    def no_sign(*args, **kwargs):
        pytest.fail("invalid arguments must be rejected before signing")

    monkeypatch.setattr(service.envelope, "issue", no_sign)
    for arguments in (
        {"query": ""},
        {"query": " "},
        {"query": "字" * 2049},
        {"query": "cancel", "unexpected": True},
    ):
        with pytest.raises(InputError):
            service.intercept("tools/call", {"name": SEARCH.name, "arguments": arguments})
        with pytest.raises(InputError):
            service.discover(arguments)


def test_schema_metadata_does_not_relax_envelope_or_context(local_service):
    service = local_service
    service.activate()
    signed = service.arguments()
    for invalid in (
        {"query": signed["query"]},
        signed | {"_auth_context": "forged"},
        signed | {"_auth_context": "x" * 8193},
    ):
        with pytest.raises(AuthenticationError):
            service.discover(invalid)
    context = service.search_context()
    context.client_context.custom["bedrockAgentCoreTargetId"] = "different-target"
    with pytest.raises(AuthenticationError):
        service.search(signed, context)


def test_policy_revocation_applies_to_already_signed_request(local_service):
    service = local_service
    service.activate()
    previous = service.arguments()
    assert service.discover(previous) == {"tools": [BUSINESS.descriptor]}
    service.update_policy(service.policy(allow_business=False))
    assert service.discover(previous) == {"tools": []}
    with pytest.raises(AuthorizationError):
        service.intercept("tools/call", {"name": BUSINESS.name, "arguments": {}})
    revoked = service.policy(allow_business=False)
    revoked["principals"][1]["disabled"] = True
    service.update_policy(revoked)
    with pytest.raises(AuthorizationError):
        service.discover(previous)


def test_observed_schema_change_revokes_until_policy_reapproval(local_service):
    service = local_service
    service.activate()
    signed = service.arguments()
    assert service.discover(signed) == {"tools": [BUSINESS.descriptor]}
    changed = BUSINESS.descriptor
    changed["inputSchema"]["required"] = ["order_id"]
    service.business = ToolRecord.from_descriptor(BUSINESS.identity, changed)
    assert service.run_indexer() == "published"
    assert service.store.read_control().catalog_revision == 2
    assert service.discover(signed) == {"tools": []}
    with pytest.raises(AuthorizationError):
        service.intercept(
            "tools/call", {"name": BUSINESS.name, "arguments": {"order_id": "fixture"}}
        )
    service.update_policy(service.policy())
    assert service.discover() == {"tools": [changed]}


def test_policy_race_rejects_old_catalog_commit_and_next_tick_recovers(local_service):
    service = local_service

    def policy_change():
        service.update_policy(service.policy(allow_business=False))

    service.after_first_page = policy_change
    with pytest.raises(PublishConflict):
        service.run_indexer()
    control = service.store.read_control()
    assert control.policy_epoch == 2 and control.catalog_revision == 0 and control.safety is None
    assert service.run_indexer() == "published"
    assert service.discover() == {"tools": []}
    assert service.store.read_control().policy_epoch == 2


def test_corrupt_policy_and_tombstone_cannot_use_warm_content(local_service):
    service = local_service
    service.activate()
    signed = service.arguments()
    assert service.discover(signed) == {"tools": [BUSINESS.descriptor]}
    control = service.store.read_control()
    reference = control.policy
    version, original = service.s3.objects[reference.key]
    service.s3.objects[reference.key] = (version, original + b" ")
    with pytest.raises(ServiceError) as failure:
        service.discover(signed)
    assert type(failure.value) is ServiceError
    service.s3.objects[reference.key] = (version, original)
    service.store.tombstone(control)
    with pytest.raises(AuthorizationError):
        service.discover(signed)

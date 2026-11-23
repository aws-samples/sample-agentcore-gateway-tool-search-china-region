"""Synthetic identity fixtures; these do not assert a live Gateway contract."""

from __future__ import annotations

import base64
import hmac
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord
from semantic_core.snapshot import IndexSnapshot
from semantic_service.auth import (
    AuthenticationError,
    AuthorizationError,
    AuthorizationView,
    DDBAuthorizer,
    EnvelopeBinding,
    EnvelopeCodec,
    FixedWindowLimiter,
    InputError,
    OIDCConfig,
    OIDCVerifier,
    Principal,
    RateLimitError,
    ServiceError,
)
from semantic_service.interceptor import InterceptorConfig, RequestInterceptor
from semantic_service.search import SearchHandler

NOW = int(time.time())
GATEWAY = GatewayIdentity("000000000000", "cn-north-1", "gateway-fixture")
SEARCH = ToolRecord.from_descriptor(
    ToolIdentity(GATEWAY, "search-target", "1", "discovery___search_tools"),
    {"name": "discovery___search_tools", "description": "Discover tools", "inputSchema": {}},
)
BUSINESS = ToolRecord.from_descriptor(
    ToolIdentity(GATEWAY, "business-target", "1", "orders___cancel_order"),
    {
        "name": "orders___cancel_order",
        "description": "Cancel an order",
        "inputSchema": {"type": "object"},
    },
)
PRINCIPAL = Principal(
    "https://idp.example.invalid", "user-fixture", frozenset(), frozenset(), NOW, NOW + 300
)
BINDING = EnvelopeBinding(
    GATEWAY.gateway_id, SEARCH.identity.target_id, SEARCH.name, "mcp-1", "cancel"
)


class Keys:
    def current(self):
        return "test-key-1", b"a" * 32

    def resolve(self, kid):
        if kid != "test-key-1":
            raise ValueError("unknown key")
        return b"a" * 32


def codec():
    return EnvelopeCodec(Keys(), clock=lambda: NOW)


def view(allowed=None, *, epoch=1):
    return AuthorizationView(
        control=SimpleNamespace(policy_epoch=epoch, safety_revision=1, tombstoned=False),
        allowed_ids=frozenset([SEARCH.tool_id, BUSINESS.tool_id] if allowed is None else allowed),
        tools_by_name={SEARCH.name: SEARCH.identity, BUSINESS.name: BUSINESS.identity},
        schema_digests={
            SEARCH.tool_id: SEARCH.schema_digest,
            BUSINESS.tool_id: BUSINESS.schema_digest,
        },
        query_egress_allowed=True,
    )


class Authorizer:
    def __init__(self, views=None):
        self.views = views or [view()]
        self.calls = 0

    def read(self, principal, gateway):
        result = self.views[min(self.calls, len(self.views) - 1)]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


class Reader:
    def current(self, control):
        return IndexSnapshot.build(GATEWAY, [BUSINESS])


def context(**changes):
    custom = {
        "bedrockAgentCoreMessageVersion": "1.0",
        "bedrockAgentCoreAwsRequestId": "request-fixture",
        "bedrockAgentCoreMcpMessageId": "mcp-1",
        "bedrockAgentCoreGatewayId": GATEWAY.gateway_id,
        "bedrockAgentCoreTargetId": SEARCH.identity.target_id,
        "bedrockAgentCoreToolName": SEARCH.name,
    }
    custom.update(changes)
    return SimpleNamespace(client_context=SimpleNamespace(custom=custom))


def handler(auth=None):
    return SearchHandler(SEARCH, codec(), auth or Authorizer(), Reader())


def event():
    return {"query": "cancel", "_auth_context": codec().issue(PRINCIPAL, BINDING, policy_epoch=1)}


def test_search_returns_only_business_contract():
    assert handler()(event(), context()) == {"tools": [BUSINESS.descriptor]}


@pytest.mark.parametrize(
    "bad", [{}, {"query": ""}, {"query": " "}, {"query": "x" * 2049}, {"query": "x", "topK": 3}]
)
def test_search_validates_query_and_unknown_fields(bad):
    with pytest.raises(InputError):
        handler()(bad, context())


def test_search_requires_valid_envelope():
    with pytest.raises(AuthenticationError):
        handler()({"query": "cancel"}, context())


@pytest.mark.parametrize(
    "change",
    [
        {"bedrockAgentCoreMcpMessageId": 1},
        {"bedrockAgentCoreMcpMessageId": "other"},
        {"bedrockAgentCoreGatewayId": "other"},
        {"bedrockAgentCoreTargetId": "other"},
        {"bedrockAgentCoreToolName": "other___search_tools"},
    ],
)
def test_search_rejects_context_binding_mismatch(change):
    with pytest.raises(AuthenticationError):
        handler()(event(), context(**change))


def test_envelope_rejects_changed_query_tamper_and_expiry():
    token = codec().issue(PRINCIPAL, BINDING, policy_epoch=1)
    with pytest.raises(AuthenticationError):
        codec().verify(token, replace(BINDING, query="different"))
    with pytest.raises(AuthenticationError):
        codec().verify(token[:-2] + "aa", BINDING)
    with pytest.raises(AuthenticationError):
        EnvelopeCodec(Keys(), clock=lambda: NOW + 61).verify(token, BINDING)


@pytest.mark.parametrize("length", [8192, 8193])
def test_search_enforces_signed_envelope_length_boundary(length):
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    short = codec().issue(PRINCIPAL, BINDING, policy_epoch=1)
    assert codec().verify(short, BINDING).principal == PRINCIPAL
    header_text, body_text, signature = short.split(".")
    header = base64.urlsafe_b64decode(header_text + "=" * (-len(header_text) % 4))
    body = base64.urlsafe_b64decode(body_text + "=" * (-len(body_text) % 4))
    for header_spaces in range(3):
        # 只补合法 JSON 空白，不改身份、时效、请求绑定或字段集合。
        padded_header = encode(header + b" " * header_spaces)
        body_length = length - len(padded_header) - len(signature) - 2
        padded_body = body + b" " * (body_length * 3 // 4 - len(body))
        encoded_body = encode(padded_body)
        if len(encoded_body) == body_length:
            break
    else:
        pytest.fail("无法构造精确长度的有效封套")
    assert json.loads(padded_body) == json.loads(body)
    signed = f"{padded_header}.{encoded_body}"
    signature = encode(hmac.digest(Keys().current()[1], signed.encode("ascii"), "sha256"))
    token = f"{signed}.{signature}"
    assert len(token) == length
    arguments = {"query": BINDING.query, "_auth_context": token}
    if length == 8192:
        assert handler()(arguments, context()) == {"tools": [BUSINESS.descriptor]}
    else:
        with pytest.raises(AuthenticationError):
            handler()(arguments, context())


def test_search_denies_missing_search_permission():
    with pytest.raises(AuthorizationError):
        handler(Authorizer([view([BUSINESS.tool_id])]))(event(), context())


def test_search_rechecks_permissions_before_return():
    auth = Authorizer([view(), view(), view([SEARCH.tool_id], epoch=2)])
    assert handler(auth)(event(), context()) == {"tools": []}
    assert auth.calls >= 3


def test_search_authorization_failure_does_not_degrade_to_empty():
    with pytest.raises(AuthorizationError):
        handler(Authorizer([view(), AuthorizationError()]))(event(), context())


def interceptor_event(method="tools/call", *, params=None):
    return {
        "interceptorInputVersion": "1.0",
        "mcp": {
            "gatewayRequest": {
                "path": "/mcp",
                "httpMethod": "POST",
                "headers": {"Authorization": "Bearer fixture"},
                "body": {
                    "jsonrpc": "2.0",
                    "id": "mcp-1",
                    "method": method,
                    "params": params
                    if params is not None
                    else {
                        "name": SEARCH.name,
                        "arguments": {"query": "cancel", "_auth_context": "forged"},
                    },
                },
            }
        },
    }


class Verifier:
    def __init__(self, principal=PRINCIPAL):
        self.principal = principal

    def verify(self, token):
        assert token == "fixture"  # nosec B105
        return self.principal


class Limiter:
    def admit(self, principal, gateway, *, machine=False):
        pass


def interceptor(principal=PRINCIPAL, auth=None):
    return RequestInterceptor(
        InterceptorConfig(SEARCH, frozenset({("https://idp.example.invalid", "machine-fixture")})),
        Verifier(principal),
        codec(),
        auth or Authorizer(),
        Limiter(),
    )


def test_interceptor_overwrites_client_envelope():
    result = interceptor()(interceptor_event(), None)
    body = result["mcp"]["transformedGatewayRequest"]["body"]
    actual = body["params"]["arguments"]["_auth_context"]
    assert actual != "forged"
    assert codec().verify(actual, BINDING).principal == PRINCIPAL


def test_bootstrap_is_single_page_and_does_not_forward():
    result = interceptor()(interceptor_event("tools/list", params={}), None)
    assert "transformedGatewayRequest" not in result["mcp"]
    assert result["mcp"]["transformedGatewayResponse"]["body"]["result"] == {
        "tools": [SEARCH.descriptor]
    }


def test_bootstrap_rejects_cursor():
    with pytest.raises(InputError):
        interceptor()(interceptor_event("tools/list", params={"cursor": "opaque"}), None)


def test_business_tool_requires_full_identity_authorization():
    request = interceptor_event(params={"name": BUSINESS.name, "arguments": {}})
    with pytest.raises(AuthorizationError):
        interceptor(auth=Authorizer([view([SEARCH.tool_id])]))(request, None)


def test_machine_can_list_but_cannot_call():
    machine = replace(PRINCIPAL, subject="machine-fixture")
    listing = interceptor(machine)(
        interceptor_event("tools/list", params={"cursor": "opaque"}), None
    )
    assert "transformedGatewayRequest" in listing["mcp"]
    with pytest.raises(AuthorizationError):
        interceptor(machine)(interceptor_event(), None)


def test_oidc_validates_signature_claims_and_jwks_cache():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid="fixture-key", alg="RS256", use="sig")
    calls = []

    def transport(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"keys": [jwk]})

    verifier = OIDCVerifier(
        OIDCConfig(
            "https://idp.example.invalid",
            "https://idp.example.invalid/jwks",
            "gateway",
            frozenset({"idp.example.invalid"}),
        ),
        httpx.Client(transport=httpx.MockTransport(transport)),
        clock=lambda: NOW,
    )
    claims = {
        "iss": PRINCIPAL.issuer,
        "sub": PRINCIPAL.subject,
        "aud": "gateway",
        "iat": NOW,
        "exp": NOW + 300,
        "token_use": "access",
    }
    token = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "fixture-key"})
    assert verifier.verify(token).subject == PRINCIPAL.subject
    verifier.verify(token)
    assert len(calls) == 1
    for bad in [
        {"aud": "other"},
        {"exp": NOW + 301},
        {"token_use": "id"},
        {"iat": NOW + 60},
        {"exp": NOW - 60},
    ]:
        with pytest.raises(AuthenticationError):
            verifier.verify(
                jwt.encode(claims | bad, private, algorithm="RS256", headers={"kid": "fixture-key"})
            )


@pytest.mark.parametrize(
    "uri",
    ["http://idp.example.invalid/jwks", "https://127.0.0.1/jwks", "https://elsewhere.invalid/jwks"],
)
def test_oidc_rejects_non_public_or_non_allowlisted_jwks(uri):
    with pytest.raises(ValueError):
        OIDCConfig(PRINCIPAL.issuer, uri, "gateway", frozenset({"idp.example.invalid"}))


def test_fixed_window_limiter_uses_conditional_counter_and_server_time():
    class Client:
        def __init__(self):
            self.request = None

        def update_item(self, **kwargs):
            self.request = kwargs
            return {}

    client = Client()
    limiter = FixedWindowLimiter(client, "fixture-table", ordinary_limit=600, clock=lambda: 121)
    limiter.admit(PRINCIPAL, GATEWAY)
    assert client.request["ConditionExpression"]
    assert client.request["ExpressionAttributeValues"][":limit"] == {"N": "600"}
    assert client.request["ExpressionAttributeValues"][":ttl"] == {"N": "240"}


def test_rate_limit_error_has_retry_after():
    from botocore.exceptions import ClientError

    class Client:
        def update_item(self, **kwargs):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")

    with pytest.raises(RateLimitError) as result:
        FixedWindowLimiter(Client(), "fixture-table", clock=lambda: 121).admit(PRINCIPAL, GATEWAY)
    assert result.value.retry_after == 59


def test_documented_gateway_fixture_round_trip():
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures/service/gateway-event.synthetic.json").read_text()
    )
    assert fixture["provenance"]["live_gateway_observation"] is False
    rewritten = interceptor()(fixture["interceptor"], None)
    arguments = rewritten["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    lambda_context = SimpleNamespace(
        client_context=SimpleNamespace(custom=fixture["context_custom"])
    )
    assert handler()(arguments, lambda_context) == {"tools": [BUSINESS.descriptor]}


def test_exact_mcp_id_type_is_bound():
    with pytest.raises(ValueError, match="MCP ID"):
        replace(BINDING, mcp_id=1)
    binding = replace(BINDING, mcp_id="1")
    token = codec().issue(PRINCIPAL, binding, policy_epoch=1)
    assert codec().verify(token, binding).principal == PRINCIPAL
    with pytest.raises(AuthenticationError):
        codec().verify(token, replace(binding, mcp_id="01"))


def test_search_masks_removed_or_schema_changed_tools():
    current = view()
    for safety in (
        {SEARCH.tool_id: SEARCH.schema_digest},
        {
            SEARCH.tool_id: SEARCH.schema_digest,
            BUSINESS.tool_id: "different-schema",
        },
    ):
        authorizer = Authorizer([replace(current, schema_digests=safety)])
        assert handler(authorizer)(event(), context()) == {"tools": []}


def test_tombstone_and_policy_rollback_fail_closed():
    for control, error_type in (
        (SimpleNamespace(policy_epoch=1, tombstoned=True), AuthorizationError),
        (SimpleNamespace(policy_epoch=0, tombstoned=False), ServiceError),
    ):
        with pytest.raises(error_type) as result:
            handler(Authorizer([replace(view(), control=control)]))(event(), context())
        assert type(result.value) is error_type


def test_provider_failure_returns_bm25_without_internal_fields():
    from semantic_core.providers import MockEmbeddingProvider, RequestContext
    from semantic_core.vectors import VectorIndex

    provider = MockEmbeddingProvider()
    vectors = provider.embed(
        [BUSINESS.summary], "document", RequestContext("fixture", time.monotonic() + 5)
    )
    snapshot = IndexSnapshot.build(
        GATEWAY,
        [BUSINESS],
        vectors=VectorIndex(vectors.vectors, [0], 1, 16),
        model_profile=provider.describe(),
    )

    class VectorReader:
        def current(self, control):
            return snapshot

    class BrokenProvider(MockEmbeddingProvider):
        def embed(self, texts, purpose, context):
            raise RuntimeError("provider body must not leak")

    service = SearchHandler(SEARCH, codec(), Authorizer(), VectorReader(), BrokenProvider())
    assert service(event(), context()) == {"tools": [BUSINESS.descriptor]}


def policy_document():
    return {
        "version": 1,
        "epoch": 1,
        "principals": [
            {
                "issuer": PRINCIPAL.issuer,
                "subject": PRINCIPAL.subject,
                "scopes": [],
                "groups": [],
                "allowed_tools": [SEARCH.tool_id, BUSINESS.tool_id],
                "allow_query_egress": True,
                "not_before": 0,
                "disabled": False,
            }
        ],
        "tools": {
            SEARCH.name: {"id": SEARCH.tool_id, "schema_digest": SEARCH.schema_digest},
            BUSINESS.name: {"id": BUSINESS.tool_id, "schema_digest": BUSINESS.schema_digest},
        },
    }


class PolicyStore:
    def __init__(self, policy=None, safety=None):
        self.policy = policy or policy_document()
        self.safety = safety if safety is not None else view().schema_digests
        self.control = SimpleNamespace(
            gateway=GATEWAY, tombstoned=False, policy_epoch=1, safety_revision=1, safety=object()
        )
        self.strong_reads = 0

    def read_control(self):
        self.strong_reads += 1
        return self.control

    def load_policy(self, control):
        assert control is self.control
        return self.policy

    def load_safety(self, control):
        assert control is self.control
        return self.safety


def test_policy_authorization_intersects_current_safety_and_reads_each_time():
    store = PolicyStore(safety={SEARCH.tool_id: SEARCH.schema_digest})
    authorizer = DDBAuthorizer(store)
    for _ in range(2):
        actual = authorizer.read(PRINCIPAL, GATEWAY)
        assert actual.allowed_ids == frozenset({SEARCH.tool_id})
        assert actual.query_egress_allowed is True
    assert store.strong_reads == 2


@pytest.mark.parametrize(
    "edit",
    [
        {"disabled": True},
        {"not_before": NOW + 1},
        {"scopes": ["missing-scope"]},
        {"groups": ["missing-group"]},
        {"subject": "other-subject"},
        {"allowed_tools": ["not-a-registered-identity"]},
    ],
)
def test_policy_denies_revocation_unknown_principal_and_unregistered_tool(edit):
    document = policy_document()
    document["principals"][0].update(edit)
    expected = ServiceError if "allowed_tools" in edit else AuthorizationError
    with pytest.raises(expected) as result:
        DDBAuthorizer(PolicyStore(document)).read(PRINCIPAL, GATEWAY)
    assert type(result.value) is expected


def test_policy_epoch_and_gateway_must_match():
    for document in (
        policy_document() | {"epoch": 2},
        policy_document()
        | {
            "tools": {
                SEARCH.name: {
                    "id": SEARCH.tool_id.replace("gateway-fixture", "other"),
                    "schema_digest": SEARCH.schema_digest,
                },
            }
        },
    ):
        with pytest.raises(ServiceError) as result:
            DDBAuthorizer(PolicyStore(document)).read(PRINCIPAL, GATEWAY)
        assert type(result.value) is ServiceError


def test_duplicate_authorization_header_is_rejected():
    request = interceptor_event()
    request["mcp"]["gatewayRequest"]["headers"]["authorization"] = "Bearer attacker"
    with pytest.raises(AuthenticationError):
        interceptor()(request, None)


def test_business_call_does_not_keep_untrusted_auth_context():
    request = interceptor_event(
        params={
            "name": BUSINESS.name,
            "arguments": {"_auth_context": "forged", "order_id": "synthetic"},
        }
    )
    result = interceptor()(request, None)
    assert result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"] == {
        "order_id": "synthetic"
    }


def test_limiter_denies_dependency_failures():
    class Client:
        def update_item(self, **kwargs):
            raise RuntimeError("database unavailable")

    with pytest.raises(ServiceError) as result:
        FixedWindowLimiter(Client(), "fixture").admit(PRINCIPAL, GATEWAY)
    assert type(result.value) is ServiceError


def test_machine_can_bootstrap_initial_catalog_without_safety_snapshot():
    machine = replace(PRINCIPAL, subject="machine-fixture")
    document = policy_document()
    document["principals"][0]["subject"] = machine.subject
    store = PolicyStore(document)
    store.control.safety = None
    auth = DDBAuthorizer(store)
    assert auth.read(machine, GATEWAY).allowed_ids == frozenset()
    result = interceptor(machine, auth)(
        interceptor_event("tools/list", params={"cursor": "opaque"}), None
    )
    assert "transformedGatewayRequest" in result["mcp"]
    with pytest.raises(AuthorizationError):
        interceptor(machine, auth)(interceptor_event(), None)
    ordinary = PolicyStore()
    ordinary.control.safety = None
    with pytest.raises(AuthorizationError):
        interceptor(auth=DDBAuthorizer(ordinary))(interceptor_event("tools/list", params={}), None)


def test_schema_change_requires_explicit_policy_reapproval_for_business_call():
    changed = dict(view().schema_digests) | {BUSINESS.tool_id: "1" * 64}
    auth = DDBAuthorizer(PolicyStore(safety=changed))
    assert BUSINESS.tool_id not in auth.read(PRINCIPAL, GATEWAY).allowed_ids
    request = interceptor_event(params={"name": BUSINESS.name, "arguments": {}})
    with pytest.raises(AuthorizationError):
        interceptor(auth=auth)(request, None)
    approved = policy_document()
    approved["tools"][BUSINESS.name]["schema_digest"] = "1" * 64
    result = interceptor(auth=DDBAuthorizer(PolicyStore(approved, changed)))(request, None)
    assert "transformedGatewayRequest" in result["mcp"]


def test_oidc_unknown_kid_refresh_is_bounded_and_rotation_recovers():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid="known", alg="RS256", use="sig")
    clock = [NOW]
    keys = [jwk]
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"keys": keys})

    config = OIDCConfig(
        PRINCIPAL.issuer,
        f"{PRINCIPAL.issuer}/jwks",
        "gateway",
        frozenset({"idp.example.invalid"}),
    )
    verifier = OIDCVerifier(
        config, httpx.Client(transport=httpx.MockTransport(transport)), clock=lambda: clock[0]
    )
    claims = {
        "iss": PRINCIPAL.issuer,
        "sub": PRINCIPAL.subject,
        "aud": "gateway",
        "iat": NOW,
        "exp": NOW + 300,
        "token_use": "access",
    }

    def token(kid):
        return jwt.encode(claims, private, algorithm="RS256", headers={"kid": kid})

    verifier.verify(token("known"))
    for kid in ("missing", "missing-again"):
        with pytest.raises(AuthenticationError):
            verifier.verify(token(kid))
    assert len(calls) == 2
    keys.append(jwk | {"kid": "rotated"})
    clock[0] += 31
    assert verifier.verify(token("rotated")).subject == PRINCIPAL.subject
    assert len(calls) == 3


@pytest.mark.parametrize(
    "headers",
    [
        {"alg": "none", "kid": "key"},
        {"alg": "HS256", "kid": "key"},
        {"alg": "RS256", "kid": "key", "jku": "https://untrusted.invalid/jwks"},
    ],
)
def test_oidc_rejects_algorithm_confusion_and_token_jwks_url_without_network(headers):
    import base64

    def transport(request):
        pytest.fail("untrusted JWT must be rejected before network")

    encoded_header = base64.urlsafe_b64encode(json.dumps(headers).encode()).rstrip(b"=").decode()
    token = encoded_header + ".e30.c2ln"
    verifier = OIDCVerifier(
        OIDCConfig(
            PRINCIPAL.issuer,
            PRINCIPAL.issuer + "/jwks",
            "gateway",
            frozenset({"idp.example.invalid"}),
        ),
        httpx.Client(transport=httpx.MockTransport(transport)),
    )
    with pytest.raises(AuthenticationError):
        verifier.verify(token)


def test_oidc_discovery_must_match_configured_issuer_and_jwks():
    calls = []

    def transport(request):
        calls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "issuer": PRINCIPAL.issuer,
                "jwks_uri": "https://untrusted.invalid/jwks",
            },
        )

    config = OIDCConfig(
        PRINCIPAL.issuer,
        PRINCIPAL.issuer + "/jwks",
        "gateway",
        frozenset({"idp.example.invalid"}),
        discovery_uri=PRINCIPAL.issuer + "/.well-known/openid-configuration",
    )
    verifier = OIDCVerifier(config, httpx.Client(transport=httpx.MockTransport(transport)))
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode({"sub": "fixture"}, private, algorithm="RS256", headers={"kid": "fixture"})
    with pytest.raises(ServiceError) as result:
        verifier.verify(token)
    assert type(result.value) is ServiceError
    assert calls == [config.discovery_uri]


def test_missing_pass_request_headers_fails_authenticated_boundary():
    request = interceptor_event()
    del request["mcp"]["gatewayRequest"]["headers"]
    with pytest.raises(AuthenticationError):
        interceptor()(request, None)


def test_fixed_window_aws_request_passes_stubber_validation():
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(  # nosec B106
        "dynamodb",
        region_name="cn-north-1",
        aws_access_key_id="synthetic-access",
        aws_secret_access_key="synthetic-secret",
    )
    with Stubber(client) as stubber:
        stubber.add_response("update_item", {})
        FixedWindowLimiter(client, "fixture-table", clock=lambda: 121).admit(PRINCIPAL, GATEWAY)
        stubber.assert_no_pending_responses()


def test_authorization_dependency_failure_is_internal_not_user_denial():
    class UnavailableStore(PolicyStore):
        def read_control(self):
            raise RuntimeError("private backend failure details")

    with pytest.raises(ServiceError) as result:
        DDBAuthorizer(UnavailableStore()).read(PRINCIPAL, GATEWAY)
    assert type(result.value) is ServiceError
    assert str(result.value) == "internal failure"


def test_policy_corruption_is_internal_but_unknown_subject_still_denied():
    corrupt = policy_document() | {"epoch": 99}
    with pytest.raises(ServiceError) as result:
        DDBAuthorizer(PolicyStore(corrupt)).read(PRINCIPAL, GATEWAY)
    assert type(result.value) is ServiceError
    with pytest.raises(AuthorizationError):
        DDBAuthorizer(PolicyStore()).read(replace(PRINCIPAL, subject="unknown-subject"), GATEWAY)


@pytest.mark.parametrize(
    "responses",
    [
        [ServiceError()],
        [view(), view(), ServiceError()],
    ],
)
def test_search_preserves_initial_and_reauthorization_internal_faults(responses):
    with pytest.raises(ServiceError) as result:
        handler(Authorizer(responses))(event(), context())
    assert type(result.value) is ServiceError


def test_jwks_dependency_outage_is_internal_without_disclosure():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode({"sub": "fixture"}, private, algorithm="RS256", headers={"kid": "fixture"})

    def unavailable(request):
        return httpx.Response(503, text="private IdP failure details")

    verifier = OIDCVerifier(
        OIDCConfig(
            PRINCIPAL.issuer,
            PRINCIPAL.issuer + "/jwks",
            "gateway",
            frozenset({"idp.example.invalid"}),
        ),
        httpx.Client(transport=httpx.MockTransport(unavailable)),
    )
    with pytest.raises(ServiceError) as result:
        verifier.verify(token)
    assert type(result.value) is ServiceError
    assert str(result.value) == "internal failure"


def test_search_success_observer_emits_only_fixed_retrieval_mode():
    observed = []
    service = SearchHandler(
        SEARCH,
        codec(),
        Authorizer(),
        Reader(),
        observer=lambda mode, abstained: observed.append((mode, abstained)),
    )
    assert service(event(), context()) == {"tools": [BUSINESS.descriptor]}
    assert observed == [("bm25", False)]

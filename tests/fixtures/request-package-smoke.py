"""Offline REQUEST behavior using only the selected host dependency closure."""

import base64
import importlib.util
import io
import json
import math
import os
import socket
import sys
import tempfile
import time
from contextlib import ExitStack, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
network_attempts = tempfile.TemporaryFile()


def no_network(*args, **kwargs):
    os.write(network_attempts.fileno(), b"x")
    raise AssertionError("isolated package test attempted network access")


socket.getaddrinfo = no_network
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
excluded = ("numpy", "jieba", "boto3", "s3transfer", "setuptools", "pkg_resources")
for name in excluded:
    assert importlib.util.find_spec(name) is None, name

descriptor = {
    "name": "discovery___search_tools",
    "description": "Discover authorized tools",
    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
}
issuer = "https://idp.example.invalid"
os.environ.update(
    {
        "SEMSEARCH_ACCOUNT": "000000000000",
        "SEMSEARCH_GATEWAY_ID": "gateway-fixture",
        "SEMSEARCH_SEARCH_TARGET_ID": "target-fixture",
        "SEMSEARCH_SEARCH_TOOL_NAME": descriptor["name"],
        "SEMSEARCH_SEARCH_INCARNATION": "generation-1",
        "SEMSEARCH_SEARCH_DESCRIPTOR_JSON": json.dumps(descriptor),
        "SEMSEARCH_BUCKET": "snapshots-fixture",
        "SEMSEARCH_TABLE": "state-fixture",
        "SEMSEARCH_PROTECTED_TABLE": "protected-fixture",
        "SEMSEARCH_MACHINE_PRINCIPALS_JSON": "[]",
        "OIDC_ISSUER": issuer,
        "OIDC_AUDIENCE": "gateway-audience",
        "OIDC_DISCOVERY_URI": issuer + "/discovery",
        "OIDC_JWKS_URI": issuer + "/keys",
        "OIDC_ALLOWED_HOSTS": "idp.example.invalid",
        "AWS_LAMBDA_FUNCTION_NAME": "request-fixture",
        "_HANDLER": "semantic_service.request_runtime.request_handler",
    }
)

# Imports happen after replacing sys.path and blocking external connections.
import httpx  # noqa: E402
import jwt  # noqa: E402
from botocore.stub import Stubber  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from semantic_core.catalog import canonical_json, digest  # noqa: E402
from semantic_service import runtime  # noqa: E402
from semantic_service.auth import (  # noqa: E402
    AuthenticationError,
    AuthorizationError,
    DDBAuthorizer,
    EnvelopeBinding,
    FixedWindowLimiter,
    RateLimitError,
)
from semantic_service.storage import ControlRecord, ObjectRef  # noqa: E402

private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
key = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
key.update({"kid": "fixture", "alg": "RS256", "use": "sig"})


class SharedOutput:
    """Observe raw fd logs and Python summaries without buffering ambiguity."""

    def __init__(self):
        self.file = tempfile.TemporaryFile(mode="a+b")

    def write(self, text):
        return os.write(self.file.fileno(), text.encode())

    def flush(self):
        pass

    def getvalue(self):
        descriptor = self.file.fileno()
        return os.pread(descriptor, os.fstat(descriptor).st_size, 0).decode()


metadata_output = SharedOutput()
request_output = SharedOutput()
before_compensation_output = SharedOutput()
request_ids = [f"01234567-89ab-cdef-0123-{index:012d}" for index in range(1, 5)]


def metadata_response(request, *, transport_id=None):
    metadata_output.write(
        json.dumps(
            {
                "path": request.url.path,
                "timeout": dict(request.extensions["timeout"]),
                "pid": os.getpid(),
                "transport_id": transport_id,
            }
        )
        + "\n"
    )
    paths = [json.loads(line)["path"] for line in metadata_output.getvalue().splitlines()]
    if request.url.path == "/discovery":
        return httpx.Response(200, json={"issuer": issuer, "jwks_uri": issuer + "/keys"})
    assert request.url.path == "/keys"
    if paths.count("/keys") == 1:
        raise httpx.ReadTimeout("synthetic-request-smoke-read-timeout")
    # Inspect output before the successful compensation, then assert outside HTTP
    # handling so a broken log contract cannot be hidden by ServiceError redaction.
    before_compensation_output.write(request_output.getvalue())
    return httpx.Response(200, json={"keys": [key]})


def prefetch_transport_response(self, request):
    return metadata_response(request, transport_id=id(self))


client = httpx.Client(transport=httpx.MockTransport(metadata_response), trust_env=False)
# Install the request-path client; prefetch creates its own production client.
# This is a valid lambda callback, not a module-level return statement.
# nosemgrep: return-not-in-function
runtime._http = lambda: client
with ExitStack() as stack:
    from semantic_service import _request_prefetch

    stack.enter_context(
        patch.object(httpx.HTTPTransport, "handle_request", prefetch_transport_response)
    )
    original_prefetch = _request_prefetch.prefetch

    def prefetch(verifier, **kwargs):
        return original_prefetch(verifier, **{**kwargs, "log_fd": request_output.file.fileno()})

    stack.enter_context(patch.object(_request_prefetch, "prefetch", prefetch))
    with redirect_stdout(request_output):
        from semantic_service import request_runtime

provisioned = os.environ["AWS_LAMBDA_INITIALIZATION_TYPE"] == "provisioned-concurrency"
assert runtime._request.cache_info().currsize == int(provisioned)
handler = runtime._request()
assert handler.verifier.client is client
assert isinstance(handler.authorizer, DDBAuthorizer)
assert isinstance(handler.limiter, FixedWindowLimiter)
for service in ("s3", "dynamodb", "secretsmanager"):
    assert runtime._aws(service).meta.region_name == "cn-north-1"
tool = handler.config.search_tool
now = int(time.time())
# Freeze only the wall clock; the production limiter still issues the conditional update.
# This is a valid lambda callback, not a module-level return statement.
# nosemgrep: return-not-in-function
handler.limiter.clock = lambda: now
store = runtime._store()
policy = {
    "version": 1,
    "epoch": 1,
    "principals": [
        {
            "issuer": issuer,
            "subject": "fixture",
            "scopes": [],
            "groups": [],
            "allowed_tools": [tool.tool_id],
            "allow_query_egress": False,
            "not_before": 0,
            "disabled": False,
        }
    ],
    "tools": {tool.name: {"id": tool.tool_id, "schema_digest": tool.schema_digest}},
}
safety = {
    "version": 1,
    "gateway": store.gateway.as_dict(),
    "revision": 1,
    "tools": {tool.tool_id: tool.schema_digest},
}


def object_response(stubber, name, value):
    data = canonical_json(value)
    reference = ObjectRef(store.prefix + name, name + "-version", len(data), digest(data))
    stubber.add_response(
        "get_object",
        {
            "Body": io.BytesIO(data),
            "ContentLength": len(data),
            "VersionId": reference.version_id,
        },
        {"Bucket": store.bucket, "Key": reference.key, "VersionId": reference.version_id},
    )
    return reference


def authorization_responses(s3, dynamodb):
    control = ControlRecord(
        store.gateway,
        safety_revision=1,
        policy=object_response(s3, "policy", policy),
        safety=object_response(s3, "safety", safety),
    )
    # The production store reads PROTECTED, CONTROL, PROTECTED consistently.
    for table, key, item in (
        (store.protected_table, store.protected_key, store._protected_item(control)),
        (store.table, store.key, store._item(control)),
        (store.protected_table, store.protected_key, store._protected_item(control)),
    ):
        dynamodb.add_response(
            "get_item",
            {"Item": item},
            {"TableName": table, "Key": {"pk": {"S": key}}, "ConsistentRead": True},
        )


minute = now // 60
namespace = digest(canonical_json([store.gateway.as_dict(), issuer, "fixture"]))
counter_request = {
    "TableName": store.table,
    "Key": {"pk": {"S": f"rate/{namespace}/{minute}"}},
    "UpdateExpression": "SET #ttl = if_not_exists(#ttl, :ttl) ADD #count :one",
    "ConditionExpression": "attribute_not_exists(#count) OR #count < :limit",
    "ExpressionAttributeNames": {"#count": "count", "#ttl": "expires_at"},
    "ExpressionAttributeValues": {
        ":ttl": {"N": str((minute + 2) * 60)},
        ":one": {"N": "1"},
        ":limit": {"N": "600"},
    },
}
claims = {
    "iss": issuer,
    "sub": "fixture",
    "aud": "gateway-audience",
    "iat": now,
    "exp": now + 120,
    "token_use": "access",
}
token = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "fixture"})
body = {
    "jsonrpc": "2.0",
    "id": "request-fixture",
    "method": "tools/call",
    "params": {
        "name": tool.name,
        "arguments": {"query": "fixture", "_auth_context": "untrusted"},
    },
}
event = {
    "interceptorInputVersion": "1.0",
    "mcp": {
        "gatewayRequest": {
            "path": "/mcp",
            "httpMethod": "POST",
            "headers": {"Authorization": "Bearer " + token},
            "body": body,
        }
    },
}
with (
    redirect_stdout(request_output),
    Stubber(runtime._aws("s3")) as s3,
    Stubber(runtime._aws("dynamodb")) as dynamodb,
    Stubber(runtime._aws("secretsmanager")) as secrets,
):
    authorization_responses(s3, dynamodb)
    dynamodb.add_response("update_item", {}, counter_request)
    secrets.add_response(
        "get_secret_value",
        {
            "SecretString": json.dumps(
                {"key_b64": base64.b64encode(b"x" * 32).decode(), "activated_at": now - 1}
            ),
            "VersionId": "v" * 32,
        },
        {"SecretId": "semsearch/signing", "VersionStage": "AWSCURRENT"},
    )
    result = request_runtime.request_handler(event, SimpleNamespace(aws_request_id=request_ids[0]))
    trusted = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    binding = EnvelopeBinding(
        "gateway-fixture", "target-fixture", tool.name, "request-fixture", "fixture"
    )
    assert trusted["_auth_context"] != "untrusted"
    assert handler.envelope.verify(trusted["_auth_context"], binding).principal.subject == "fixture"

    # A real conditional counter rejection retains the existing rate-limit boundary.
    authorization_responses(s3, dynamodb)
    dynamodb.add_client_error(
        "update_item",
        service_error_code="ConditionalCheckFailedException",
        expected_params=counter_request,
    )
    try:
        request_runtime.request_handler(event, SimpleNamespace(aws_request_id=request_ids[1]))
    except RateLimitError:
        pass
    else:
        raise AssertionError("rate-limited request was accepted")

    # A revoked principal still traverses the real immutable policy and safety reads.
    policy["principals"][0]["disabled"] = True
    authorization_responses(s3, dynamodb)
    try:
        request_runtime.request_handler(event, SimpleNamespace(aws_request_id=request_ids[2]))
    except AuthorizationError:
        pass
    else:
        raise AssertionError("revoked principal was accepted")
    for stubber in (s3, dynamodb, secrets):
        stubber.assert_no_pending_responses()
metadata_rows = [json.loads(line) for line in metadata_output.getvalue().splitlines()]
metadata_requests = [row["path"] for row in metadata_rows]
metadata_timeouts = [row["timeout"] for row in metadata_rows]
assert metadata_requests == ["/discovery", "/keys", "/keys"]
assert metadata_timeouts == [
    dict.fromkeys(("connect", "read", "write", "pool"), timeout)
    for timeout in ((0.8, 0.8, 0.35) if provisioned else (2.0, 2.0, 0.5))
]
event["mcp"]["gatewayRequest"]["headers"]["Authorization"] = "Bearer invalid"
with redirect_stdout(request_output):
    try:
        request_runtime.request_handler(event, SimpleNamespace(aws_request_id=request_ids[3]))
    except AuthenticationError:
        pass
    else:
        raise AssertionError("invalid JWT was accepted")

rows = [json.loads(line) for line in request_output.getvalue().splitlines()]
attempts = [row for row in rows if row["operation"] == "oidc_fetch"]
summaries = [row for row in rows if row["operation"] == "request"]
initializations = [row for row in rows if row["operation"] == "request_init"]
before_compensation = [
    json.loads(line) for line in before_compensation_output.getvalue().splitlines()
]
assert len(attempts) == 2, "JWKS attempt events must include the failure and compensation"
assert before_compensation == [attempts[0]], "JWKS attempt failure was not written before retry"
assert rows == attempts + initializations + summaries, "JWKS attempts must precede their summary"
assert len(initializations) == int(provisioned)
assert len(summaries) == 4 and [row["aws_request_id"] for row in summaries] == request_ids
assert [row["status"] for row in summaries] == [
    "ok",
    "rate limited",
    "unauthorized",
    "unauthenticated",
]
metadata_summary = initializations[0] if provisioned else summaries[0]
recovery = metadata_summary["oidc_prefetch"] if provisioned else metadata_summary
assert recovery["auth_recovered"] is True
if provisioned:
    assert initializations[0]["oidc_prefetch"]["status"] == "ready"
    assert all("auth_recovered" not in row for row in summaries)
    assert {row["pid"] for row in metadata_rows} == {os.getpid()}
    transports = {row["transport_id"] for row in metadata_rows}
    assert len(transports) == 1 and None not in transports
    assert id(client._transport) not in transports
assert all("auth_failure" not in row for row in summaries)
assert all("auth_recovered" not in row for row in summaries[1:])
assert metadata_summary["stage_timings"]["auth.jwks"]["count"] == 2
assert metadata_summary["stage_timings"]["auth.jwks"]["errors"] == 1
assert all("auth.jwks" not in row["stage_timings"] for row in summaries[0 if provisioned else 1 :])
for index, attempt in enumerate(attempts, 1):
    assert set(attempt) == {
        "operation",
        "event",
        "endpoint",
        "aws_request_id",
        "attempt",
        "compensation",
        "duration_ms",
        "outcome",
        "failure",
    } | ({"phase"} if provisioned else set()), "JWKS attempt schema changed"
    assert attempt["event"] == "attempt_finished" and attempt["endpoint"] == "jwks"
    assert attempt["aws_request_id"] == (None if provisioned else request_ids[0])
    if provisioned:
        assert attempt["phase"] == "init"
    assert attempt["attempt"] == index and attempt["compensation"] is (index == 2)
    assert type(attempt["duration_ms"]) in (int, float)
    assert math.isfinite(attempt["duration_ms"]) and attempt["duration_ms"] >= 0
assert attempts[0]["outcome"] == "failed" and attempts[0]["failure"] == {
    "endpoint": "jwks",
    "category": "timeout",
    "reason": "read",
    "http_status": None,
    "cause": None,
}
assert attempts[1]["outcome"] == "ok" and attempts[1]["failure"] is None
for private_value in (issuer, token, key["n"], "synthetic-request-smoke-read-timeout"):
    assert private_value not in request_output.getvalue(), "JWKS attempt leaked fixture input"
print(request_output.getvalue(), end="")
forbidden = (
    "semantic_core.bm25",
    "semantic_core.providers",
    "semantic_core.search",
    "semantic_core.snapshot",
    "semantic_core.vectors",
    "semantic_service.catalog",
    "semantic_service.enhancer",
    "semantic_service.indexer",
    "semantic_service.models",
    "semantic_service.sdk",
    "semantic_service.search",
)
loaded = [
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
]
assert not loaded, "REQUEST imported retrieval modules: " + ", ".join(loaded)
client.close()
assert os.fstat(network_attempts.fileno()).st_size == 0, "smoke caught a network attempt"
if provisioned:
    print("PASS: parent-process metadata with independent HTTP client")
print("PASS: isolated REQUEST factory, JWT and handler")

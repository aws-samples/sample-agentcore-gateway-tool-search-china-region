"""Synthetic protocol cases exercise identity, pagination and error boundaries offline."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from test_identity import (
    BINDING,
    BUSINESS,
    GATEWAY,
    PRINCIPAL,
    SEARCH,
    Authorizer,
    Limiter,
    Verifier,
    codec,
    context,
    event,
    handler,
    interceptor,
    interceptor_event,
    view,
)

from semantic_core.catalog import ToolIdentity, ToolRecord, canonical_json
from semantic_core.providers import RequestContext
from semantic_service.auth import AuthenticationError, EnvelopeBinding, InputError
from semantic_service.catalog import CatalogError, MCPClient
from semantic_service.interceptor import InterceptorConfig, RequestInterceptor


def capture(name):
    """Construct protocol input independently; no captured service data is used."""
    initialization = {
        "http_status": 200,
        "response": {
            "jsonrpc": "2.0",
            "id": "init-fixture",
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic-gateway", "version": "1"},
            },
        },
    }
    if name == "gateway-protocol-negotiation.json":
        return {"initialization": initialization}
    pages = []
    for index, offset in enumerate(range(0, 205, 30)):
        params = {} if index == 0 else {"cursor": f"opaque-fixture:{index}"}
        result = {
            "tools": [
                {
                    "name": f"fixture___tool{number}",
                    "description": "Synthetic inventory lookup",
                    "inputSchema": {"type": "object"},
                }
                for number in range(offset, min(offset + 30, 205))
            ]
        }
        if offset + 30 < 205:
            result["nextCursor"] = f"opaque-fixture:{index + 1}"
        pages.append(
            {
                "request": {"params": params},
                "sent_protocol_version": "2025-03-26",
                "response": {"jsonrpc": "2.0", "id": "list-fixture", "result": result},
            }
        )
    return {"pages": pages, "total": 205, "unique": 205}


def synthetic_call(request_id):
    name = "fixture___inventory"
    arguments = {"mode": "echo", "query": "synthetic inventory"}
    custom = {
        "bedrockAgentCoreTargetId": "fixture-target",
        "bedrockAgentCoreGatewayId": GATEWAY.gateway_id,
        "bedrockAgentCoreMessageVersion": "1.0",
        # Synthetic type-preserving context, not an observed Gateway contract.
        # See docs/service.md#mcp-id-deployment-acceptance before live use.
        "bedrockAgentCoreMcpMessageId": request_id,
        "bedrockAgentCoreAwsRequestId": "fixture-request",
        "bedrockAgentCoreToolName": name,
    }
    return {
        "request": {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        "http_status": 200,
        "response": {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"event": arguments, "client_context_custom": custom}),
                    }
                ]
            },
        },
    }


LAMBDA_CALLS = [synthetic_call(value) for value in ("string-id", 7, 0, "7")]


def synthetic_request(body=None):
    fixture = Path(__file__).parents[1] / "fixtures/service/gateway-event.synthetic.json"
    request = json.loads(fixture.read_text())["interceptor"]
    request["mcp"]["gatewayResponse"] = None
    request["mcp"]["gatewayRequest"]["body"] = copy.deepcopy(
        LAMBDA_CALLS[0]["request"] if body is None else body
    )
    return request


def synthetic_interceptor():
    # Authorize an independent synthetic business tool alongside Search.
    name = LAMBDA_CALLS[0]["request"]["params"]["name"]
    business = ToolRecord.from_descriptor(
        ToolIdentity(GATEWAY, "probe-target", "fixture", name),
        {"name": name, "description": "Probe", "inputSchema": {"type": "object"}},
    )
    authorized = replace(
        view(),
        allowed_ids=frozenset({SEARCH.tool_id, business.tool_id}),
        tools_by_name={SEARCH.name: SEARCH.identity, business.name: business.identity},
        schema_digests={
            SEARCH.tool_id: SEARCH.schema_digest,
            business.tool_id: business.schema_digest,
        },
    )
    return RequestInterceptor(
        InterceptorConfig(SEARCH, frozenset()),
        Verifier(),
        codec(),
        Authorizer([authorized]),
        Limiter(),
    )


@pytest.mark.parametrize("call", LAMBDA_CALLS, ids=lambda call: repr(call["request"]["id"]))
def test_synthetic_gateway_calls_preserve_string_or_integer_request_ids(call):
    request_id = call["request"]["id"]
    response = call["response"]
    assert response["id"] == request_id
    assert type(response["id"]) is type(request_id)
    assert call["http_status"] == 200
    transformed = synthetic_interceptor()(synthetic_request(call["request"]), None)
    assert transformed["mcp"]["transformedGatewayRequest"]["body"] == call["request"]
    if call["request"]["params"]["arguments"]["mode"] == "echo":
        echo = json.loads(response["result"]["content"][0]["text"])
        custom = echo["client_context_custom"]
        assert custom["bedrockAgentCoreMcpMessageId"] == request_id
        assert custom["bedrockAgentCoreMessageVersion"] == "1.0"
        assert custom["bedrockAgentCoreToolName"] == call["request"]["params"]["name"]
        binding = EnvelopeBinding(
            custom["bedrockAgentCoreGatewayId"],
            custom["bedrockAgentCoreTargetId"],
            custom["bedrockAgentCoreToolName"],
            request_id,
            echo["event"]["query"],
        )
        signed = codec().issue(PRINCIPAL, binding, policy_epoch=1)
        assert codec().verify(signed, binding).principal == PRINCIPAL


@pytest.mark.parametrize("response_field", ["absent", "null", "populated"])
def test_synthetic_request_dispatch_accepts_absent_or_null_response_only(response_field):
    request = synthetic_request()
    if response_field == "absent":
        request["mcp"].pop("gatewayResponse")
    elif response_field == "populated":
        request["mcp"]["gatewayResponse"] = {"body": {"result": {}}}
    if response_field == "populated":
        with pytest.raises(InputError):
            synthetic_interceptor()(request, None)
    else:
        transformed = synthetic_interceptor()(request, None)
        assert (
            transformed["mcp"]["transformedGatewayRequest"]["body"]
            == request["mcp"]["gatewayRequest"]["body"]
        )


@pytest.mark.parametrize("request_id", [7.0, True, False, None, {}, []])
@pytest.mark.parametrize("machine", [False, True])
def test_invalid_external_ids_are_rejected_before_authorization_and_signing(request_id, machine):
    auth = Authorizer()
    principal = replace(PRINCIPAL, subject="machine-fixture") if machine else PRINCIPAL
    request = interceptor_event("initialize" if machine else "tools/call")
    request["mcp"]["gatewayRequest"]["body"]["id"] = request_id
    with pytest.raises(InputError):
        interceptor(principal, auth)(request, None)
    assert auth.calls == 0


@pytest.mark.parametrize(
    "request_id", [7.0, True, False, None, {}, [], "", " ", "x" * 257, 10**256]
)
def test_signed_binding_rejects_invalid_or_unbounded_ids(request_id):
    with pytest.raises(ValueError, match="MCP ID"):
        replace(BINDING, mcp_id=request_id)


@pytest.mark.parametrize("request_id", ["notification-id", 7, None])
def test_initialized_notifications_must_not_carry_an_id(request_id):
    request = interceptor_event("notifications/initialized", params={})
    request["mcp"]["gatewayRequest"]["body"]["id"] = request_id
    with pytest.raises(InputError):
        interceptor()(request, None)


def test_idless_initialized_notification_is_forwarded():
    request = interceptor_event("notifications/initialized", params={})
    request["mcp"]["gatewayRequest"]["body"].pop("id")
    forwarded = interceptor()(request, None)["mcp"]["transformedGatewayRequest"]["body"]
    assert forwarded["method"] == "notifications/initialized"
    assert "id" not in forwarded


@pytest.mark.parametrize("request_id", [7.0, True, False, None, {}, []])
def test_invalid_context_is_rejected_before_envelope_verification(monkeypatch, request_id):
    service = handler()
    monkeypatch.setattr(
        service.envelope, "verify", lambda *_: pytest.fail("invalid context reached verification")
    )
    with pytest.raises(AuthenticationError):
        service(event(), context(bedrockAgentCoreMcpMessageId=request_id))


def test_numeric_signed_claim_matches_canonical_text_only_after_authentication():
    binding = replace(BINDING, mcp_id="7")
    signed = codec().issue(PRINCIPAL, binding, policy_epoch=1)
    header, body, _ = signed.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["mcp_id"] = 7
    body = base64.urlsafe_b64encode(canonical_json(payload)).rstrip(b"=").decode()
    content = f"{header}.{body}"
    signature = (
        base64.urlsafe_b64encode(hmac.digest(b"a" * 32, content.encode(), hashlib.sha256))
        .rstrip(b"=")
        .decode()
    )
    numeric_signed = f"{content}.{signature}"
    assert codec().verify(numeric_signed, binding).principal == PRINCIPAL
    # Changing the JSON type without re-signing must never gain compatibility.
    with pytest.raises(AuthenticationError):
        codec().verify(f"{content}.{signed.rsplit('.', 1)[1]}", binding)
    with pytest.raises(AuthenticationError):
        codec().verify(numeric_signed, replace(binding, mcp_id="07"))
    with pytest.raises(AuthenticationError):
        codec().verify(signed, replace(binding, mcp_id=7))
    with pytest.raises(AuthenticationError):
        codec().verify(signed, replace(binding, mcp_id="07"))
    arguments = {"query": binding.query, "_auth_context": signed}
    assert handler()(arguments, context(bedrockAgentCoreMcpMessageId="7")) == {
        "tools": [BUSINESS.descriptor]
    }


class Tokens:
    def token(self, context, *, force_refresh=False):
        return "fixture"


def request_context():
    return RequestContext("offline-protocol-test", time.monotonic() + 30)


def mcp_client(transport, **kwargs):
    return MCPClient(
        httpx.Client(transport=httpx.MockTransport(transport), trust_env=False),
        "https://gateway.invalid/mcp",
        ("gateway.invalid",),
        Tokens(),
        **kwargs,
    )


def test_default_protocol_negotiates_the_configured_march_profile():
    observed = capture("gateway-protocol-negotiation.json")["initialization"]
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["method"] == "notifications/initialized":
            assert "id" not in payload
            assert request.headers["MCP-Protocol-Version"] == "2025-03-26"
            return httpx.Response(202)
        # Preserve the synthetic result; bind its correlation ID to this request.
        response = observed["response"] | {"id": payload["id"]}
        return httpx.Response(observed["http_status"], json=response)

    client = mcp_client(transport)
    client.initialize(request_context())
    assert requests[0]["params"]["protocolVersion"] == "2025-03-26"
    assert type(requests[0]["id"]) is str
    assert len(requests) == 2


def test_explicit_protocol_allowlist_still_rejects_unaccepted_version():
    observed = capture("gateway-protocol-negotiation.json")["initialization"]

    def transport(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json=observed["response"] | {"id": payload["id"]})

    with pytest.raises(CatalogError, match="unsupported negotiated"):
        mcp_client(transport, protocol_versions=("2025-06-18",)).initialize(request_context())


@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
def test_internal_response_correlation_rejects_numeric_ids(content_type):
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        response = capture("gateway-protocol-negotiation.json")["initialization"]["response"] | {
            "id": int(payload["id"])
        }
        if content_type == "text/event-stream":
            return httpx.Response(
                200,
                headers={"content-type": content_type},
                text=f"data: {json.dumps(response)}\n\n",
            )
        return httpx.Response(200, json=response)

    with pytest.raises(CatalogError):
        mcp_client(transport, protocol_versions=("2025-03-26",)).initialize(request_context())
    assert type(requests[0]["id"]) is str
    assert len(requests) == 1


@pytest.mark.parametrize(
    "filename", ["gateway-tools-list.json", "gateway-tools-list-requested-limit.json"]
)
def test_synthetic_seven_page_catalogs_replay_with_exact_opaque_cursors(filename):
    recorded = capture(filename)
    pages = iter(recorded["pages"])
    calls = []
    traversed = []
    machine = replace(PRINCIPAL, subject="machine-fixture")

    def transport(request):
        payload = json.loads(request.content)
        calls.append(payload)
        gateway_event = synthetic_request(payload)
        forwarded = interceptor(machine)(gateway_event, None)
        assert forwarded["mcp"]["transformedGatewayRequest"]["body"] == payload
        if payload["method"] == "notifications/initialized":
            assert "id" not in payload
            return httpx.Response(202)
        assert type(payload["id"]) is str
        if payload["method"] == "initialize":
            response = capture("gateway-protocol-negotiation.json")["initialization"]["response"]
        else:
            page = next(pages)
            assert payload["params"].get("cursor") == page["request"]["params"].get("cursor")
            assert request.headers["MCP-Protocol-Version"] == page["sent_protocol_version"]
            response = page["response"]
            traversed.append(len(response["result"]["tools"]))
        return httpx.Response(200, json=response | {"id": payload["id"]})

    descriptors = mcp_client(transport).list_tools(request_context())
    assert len(descriptors) == recorded["total"] == recorded["unique"] == 205
    assert len({item["name"] for item in descriptors}) == 205
    assert traversed == [30, 30, 30, 30, 30, 30, 25]
    assert len(calls) == 9
    assert next(pages, None) is None
    assert "nextCursor" not in recorded["pages"][-1]["response"]["result"]

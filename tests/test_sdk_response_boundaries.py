"""显式兼容模型下，真实 SDK 请求与解析链路的独立边界负例。"""

import json

import pytest
from botocore.awsrequest import AWSResponse
from test_sdk_response_adapter import (
    EVENT,
    GATEWAY,
    TARGET,
    _client_with_responses,
    get_target,
    install,
    server,
    target,
)

from semantic_service.catalog import CatalogError
from semantic_service.sdk import _BINDING


def test_cross_gateway_request_rejected_before_http(monkeypatch, reviewed_control_plane_session):
    client = _client_with_responses(monkeypatch, [], reviewed_control_plane_session)
    install(client)
    attempted = []

    def observe_send(request, **kwargs):
        attempted.append(request.url)
        pytest.fail("cross-gateway request reached HTTP")

    client.meta.events.register_first(f"before-send.{EVENT}", observe_send)
    with pytest.raises(CatalogError, match="compatibility validation failed"):
        client.get_gateway_target(gatewayIdentifier="different-0123456789", targetId=TARGET)
    assert attempted == []


def test_missing_per_call_binding_rejects_an_otherwise_valid_response(
    monkeypatch, reviewed_control_plane_session
):
    client = _client_with_responses(
        monkeypatch, [(200, target(listingMode="DEFAULT"))], reviewed_control_plane_session
    )
    install(client)

    def remove_binding(context, **kwargs):
        context.pop(_BINDING)

    client.meta.events.register(f"before-call.{EVENT}", remove_binding)
    with pytest.raises(CatalogError, match="compatibility validation failed"):
        get_target(client)


def test_boolean_and_integer_are_not_equal_modeled_json_values(
    monkeypatch, reviewed_control_plane_session
):
    client = _client_with_responses(
        monkeypatch, [(200, target(listingMode=True))], reviewed_control_plane_session
    )

    def alter(parsed, **kwargs):
        server(parsed)["listingMode"] = 1

    client.meta.events.register(f"after-call.{EVENT}", alter)
    install(client)
    with pytest.raises(CatalogError, match="compatibility validation failed"):
        get_target(client)


def test_openapi_target_response_passes_through_without_extensions(
    monkeypatch, reviewed_control_plane_session
):
    payload = target()
    configuration = {
        "mcp": {
            "openApiSchema": {
                "inlinePayload": (
                    '{"openapi":"3.0.0","info":{"title":"fixture","version":"1"},"paths":{}}'
                )
            }
        }
    }
    payload["targetConfiguration"] = configuration
    client = _client_with_responses(monkeypatch, [(200, payload)], reviewed_control_plane_session)
    install(client)
    assert get_target(client)["targetConfiguration"] == configuration


def test_list_operation_does_not_receive_get_target_adapter(
    monkeypatch, reviewed_control_plane_session
):
    client = _client_with_responses(monkeypatch, [], reviewed_control_plane_session)
    install(client)
    observed = []
    listing = {
        "items": [{"targetId": TARGET, "name": "fixture", "status": "READY"}],
        "nextToken": "opaque-token",
    }

    class Raw:
        def stream(self, *args, **kwargs):
            yield json.dumps(listing).encode()

    def respond(request, **kwargs):
        assert request.method == "GET"
        assert request.url.endswith(f"/gateways/{GATEWAY.gateway_id}/targets/")
        observed.append(request.url)
        return AWSResponse(request.url, 200, {"content-type": "application/json"}, Raw())

    list_event = "bedrock-agentcore-control.ListGatewayTargets"
    client.meta.events.register(f"before-send.{list_event}", respond)
    result = client.list_gateway_targets(gatewayIdentifier=GATEWAY.gateway_id)
    assert result["items"] == listing["items"]
    assert result["nextToken"] == listing["nextToken"]
    assert len(observed) == 1

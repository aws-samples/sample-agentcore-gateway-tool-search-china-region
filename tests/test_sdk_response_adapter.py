"""合成 HTTP 响应分别验证兼容模型与锁定 SDK 包内模型的实际解析行为。"""

import copy
import json
import time
from types import SimpleNamespace

import pytest
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.parsers import ResponseParserError

from semantic_core.catalog import GatewayIdentity
from semantic_core.providers import RequestContext
from semantic_service import runtime
from semantic_service.catalog import CatalogError, RegisteredTool, TargetRegistry, TargetStateSource

GATEWAY = GatewayIdentity("000000000000", "cn-north-1", "fixture-0123456789")
ARN = "arn:aws-cn:bedrock-agentcore:cn-north-1:000000000000:gateway/fixture-0123456789"
TARGET = "0123456789"
EVENT = "bedrock-agentcore-control.GetGatewayTarget"


def target(**extensions):
    return {
        "gatewayArn": ARN,
        "targetId": TARGET,
        "name": "fixture",
        "status": "READY",
        "createdAt": 1_700_000_000,
        "updatedAt": 1_700_000_000,
        "targetConfiguration": {
            "mcp": {"mcpServer": {"endpoint": "https://upstream.invalid/mcp", **extensions}}
        },
    }


def server(response):
    return response["targetConfiguration"]["mcp"]["mcpServer"]


def _client_with_responses(monkeypatch, responses, sdk_session):
    client = sdk_session.create_client(  # nosec B106
        "bedrock-agentcore-control",
        region_name=GATEWAY.region,
        aws_access_key_id="synthetic-access",
        aws_secret_access_key="synthetic-secret",
        config=Config(retries={"total_max_attempts": 1}),
    )
    pending = iter(responses)

    class Raw:
        def __init__(self, body):
            self.body = body

        def stream(self, *args, **kwargs):
            yield self.body

    def respond(request, **kwargs):
        assert request.method == "GET"
        assert request.url.endswith(f"/gateways/{GATEWAY.gateway_id}/targets/{TARGET}/")
        assert "Authorization" in request.headers  # Real synthetic SigV4 request.
        status, body = next(pending)
        encoded = json.dumps(body).encode() if isinstance(body, dict) else body
        return AWSResponse(request.url, status, {"content-type": "application/json"}, Raw(encoded))

    def forbidden(*args, **kwargs):
        pytest.fail("real AWS HTTP transport must never run")

    monkeypatch.setattr(client._endpoint.http_session, "send", forbidden)
    client.meta.events.register(f"before-send.{EVENT}", respond)
    return client


@pytest.fixture
def client_with_responses(monkeypatch, reviewed_control_plane_session):
    """兼容性用例显式绑定留存模型，不影响独立的包内模型测试。"""
    return lambda responses: _client_with_responses(
        monkeypatch, responses, reviewed_control_plane_session
    )


def get_target(client):
    return client.get_gateway_target(gatewayIdentifier=GATEWAY.gateway_id, targetId=TARGET)


def install(client):
    # Import here so the first red run exercises the existing runtime's actual
    # field loss, rather than failing collection for a not-yet-created module.
    from semantic_service.sdk import install_target_response_adapter

    install_target_response_adapter(client, GATEWAY)


def test_unadapted_reviewed_model_demonstrably_drops_both_fields(client_with_responses):
    client = client_with_responses(
        [(200, target(listingMode="DEFAULT", mcpToolSchema={"inlinePayload": '{"tools":[]}'}))],
    )
    assert server(get_target(client)) == {"endpoint": "https://upstream.invalid/mcp"}


def test_runtime_retains_only_the_two_documented_fields(monkeypatch, client_with_responses):
    static = {"inlinePayload": '{"tools":[]}'}
    client = client_with_responses(
        [(200, target(listingMode="DEFAULT", mcpToolSchema=static, rawSecret="never-copy"))],
    )
    monkeypatch.setattr(
        runtime,
        "_aws_session",
        lambda: SimpleNamespace(create_client=lambda *args, **kwargs: client),
    )
    monkeypatch.setattr(runtime, "_settings", lambda: SimpleNamespace(gateway=GATEWAY))
    runtime._aws.cache_clear()
    try:
        configured = runtime._aws("bedrock-agentcore-control")
        response = get_target(configured)
        assert server(response) == {
            "endpoint": "https://upstream.invalid/mcp",
            "listingMode": "DEFAULT",
            "mcpToolSchema": static,
        }
        assert "never-copy" not in repr(response)
    finally:
        runtime._aws.cache_clear()


def test_later_response_cannot_inherit_previous_mode(client_with_responses):
    client = client_with_responses([(200, target(listingMode="DEFAULT")), (200, target())])
    install(client)
    install(client)  # Registration is idempotent for this client.
    assert server(get_target(client))["listingMode"] == "DEFAULT"
    assert "listingMode" not in server(get_target(client))


@pytest.mark.parametrize("field", ["targetId", "gatewayArn"])
def test_response_must_belong_to_bound_request(client_with_responses, field):
    raw = target(listingMode="DEFAULT")
    raw[field] += "-other"
    client = client_with_responses([(200, raw)])
    install(client)
    with pytest.raises(CatalogError, match="compatibility validation failed"):
        get_target(client)


@pytest.mark.parametrize("field", ["endpoint", "listingMode", "mcpToolSchema"])
def test_already_parsed_conflicting_fields_are_rejected(client_with_responses, field):
    client = client_with_responses(
        [(200, target(listingMode="DEFAULT", mcpToolSchema={"inlinePayload": "{}"}))],
    )

    def alter(parsed, **kwargs):
        server(parsed)[field] = "conflicting-value"

    client.meta.events.register(f"after-call.{EVENT}", alter)
    install(client)
    with pytest.raises(CatalogError, match="compatibility validation failed"):
        get_target(client)


def test_equal_fields_from_a_newer_parser_remain_usable(client_with_responses):
    extensions = {"listingMode": "DEFAULT", "mcpToolSchema": {"inlinePayload": "{}"}}
    client = client_with_responses([(200, target(**extensions))])

    def restore_like_newer_sdk(parsed, **kwargs):
        server(parsed).update(copy.deepcopy(extensions))

    client.meta.events.register(f"after-call.{EVENT}", restore_like_newer_sdk)
    install(client)
    assert server(get_target(client))["mcpToolSchema"] == extensions["mcpToolSchema"]


def test_duplicate_json_keys_fail_closed_without_echoing_response(client_with_responses, capsys):
    raw = json.dumps(target(listingMode="DEFAULT")).replace(
        '"listingMode": "DEFAULT"', '"listingMode": "DYNAMIC", "listingMode": "DEFAULT"'
    )
    client = client_with_responses([(200, raw.encode())])
    install(client)
    with pytest.raises(CatalogError, match="compatibility validation failed") as error:
        get_target(client)
    assert "DYNAMIC" not in str(error.value)
    assert not capsys.readouterr().out


def test_nonfinite_raw_json_is_not_accepted(client_with_responses):
    raw = json.dumps(target(listingMode="DEFAULT", futureField=1)).replace(
        '"futureField": 1', '"futureField": 1e9999'
    )
    client = client_with_responses([(200, raw.encode())])
    install(client)
    with pytest.raises(CatalogError, match="compatibility validation failed"):
        get_target(client)


def test_oversized_compatibility_reparse_fails_closed(client_with_responses):
    from semantic_service.sdk import MAX_TARGET_RESPONSE_BYTES

    raw = json.dumps(target(listingMode="DEFAULT")).encode()
    client = client_with_responses([(200, raw + b" " * MAX_TARGET_RESPONSE_BYTES)])
    install(client)
    with pytest.raises(CatalogError, match="compatibility validation failed"):
        get_target(client)


def test_sdk_error_response_is_not_reinterpreted(client_with_responses):
    client = client_with_responses(
        [(403, {"__type": "AccessDeniedException", "message": "synthetic refusal"})]
    )
    install(client)
    with pytest.raises(ClientError) as error:
        get_target(client)
    assert error.value.response["Error"]["Code"] == "AccessDeniedException"


@pytest.mark.parametrize(
    ("extensions", "allowed"),
    [
        ({"listingMode": "DEFAULT"}, True),
        ({"mcpToolSchema": {"inlinePayload": '{"tools":[]}'}}, True),
        ({"mcpToolSchema": {"s3": {"uri": "s3://fixture-bucket/schema.json"}}}, True),
        ({"listingMode": "DYNAMIC"}, False),
        ({}, False),
        ({"listingMode": "UNRECOGNIZED"}, False),
        ({"listingMode": None}, False),
        ({"listingMode": "DEFAULT", "mcpToolSchema": {}}, False),
    ],
)
def test_real_parser_output_reaches_catalog_admission(client_with_responses, extensions, allowed):
    client = client_with_responses([(200, target(**extensions))])
    install(client)

    class API:
        # The real SDK List/Get request shapes have their own Stubber tests.
        # Here only the listing is stubbed, to exercise the real Get parser
        # through preservation, fingerprinting and target admission together.
        def list_gateway_targets(self, **kwargs):
            assert kwargs == {"gatewayIdentifier": GATEWAY.gateway_id, "maxResults": 100}
            return {"items": [{"targetId": TARGET}]}

        def get_gateway_target(self, **kwargs):
            return client.get_gateway_target(**kwargs)

    source = TargetStateSource(API(), GATEWAY)
    source.fingerprint(RequestContext("sdk-catalog", time.monotonic() + 45))
    registry = TargetRegistry(
        GATEWAY, (RegisteredTool("fixture___lookup", TARGET, "generation-1"),)
    )
    if allowed:
        source.validate_registry(registry)
    else:
        with pytest.raises(CatalogError):
            source.validate_registry(registry)


def test_non_mcp_target_keeps_its_sdk_parsed_configuration(client_with_responses):
    raw = target()
    raw["targetConfiguration"] = {
        "mcp": {
            "lambda": {
                "lambdaArn": "arn:aws-cn:lambda:cn-north-1:000000000000:function:fixture",
                "toolSchema": {"inlinePayload": []},
            }
        }
    }
    client = client_with_responses([(200, raw)])
    install(client)
    assert get_target(client)["targetConfiguration"] == raw["targetConfiguration"]


def test_packaged_sdk_preserves_current_server_fields(monkeypatch, packaged_control_plane_session):
    extensions = {
        "listingMode": "DEFAULT",
        "mcpToolSchema": {"inlinePayload": '{"tools":[]}'},
        "resourcePriority": 2,
    }
    client = _client_with_responses(
        monkeypatch, [(200, target(**extensions))], packaged_control_plane_session
    )
    assert server(get_target(client)) == {
        "endpoint": "https://upstream.invalid/mcp",
        **extensions,
    }


def test_packaged_sdk_rejects_empty_union_before_adapter(
    monkeypatch, packaged_control_plane_session
):
    client = _client_with_responses(
        monkeypatch,
        [(200, target(listingMode="DEFAULT", mcpToolSchema={}))],
        packaged_control_plane_session,
    )
    install(client)
    with pytest.raises(ResponseParserError, match="must have one and only one member set"):
        get_target(client)

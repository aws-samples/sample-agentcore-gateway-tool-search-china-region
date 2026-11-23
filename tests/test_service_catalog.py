"""MCP initialization, complete pagination and stable full-catalog publication."""

import json
import time

import httpx
import pytest
from test_service_storage import DynamoStub, S3Stub, SnapshotStore

from semantic_core.catalog import GatewayIdentity
from semantic_core.providers import RequestContext
from semantic_service.catalog import CatalogError, MCPClient, RegisteredTool, TargetRegistry
from semantic_service.indexer import CatalogIndexer
from semantic_service.secrets import SecretsCache

GW = GatewayIdentity("123456789012", "cn-north-1", "gateway-test")


def descriptor(name):
    return {"name": name, "description": "Find orders", "inputSchema": {"type": "object"}}


class Tokens:
    def token(self, context, *, force_refresh=False):
        return "synthetic-fixture-token"


class Targets:
    def __init__(self, fingerprints=("ready-v1", "ready-v1")):
        self.fingerprints = iter(fingerprints)

    def validate_registry(self, registry):
        pass

    def fingerprint(self, context):
        context.remaining()
        return next(self.fingerprints)


def context():
    return RequestContext("test-request", time.monotonic() + 45)


def client(responses, calls=None):
    queue = iter(responses)
    if calls is None:
        calls = []

    def handle(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert request.headers["Authorization"] == "Bearer synthetic-fixture-token"
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fixture", "version": "1"},
                    },
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": payload["id"], "result": next(queue)}
        )

    return MCPClient(
        httpx.Client(transport=httpx.MockTransport(handle)),
        "https://gateway.invalid/mcp",
        ("gateway.invalid",),
        Tokens(),
    )


def registry():
    return TargetRegistry(
        GW,
        (
            RegisteredTool("orders___find", "target-1", "inc-1"),
            RegisteredTool("toolsearch___search_tools", "target-search", "inc-1", role="discovery"),
            RegisteredTool("orders___search_orders", "target-1", "inc-1"),
        ),
    )


def test_initialize_pagination_exact_exclusions_and_no_substring_filter():
    calls = []
    first = {"tools": [descriptor("orders___find")], "nextCursor": "opaque/cursor="}
    last = {
        "tools": [descriptor("toolsearch___search_tools"), descriptor("orders___search_orders")]
    }
    mcp = client([first, last], calls)
    records = registry().materialize(mcp.list_tools(context()))
    assert [r.name for r in records] == [
        "orders___find",
        "orders___search_orders",
        "toolsearch___search_tools",
    ]
    searchable = registry().searchable(records)
    assert [r.name for r in searchable] == ["orders___find", "orders___search_orders"]
    assert calls[0]["method"] == "initialize"
    assert calls[1]["method"] == "notifications/initialized"
    assert calls[3]["params"] == {"cursor": "opaque/cursor="}


@pytest.mark.parametrize(
    "pages",
    [
        [{"tools": [], "nextCursor": "repeat"}, {"tools": [], "nextCursor": "repeat"}],
        [
            {"tools": [descriptor("orders___find")], "nextCursor": "n"},
            {"tools": [{**descriptor("orders___find"), "description": "changed"}]},
        ],
        [{"tools": [], "nextCursor": ""}],
        [{"tools": "not-an-array"}],
    ],
)
def test_bad_pagination_or_conflicting_names_reject_entire_catalog(pages):
    with pytest.raises(CatalogError):
        client(pages).list_tools(context())


def test_unknown_tool_never_gets_guessed_target_identity():
    with pytest.raises(CatalogError):
        registry().materialize([descriptor("unknown___operation")])


def test_indexer_requires_two_matching_full_reads_and_ready_fingerprints():
    page = {"tools": [descriptor("orders___find")]}
    store = SnapshotStore(
        S3Stub(), DynamoStub(), "bucket-test", "table-test", "table-protected", GW
    )
    store.initialize({"version": 1, "epoch": 1, "principals": [], "tools": {}}, policy_epoch=1)
    first = CatalogIndexer(store, registry(), client([page, page]), Targets())
    assert first.run(context()) == "published"
    active = store.read_control()
    second = CatalogIndexer(store, registry(), client([page, page]), Targets())
    assert second.run(context()) == "reconciled"
    assert store.read_control().catalog_revision == active.catalog_revision
    broken = CatalogIndexer(store, registry(), client([page, {"tools": []}]), Targets())
    with pytest.raises(CatalogError):
        broken.run(context())
    assert store.read_control().catalog_revision == active.catalog_revision


def test_indexer_control_change_and_timeout_keep_previous_version():
    page = {"tools": [descriptor("orders___find")]}
    store = SnapshotStore(
        S3Stub(), DynamoStub(), "bucket-test", "table-test", "table-protected", GW
    )
    initial = store.initialize(
        {"version": 1, "epoch": 1, "principals": [], "tools": {}}, policy_epoch=1
    )
    indexer = CatalogIndexer(store, registry(), client([page, page]), Targets(("a", "b")))
    with pytest.raises(CatalogError):
        indexer.run(context())
    assert store.read_control() == initial
    with pytest.raises(CatalogError):
        indexer.run(RequestContext("expired", time.monotonic() - 1))
    assert store.read_control() == initial


class TargetAPI:
    def __init__(self, target_id="target-1", name="orders"):
        self.target_id, self.name = target_id, name

    def list_gateway_targets(self, **kwargs):
        return {"items": [{"targetId": self.target_id}]}

    def get_gateway_target(self, **kwargs):
        return {
            "targetId": self.target_id,
            "name": self.name,
            "status": "READY",
            "targetConfiguration": {"mcp": {"lambda": {"lambdaArn": "fixture"}}},
        }


def test_target_registration_must_match_observed_id_and_owner_name():
    from semantic_service.catalog import TargetStateSource

    registrations = TargetRegistry(GW, (RegisteredTool("orders___find", "target-1", "inc-1"),))
    recreated = TargetStateSource(TargetAPI("replacement-target", "orders"), GW)
    recreated.fingerprint(context())
    with pytest.raises(CatalogError):
        recreated.validate_registry(registrations)
    renamed = TargetStateSource(TargetAPI("target-1", "billing"), GW)
    renamed.fingerprint(context())
    with pytest.raises(CatalogError):
        renamed.validate_registry(registrations)
    matching = TargetStateSource(TargetAPI(), GW)
    matching.fingerprint(context())
    matching.validate_registry(registrations)


class SecretCacheFixture(SecretsCache):
    def __init__(self):
        from types import SimpleNamespace

        from botocore.config import Config

        self.calls = []
        self.meta = SimpleNamespace(
            config=Config(
                connect_timeout=1,
                read_timeout=3,
                retries={"total_max_attempts": 1, "mode": "standard"},
            )
        )
        super().__init__(self)

    def get(self, secret_id, *, stage="AWSCURRENT", force_refresh=False):
        self.calls.append((secret_id, force_refresh))
        return super().get(secret_id, stage=stage, force_refresh=force_refresh)

    def get_secret_value(self, **kwargs):
        return {
            "VersionId": "synthetic-version",
            "SecretString": json.dumps(
                {"client_id": "client-fixture", "client_secret": "secret-fixture"}
            ),
        }


def test_machine_oauth_cache_and_one_rotation_retry():
    from semantic_service.catalog import OAuthTokenSource

    cache = SecretCacheFixture()
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(401)
        return httpx.Response(
            200, json={"access_token": "synthetic-token", "token_type": "Bearer", "expires_in": 120}
        )

    tokens = OAuthTokenSource(
        cache,
        "machine-secret",
        "https://idp.invalid/token",
        ("idp.invalid",),
        ("catalog.read",),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert tokens.token(context()) == "synthetic-token"
    assert tokens.token(context()) == "synthetic-token"
    assert len(calls) == 2
    assert cache.calls == [("machine-secret", False), ("machine-secret", True)]
    assert b"grant_type=client_credentials" in calls[-1].content


def test_machine_oauth_rejects_large_stream_before_consuming_remainder():
    from semantic_service.catalog import OAuthTokenSource

    reads = []

    class LargeStream(httpx.SyncByteStream):
        def __iter__(self):
            reads.append(1)
            yield b"x" * (65536 + 1)
            reads.append(2)
            yield b"x" * (65536 + 1)

    tokens = OAuthTokenSource(
        SecretCacheFixture(),
        "machine-secret",
        "https://idp.invalid/token",
        ("idp.invalid",),
        ("catalog.read",),
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=LargeStream()))
        ),
    )
    with pytest.raises(CatalogError):
        tokens.token(context())
    assert reads == [1]


def test_mcp_boolean_id_does_not_match_numeric_rpc_id():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": True,
                "result": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}},
            },
        )

    mcp = MCPClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "https://gateway.invalid/mcp",
        ("gateway.invalid",),
        Tokens(),
    )
    with pytest.raises(CatalogError):
        mcp.initialize(context())


def test_all_tools_from_registered_discovery_target_are_excluded():
    registrations = TargetRegistry(
        GW,
        (
            RegisteredTool("discovery___search_tools", "discovery-target", "inc-1", "discovery"),
            RegisteredTool("discovery___inspect", "discovery-target", "inc-1", "business"),
            RegisteredTool("orders___search_orders", "target-1", "inc-1", "business"),
        ),
    )
    tools = registrations.materialize([descriptor(item.name) for item in registrations.tools])
    assert [tool.name for tool in registrations.searchable(tools)] == ["orders___search_orders"]


def test_deleted_target_absent_from_catalog_can_publish_empty_snapshot():
    from semantic_service.catalog import TargetStateSource

    class DeletedTargets:
        def list_gateway_targets(self, **kwargs):
            return {"items": []}

        def get_gateway_target(self, **kwargs):
            raise AssertionError("no deleted target should be read")

    store = SnapshotStore(
        S3Stub(), DynamoStub(), "bucket-test", "table-test", "table-protected", GW
    )
    store.initialize({"version": 1, "epoch": 1, "principals": [], "tools": {}}, policy_epoch=1)
    indexer = CatalogIndexer(
        store,
        registry(),
        client([{"tools": []}, {"tools": []}]),
        TargetStateSource(DeletedTargets(), GW),
    )
    assert indexer.run(context()) == "published"
    assert store.load_snapshot(store.read_control()).tools == ()


def test_mcp_supports_sse_results_and_negotiated_session_headers():
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(request)
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        if payload["method"] == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}
        else:
            assert request.headers["Mcp-Session-Id"] == "session-fixture"
            assert request.headers["MCP-Protocol-Version"] == "2025-03-26"
            result = {"tools": [descriptor("orders___find")]}
        body = (
            "event: message\r\ndata: "
            + json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result})
            + "\r\n\r\n"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "Mcp-Session-Id": "session-fixture"},
            text=body,
        )

    mcp = MCPClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "https://gateway.invalid/mcp",
        ("gateway.invalid",),
        Tokens(),
    )
    assert mcp.list_tools(context()) == [descriptor("orders___find")]
    assert len(calls) == 3


def test_endpoint_only_mcp_target_is_rejected_when_listing_mode_is_unobservable():
    from semantic_service.catalog import TargetStateSource

    class EndpointOnlyTarget(TargetAPI):
        def get_gateway_target(self, **kwargs):
            target = super().get_gateway_target(**kwargs)
            target["targetConfiguration"] = {
                "mcp": {"mcpServer": {"endpoint": "https://upstream.invalid/mcp"}}
            }
            return target

    source = TargetStateSource(EndpointOnlyTarget(), GW)
    source.fingerprint(context())
    registrations = TargetRegistry(GW, (RegisteredTool("orders___find", "target-1", "inc-1"),))
    with pytest.raises(CatalogError):
        source.validate_registry(registrations)


class ModeTarget(TargetAPI):
    def __init__(self, fields):
        super().__init__()
        self.fields = fields

    def get_gateway_target(self, **kwargs):
        target = super().get_gateway_target(**kwargs)
        target["targetConfiguration"] = {
            "mcp": {"mcpServer": {"endpoint": "https://upstream.invalid/mcp", **self.fields}}
        }
        return target


def validate_mode_target(fields):
    from semantic_service.catalog import TargetStateSource

    source = TargetStateSource(ModeTarget(fields), GW)
    source.fingerprint(context())
    registrations = TargetRegistry(GW, (RegisteredTool("orders___find", "target-1", "inc-1"),))
    source.validate_registry(registrations)
    return source


def test_explicit_default_mcp_listing_mode_is_indexable():
    validate_mode_target({"listingMode": "DEFAULT"})


@pytest.mark.parametrize("value", ["DYNAMIC", "UNKNOWN", "", None, False, 1, [], {}])
def test_dynamic_unknown_or_malformed_listing_modes_are_rejected(value):
    with pytest.raises(CatalogError):
        validate_mode_target({"listingMode": value})


def test_listing_mode_changes_are_included_in_target_fingerprint():
    from semantic_service.catalog import TargetStateSource

    api = ModeTarget({"listingMode": "DEFAULT"})
    source = TargetStateSource(api, GW)
    before = source.fingerprint(context())
    api.fields = {"listingMode": "DYNAMIC"}
    assert source.fingerprint(context()) != before


@pytest.mark.parametrize(
    "schema",
    [
        {"inlinePayload": '{"tools": []}'},
        {"inlinePayload": '[{"name":"find"}]'},
        {"s3": {"uri": "s3://fixture-bucket/tool-schema.json"}},
        {
            "s3": {
                "uri": "s3://fixture-bucket/tool-schema.json",
                "bucketOwnerAccountId": "123456789012",
            }
        },
    ],
)
def test_explicit_static_mcp_schema_is_indexable_without_inferred_listing_mode(schema):
    validate_mode_target({"mcpToolSchema": schema})
    validate_mode_target({"listingMode": "DEFAULT", "mcpToolSchema": schema})


@pytest.mark.parametrize(
    "schema",
    [
        None,
        False,
        [],
        {},
        {"inlinePayload": []},
        {"inlinePayload": ""},
        {"inlinePayload": "  "},
        {"inlinePayload": "{invalid-json"},
        {"inlinePayload": '{"tools":[],"tools":[]}'},
        {"inlinePayload": '{"value":NaN}'},
        {"inlinePayload": '{"value":1e1000}'},
        {"inlinePayload": "{}", "s3": {"uri": "s3://fixture-bucket/schema"}},
        {"unknownMember": "{}"},
        {"s3": {}},
        {"s3": "s3://fixture-bucket/schema"},
        {"s3": {"uri": ""}},
        {"s3": {"uri": "https://fixture.invalid/schema"}},
        {"s3": {"uri": "s3://"}},
        {"s3": {"uri": "s3://" + "x" * 2044}},
        {"s3": {"uri": "s3://fixture-bucket/schema", "bucketOwnerAccountId": "123"}},
        {"s3": {"uri": "s3://fixture-bucket/schema", "bucketOwnerAccountId": 123456789012}},
        {"s3": {"uri": "s3://fixture-bucket/schema", "unexpected": "field"}},
    ],
)
def test_malformed_static_union_is_rejected_even_with_default_mode(schema):
    for fields in ({"mcpToolSchema": schema}, {"listingMode": "DEFAULT", "mcpToolSchema": schema}):
        with pytest.raises(CatalogError):
            validate_mode_target(fields)


@pytest.mark.parametrize("mode", ["DYNAMIC", "UNKNOWN", None, False, 1])
def test_static_schema_never_overrides_dynamic_unknown_or_malformed_mode(mode):
    with pytest.raises(CatalogError):
        validate_mode_target(
            {"listingMode": mode, "mcpToolSchema": {"inlinePayload": '{"tools":[]}'}}
        )


def test_static_schema_changes_are_included_in_target_fingerprint():
    from semantic_service.catalog import TargetStateSource

    api = ModeTarget({"mcpToolSchema": {"s3": {"uri": "s3://fixture-bucket/schema-v1"}}})
    source = TargetStateSource(api, GW)
    before = source.fingerprint(context())
    api.fields = {"mcpToolSchema": {"s3": {"uri": "s3://fixture-bucket/schema-v2"}}}
    assert source.fingerprint(context()) != before

"""Runtime wiring is lazy, namespace-bound, and respects scheduler deadlines."""

import json
from types import SimpleNamespace

import httpx
import pytest

from semantic_service import runtime
from semantic_service.auth import ServiceError


def context(remaining=300_000):
    return SimpleNamespace(
        aws_request_id="lambda-fixture",
        get_remaining_time_in_millis=lambda: remaining,
    )


def test_indexer_runtime_discards_expired_tick_without_building_clients(monkeypatch):
    monkeypatch.setattr(runtime, "_indexer", lambda: pytest.fail("must not build stale work"))
    assert runtime.indexer_handler({"time": "2000-01-01T00:00:00Z"}, context()) == {
        "status": "expired"
    }


def test_indexer_runtime_uses_one_bounded_invocation(monkeypatch, capsys):
    calls = []

    class Indexer:
        def run(self, request):
            calls.append(request)
            assert 0 < request.remaining() <= 45
            return "published"

    monkeypatch.setattr(runtime, "_indexer", lambda: Indexer())
    assert runtime.indexer_handler({}, context()) == {"status": "published"}
    assert len(calls) == 1
    assert "published" in capsys.readouterr().out


def test_runtime_reserves_time_for_lambda_return(monkeypatch):
    class Indexer:
        def run(self, request):
            assert 0 < request.remaining() <= 0.5
            return "reconciled"

    monkeypatch.setattr(runtime, "_indexer", lambda: Indexer())
    assert runtime.indexer_handler({}, context(remaining=1000)) == {"status": "reconciled"}


def test_background_failure_does_not_echo_exception_body(monkeypatch, capsys):
    def broken():
        raise RuntimeError("sensitive-credential-fixture")

    monkeypatch.setattr(runtime, "_indexer", broken)
    with pytest.raises(ServiceError, match="internal failure"):
        runtime.indexer_handler({}, context())
    assert "sensitive-credential-fixture" not in capsys.readouterr().out


def test_search_wiring_preserves_the_public_business_object(monkeypatch):
    expected = {"tools": []}
    monkeypatch.setattr(runtime, "_search", lambda: lambda event, ctx, **kwargs: expected)
    assert runtime.search_handler({"query": "fixture"}, context()) is expected


def test_disabled_enhancement_does_not_require_provider_or_secret(monkeypatch):
    monkeypatch.setenv("SEMSEARCH_MODELS_ENABLED", "false")
    monkeypatch.setattr(runtime, "_enhancer", lambda: pytest.fail("must remain lazy"))
    assert runtime.enhancer_handler({}, context()) == {"status": "disabled"}


def test_registry_incarnation_must_match_search_runtime(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "_settings",
        lambda: SimpleNamespace(
            search_tool_name="discovery___search_tools",
            search_target_id="target-1",
            gateway=None,
        ),
    )
    monkeypatch.setenv("SEMSEARCH_SEARCH_INCARNATION", "current")
    monkeypatch.setenv(
        "SEMSEARCH_REGISTRY_JSON",
        json.dumps(
            [
                {
                    "name": "discovery___search_tools",
                    "target_id": "target-1",
                    "incarnation": "stale",
                    "role": "discovery",
                }
            ]
        ),
    )
    runtime._registry.cache_clear()
    try:
        with pytest.raises(ValueError, match="incarnation"):
            runtime._registry()
    finally:
        runtime._registry.cache_clear()


def test_cold_wiring_does_not_fetch_credentials_or_contact_endpoints(monkeypatch):
    descriptor = {
        "name": "discovery___search_tools",
        "description": "Discover authorized tools",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 2048},
                "_auth_context": {"type": "string", "maxLength": 8192},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }
    values = {
        "SEMSEARCH_ACCOUNT": "000000000000",
        "AWS_REGION": "cn-north-1",
        "SEMSEARCH_GATEWAY_ID": "gateway-fixture",
        "SEMSEARCH_SEARCH_TARGET_ID": "target-fixture",
        "SEMSEARCH_SEARCH_TOOL_NAME": descriptor["name"],
        "SEMSEARCH_SEARCH_INCARNATION": "generation-1",
        "SEMSEARCH_SEARCH_DESCRIPTOR_JSON": json.dumps(descriptor),
        "SEMSEARCH_BUCKET": "snapshots-fixture",
        "SEMSEARCH_TABLE": "state-fixture",
        "SEMSEARCH_PROTECTED_TABLE": "protected-fixture",
        "SEMSEARCH_GATEWAY_URL": "https://gateway.example.invalid/mcp",
        "SEMSEARCH_GATEWAY_ALLOWED_HOSTS": "gateway.example.invalid",
        "SEMSEARCH_MODELS_ENABLED": "true",
        "SEMSEARCH_MACHINE_PRINCIPALS_JSON": '[["https://idp.example.invalid","indexer"]]',
        "SEMSEARCH_REGISTRY_JSON": json.dumps(
            [
                {
                    "name": descriptor["name"],
                    "target_id": "target-fixture",
                    "incarnation": "generation-1",
                    "role": "discovery",
                }
            ]
        ),
        "OIDC_ISSUER": "https://idp.example.invalid",
        "OIDC_AUDIENCE": "gateway-audience",
        "OIDC_DISCOVERY_URI": "https://idp.example.invalid/.well-known/openid-configuration",
        "OIDC_JWKS_URI": "https://idp.example.invalid/keys",
        "OIDC_TOKEN_ENDPOINT": "https://idp.example.invalid/token",
        "OIDC_ALLOWED_HOSTS": "idp.example.invalid",
        "OIDC_MACHINE_SCOPES": "catalog.read",
        "MODEL_ALLOWED_HOSTS": "models.example.invalid",
        "MODEL_EMBEDDING_DIMENSIONS": "4",
    }
    factories = [
        runtime._settings,
        runtime._store,
        runtime._secrets,
        runtime._envelope,
        runtime._search_tool,
        runtime._models,
        runtime._search,
        runtime._request,
        runtime._registry,
        runtime._indexer,
        runtime._enhancer,
    ]
    for factory in factories:
        factory.cache_clear()
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    class NoAWS:
        def __getattr__(self, name):
            pytest.fail(f"cold wiring unexpectedly accessed AWS: {name}")

    monkeypatch.setattr(runtime, "_aws", lambda service: NoAWS())
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected network")),
        trust_env=False,
    )
    monkeypatch.setattr(runtime, "_http", lambda: client)
    try:
        search = runtime._search()
        interceptor = runtime._request()
        indexer = runtime._indexer()
        enhancer = runtime._enhancer()
        assert search.search_tool.identity == interceptor.config.search_tool.identity
        assert indexer.registry.tools[0].incarnation == search.search_tool.identity.incarnation
        assert indexer.store.table == "state-fixture"
        assert indexer.store.protected_table == "protected-fixture"
        assert interceptor.limiter.table_name == "state-fixture"
        assert indexer.mcp.protocol_versions == ("2025-03-26",)
        assert enhancer.embedding.describe() == search.embedding.describe()
        assert search.reranker is None
    finally:
        client.close()
        for factory in factories:
            factory.cache_clear()

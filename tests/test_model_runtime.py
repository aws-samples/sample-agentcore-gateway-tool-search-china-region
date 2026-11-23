"""Secrets Manager ownership, rotation and provider integration stay offline."""

import json
import time

import httpx
import pytest

from semantic_core.providers import (
    ErrorCode,
    ProviderError,
    ProviderFailure,
    RequestContext,
    RerankDocument,
)
from semantic_service.models import ModelSettings, RotatingModelProviders
from semantic_service.secrets import SecretError, SecretsCache


class FakeSecrets:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    def get_secret_value(self, **kwargs):
        self.calls.append(kwargs)
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


def secret(version="v1", key="synthetic-key", base="https://models.invalid/v1"):
    return {"VersionId": version, "SecretString": json.dumps({"base_url": base, "api_key": key})}


def settings(**values):
    return ModelSettings.from_env(
        {"MODEL_ALLOWED_HOSTS": "models.invalid", "MODEL_EMBEDDING_DIMENSIONS": "2", **values}
    )


def ctx():
    return RequestContext("runtime-fixture", time.monotonic() + 60)


def test_secret_cache_ttl_stages_and_repr_are_safe():
    now = [1.0]
    sdk = FakeSecrets([secret(), secret("v2"), secret("previous")])
    cache = SecretsCache(sdk, ttl_seconds=300, clock=lambda: now[0])
    first = cache.get("semsearch/model-api")
    assert first.value["api_key"] == "synthetic-key"
    assert "synthetic-key" not in repr(first)
    now[0] = 300.9
    assert cache.get("semsearch/model-api") is first
    now[0] = 301.0
    assert cache.get("semsearch/model-api").version_id == "v2"
    assert cache.get("signing", stage="AWSPREVIOUS").version_id == "previous"
    assert sdk.calls == [
        {"SecretId": "semsearch/model-api", "VersionStage": "AWSCURRENT"},
        {"SecretId": "semsearch/model-api", "VersionStage": "AWSCURRENT"},
        {"SecretId": "signing", "VersionStage": "AWSPREVIOUS"},
    ]
    with pytest.raises(ValueError):
        SecretsCache(sdk, ttl_seconds=301)


@pytest.mark.parametrize(
    "response",
    [
        {"SecretString": "{}"},
        {"VersionId": "v1", "SecretString": "[]"},
        RuntimeError("synthetic-key"),
    ],
)
def test_invalid_secret_or_sdk_failure_has_fixed_error(response):
    with pytest.raises(SecretError) as error:
        SecretsCache(FakeSecrets([response])).get("configured")
    assert "synthetic-key" not in str(error.value)
    assert error.value.__cause__ is None


def test_env_never_supplies_credential_and_rerank_remains_disabled_by_default():
    config = settings(MODEL_API_KEY="ignored", OPENAI_API_KEY="ignored")
    assert config.secret_id == "semsearch/model-api"  # nosec B105
    assert config.embedding.profile.model_id == "text-embedding-v4"
    assert config.chat.profile.model_id == "qwen-plus"
    assert config.rerank is None


@pytest.mark.parametrize(
    "override",
    [
        {"MODEL_RERANK_PATH": ""},
        {"MODEL_RERANK_ENVELOPE": "unknown"},
        {"MODEL_RERANK_SCORE_FIELD": "unknown"},
    ],
)
def test_invalid_explicit_rerank_profile_does_not_fall_back_to_defaults(override):
    with pytest.raises(ValueError, match="valid path, envelope and score field"):
        settings(MODEL_RERANK_ENABLED="true", **override)


def test_rotation_refreshes_once_and_preserves_request_deadline():
    sdk = FakeSecrets([secret(key="old-synthetic"), secret("v2", "new-synthetic")])
    requests = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handle(request):
        requests.append(request)
        assert str(request.url) == "https://models.invalid/v1/embeddings"
        if len(requests) == 1:
            return httpx.Response(401)
        assert request.headers["Authorization"] == "Bearer new-synthetic"
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [3, 4]}]})

    with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client:
        providers = RotatingModelProviders(SecretsCache(sdk), settings(), client)
        assert providers.embedding.embed(["query"], "query", ctx()).vectors.shape == (1, 2)
        assert providers.embedding.describe() == settings().embedding.profile
    assert len(sdk.calls) == 2
    assert requests[1].extensions["timeout"]["read"] <= requests[0].extensions["timeout"]["read"]


def test_persistent_auth_failure_is_bounded_and_secret_failure_is_provider_unavailable():
    sdk = FakeSecrets([secret(), secret("v2")])
    requests = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handle(request):
        requests.append(request)
        return httpx.Response(401)

    with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client:
        providers = RotatingModelProviders(SecretsCache(sdk), settings(), client)
        with pytest.raises(ProviderError) as error:
            providers.embedding.embed(["query"], "query", ctx())
        assert error.value.code is ErrorCode.AUTHENTICATION
        broken = RotatingModelProviders(
            SecretsCache(FakeSecrets([RuntimeError("secret")])), settings(), client
        )
        with pytest.raises(ProviderError) as unavailable:
            broken.embedding.embed(["query"], "query", ctx())
        assert unavailable.value.code is ErrorCode.UNAVAILABLE
    assert len(requests) == 2


def test_chat_has_independent_budget_json_object_and_local_validation():
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handle(request):
        assert str(request.url) == "https://models.invalid/v1/chat/completions"
        assert request.extensions["timeout"]["read"] > 0.45
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert "enable_thinking" not in body
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": '{"queries":["find orders"]}'}}
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client:
        providers = RotatingModelProviders(
            SecretsCache(FakeSecrets([secret()])), settings(), client
        )
        assert providers.llm.generate_queries("orders", ("en",), 1, ctx()).queries == (
            "find orders",
        )


def test_secret_url_still_requires_independent_exact_allowlist():
    sdk = FakeSecrets([secret(base="https://unexpected.invalid/v1")])
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("sent"))) as client:
        providers = RotatingModelProviders(SecretsCache(sdk), settings(), client)
        with pytest.raises(ProviderError) as error:
            providers.embedding.embed(["query"], "query", ctx())
        assert error.value.code is ErrorCode.INVALID_REQUEST


def test_rotation_retry_uses_original_deadline_after_elapsed_first_request(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("semantic_core.providers.time.monotonic", lambda: clock[0])
    requests = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            clock[0] += 0.3
            return httpx.Response(401)
        assert request.extensions["timeout"]["read"] == pytest.approx(0.1)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [3, 4]}]})

    sdk = FakeSecrets([secret(), secret("v2")])
    with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client:
        providers = RotatingModelProviders(SecretsCache(sdk), settings(), client)
        providers.embedding.embed(["query"], "query", RequestContext("deadline", 100.4))
    assert len(requests) == 2


def test_continuing_auth_failures_have_refresh_cooldown():
    sdk = FakeSecrets([secret(), secret("v2")])
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(401)), trust_env=False
    ) as client:
        providers = RotatingModelProviders(SecretsCache(sdk), settings(), client)
        for _ in range(2):
            with pytest.raises(ProviderError):
                providers.embedding.embed(["query"], "query", ctx())
    assert len(sdk.calls) == 2


def test_rotated_endpoint_cannot_silently_change_an_existing_vector_space():
    sdk = FakeSecrets([secret(), secret("v2", base="https://models.invalid/new-v1")])
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(401)), trust_env=False
    ) as client:
        providers = RotatingModelProviders(SecretsCache(sdk), settings(), client)
        with pytest.raises(ProviderError) as error:
            providers.embedding.embed(["query"], "query", ctx())
    assert error.value.code is ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("refresh_failure", ["secret", "endpoint"])
@pytest.mark.parametrize("operation", ["query_embedding", "document_embedding", "chat", "rerank"])
def test_rotation_setup_failure_keeps_http_diagnostics_only_for_the_current_call(
    status, refresh_failure, operation
):
    sdk = FakeSecrets(
        [secret(), RuntimeError("synthetic-key"), RuntimeError("synthetic-key")]
        if refresh_failure == "secret"
        else [secret(), secret("v2", base="https://models.invalid/new-v1")]
    )
    requests = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={"Retry-After": "11"})

    def invoke(providers):
        if operation in ("query_embedding", "document_embedding"):
            return providers.embedding.embed(
                ["query"], "query" if operation == "query_embedding" else "document", ctx()
            )
        if operation == "chat":
            return providers.llm.generate_queries("orders", ("en",), 1, ctx())
        return providers.rerank.rerank("orders", [RerankDocument("a", "orders")], 1, ctx())

    config = settings(MODEL_RERANK_ENABLED="true")
    with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client:
        providers = RotatingModelProviders(SecretsCache(sdk), config, client)
        with pytest.raises(ProviderError) as first:
            invoke(providers)
        with pytest.raises(ProviderError) as second:
            invoke(providers)
    expected_code = (
        ErrorCode.UNAVAILABLE if refresh_failure == "secret" else ErrorCode.INVALID_REQUEST
    )
    assert first.value.code is second.value.code is expected_code
    assert first.value.failure.kind == "http_client_error"
    assert first.value.failure.http_status == status
    # HTTP 采集策略只接受 429/503 的 Retry-After。
    assert first.value.failure.retry_after_seconds is None
    assert second.value.failure == ProviderFailure(expected_code.value)
    assert len(requests) == 1
    assert len(sdk.calls) == (3 if refresh_failure == "secret" else 2)
    assert first.value.usage == second.value.usage == {}


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("refresh_failure", ["secret", "endpoint"])
def test_rotation_wrapper_preserves_all_already_collected_failure_metadata(status, refresh_failure):
    sdk = FakeSecrets(
        [secret(), RuntimeError("synthetic-key")]
        if refresh_failure == "secret"
        else [secret(), secret("v2", base="https://models.invalid/new-v1")]
    )
    # 单独验证包装器契约：自定义 provider 已收集的受控元数据必须原样传递。
    observed = ProviderFailure("http_client_error", http_status=status, retry_after_seconds=11)
    attempts = []

    def authenticate(transport, key):
        attempts.append(transport)
        raise ProviderError(ErrorCode.AUTHENTICATION, failure=observed)

    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("sent"))) as client:
        providers = RotatingModelProviders(SecretsCache(sdk), settings(), client)
        with pytest.raises(ProviderError) as caught:
            providers._invoke(ctx(), 0.45, authenticate)
    assert caught.value.failure is observed
    assert caught.value.failure.retry_after_seconds == 11
    assert len(attempts) == 1
    assert len(sdk.calls) == 2


def test_aws_stubber_confirms_only_explicit_get_secret_value():
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(  # nosec B106
        "secretsmanager",
        region_name="cn-north-1",
        aws_access_key_id="synthetic-access",
        aws_secret_access_key="synthetic-secret",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "get_secret_value",
            secret("a" * 32),
            {"SecretId": "semsearch/model-api", "VersionStage": "AWSCURRENT"},
        )
        cache = SecretsCache(client)
        assert cache.get("semsearch/model-api").version_id == "a" * 32
        assert cache.get("semsearch/model-api").value["api_key"] == "synthetic-key"
        stubber.assert_no_pending_responses()


@pytest.mark.parametrize("failure_kind", ["embedding", "rerank"])
def test_runtime_provider_failures_degrade_without_authorization_leak(failure_kind):
    from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord
    from semantic_core.search import SearchConfig, SearchEngine
    from semantic_core.snapshot import IndexSnapshot
    from semantic_core.vectors import VectorIndex

    config = settings(
        MODEL_RERANK_ENABLED="true",
        MODEL_RERANK_PATH="/explicit-rerank",
        MODEL_RERANK_ENVELOPE="flat",
        MODEL_RERANK_SCORE_FIELD="score",
    )
    gateway = GatewayIdentity("000000000000", "cn-north-1", "test-gateway")
    tools = tuple(
        ToolRecord.from_descriptor(
            ToolIdentity(gateway, "test-target", "v1", name),
            {"name": name, "description": description, "inputSchema": {"type": "object"}},
        )
        for name, description in [
            ("orders___cancel", "cancel order"),
            ("private___tool", "private hidden tool"),
        ]
    )
    snapshot = IndexSnapshot.build(
        gateway,
        tools,
        vectors=VectorIndex([[1, 0], [0, 1]], [0, 1], 2, 2),
        model_profile=config.embedding.profile,
    )
    calls = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handle(request):
        calls.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        if request.url.path == "/v1/embeddings":
            if failure_kind == "embedding":
                return httpx.Response(503)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})
        assert request.url.path == "/v1/explicit-rerank"
        assert "private hidden tool" not in request.content.decode()
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client:
        providers = RotatingModelProviders(SecretsCache(FakeSecrets([secret()])), config, client)
        result = SearchEngine(
            snapshot, providers.embedding, providers.rerank, SearchConfig(rerank_enabled=True)
        ).search("cancel order", frozenset({tools[0].tool_id}), ctx())
    assert [hit.tool.name for hit in result.hits] == ["orders___cancel"]
    assert result.mode == ("bm25" if failure_kind == "embedding" else "rrf")
    assert len(calls) == (1 if failure_kind == "embedding" else 2)


def test_runtime_stage_budgets_match_design_defaults():
    config = settings()
    assert config.embedding_timeout_seconds == 1.0
    assert config.rerank_timeout_seconds == 0.45
    assert config.chat_timeout_seconds == 30
    assert config.document_timeout_seconds == 30

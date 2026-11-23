"""Portable model contracts exercised only through injected MockTransport."""

import json
import time
from dataclasses import FrozenInstanceError

import httpx
import numpy as np
import pytest

from semantic_core.openai_compatible import (
    ChatConfig,
    EmbeddingConfig,
    OpenAIChatProvider,
    OpenAICompatibleConfig,
    OpenAIEmbeddingProvider,
    bailian_chat_config,
    bailian_embedding_config,
)
from semantic_core.providers import ErrorCode, ModelProfile, ProviderError, RequestContext

EMBEDDING_PROFILE = ModelProfile("portable-embedding", "revision-1", 3)
CHAT_PROFILE = ModelProfile("portable-chat", "revision-2")


def transport() -> OpenAICompatibleConfig:
    return OpenAICompatibleConfig("https://offline.example", ("offline.example",))


def embedding_response() -> dict[str, object]:
    return {
        "model": EMBEDDING_PROFILE.model_id,
        "data": [{"index": 0, "embedding": [3, 4, 0]}],
    }


def chat_response(content: object = None) -> dict[str, object]:
    return {
        "model": CHAT_PROFILE.model_id,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {"queries": ["find a tool"]} if content is None else content
                    )
                },
            }
        ],
    }


def context() -> RequestContext:
    return RequestContext("portable-contract", time.monotonic() + 10)


def test_portable_embedding_route_model_and_dimensions() -> None:
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://offline.example/v1/embeddings"
        assert json.loads(request.content) == {
            "model": "portable-embedding",
            "input": ["query"],
            "dimensions": 3,
            "encoding_format": "float",
        }
        return httpx.Response(200, json=embedding_response())

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        provider = OpenAIEmbeddingProvider(
            client, transport(), EmbeddingConfig(EMBEDDING_PROFILE, "/v1/embeddings", 16, 1024)
        )
        result = provider.embed(["query"], "query", context())
    assert result.profile == EMBEDDING_PROFILE
    np.testing.assert_allclose(result.vectors, [[0.6, 0.8, 0]])


def test_portable_chat_route_and_vendor_neutral_body() -> None:
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://offline.example/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "portable-chat"
        assert "enable_thinking" not in payload
        return httpx.Response(200, json=chat_response())

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        provider = OpenAIChatProvider(
            client, transport(), ChatConfig(CHAT_PROFILE, "/v1/chat/completions", 2048)
        )
        result = provider.generate_queries("tool description", ("en",), 1, context())
    assert result.profile == CHAT_PROFILE
    assert result.queries == ("find a tool",)


def test_embedding_fields_can_be_omitted_and_config_allows_more_than_ten_inputs() -> None:
    texts = ["query"] * 12

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "model": EMBEDDING_PROFILE.model_id,
            "input": texts,
            "task": "retrieval",
        }
        return httpx.Response(
            200,
            json={
                "data": [{"index": index, "embedding": [3, 4, 0]} for index in reversed(range(12))]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        settings = EmbeddingConfig(
            EMBEDDING_PROFILE,
            "/v1/embeddings",
            12,
            32,
            send_dimensions=False,
            encoding_format=None,
            extra_body={"task": "retrieval"},
        )
        result = OpenAIEmbeddingProvider(client, transport(), settings).embed(
            texts, "document", context()
        )
    assert result.vectors.shape == (12, 3)
    np.testing.assert_allclose(result.vectors[0], [0.6, 0.8, 0])


def test_base_prefix_port_and_explicit_auth_are_forwarded_to_shared_transport() -> None:
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://offline.example:8443/tenant/v1/embeddings"
        assert request.headers["x-api-key"] == "fixture-credential"
        return httpx.Response(200, json=embedding_response())

    settings = OpenAICompatibleConfig(
        "https://offline.example:8443/tenant",
        ("offline.example",),
        auth_header_name="x-api-key",
        auth_header_format="{token}",
    )
    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        result = OpenAIEmbeddingProvider(
            client,
            settings,
            EmbeddingConfig(EMBEDDING_PROFILE, "/v1/embeddings", 1, 128),
            credential="fixture-credential",
        ).embed(["query"], "query", context())
    assert result.profile == EMBEDDING_PROFILE


def test_generic_model_identity_ignores_nonstandard_echo_fields() -> None:
    payload = {
        **embedding_response(),
        "revision": "supplier-internal-value",
        "config_version": "supplier-internal-config",
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)), trust_env=False
    ) as client:
        result = OpenAIEmbeddingProvider(
            client, transport(), EmbeddingConfig(EMBEDDING_PROFILE, "/v1/embeddings", 1, 128)
        ).embed(["query"], "query", context())
    assert result.profile == EMBEDDING_PROFILE


@pytest.mark.parametrize("purpose", ["query", "document"])
def test_embedding_request_limits_are_configured_and_checked_before_io(purpose: str) -> None:
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def never(_: httpx.Request) -> httpx.Response:
        pytest.fail("invalid input must not reach transport")

    settings = EmbeddingConfig(EMBEDDING_PROFILE, "/v1/embeddings", 1, 3)
    with httpx.Client(transport=httpx.MockTransport(never), trust_env=False) as client:
        provider = OpenAIEmbeddingProvider(client, transport(), settings)
        for texts in (["a", "b"], ["four"]):
            with pytest.raises(ProviderError) as error:
                provider.embed(texts, purpose, context())
            assert error.value.code is ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize(
    "data",
    [
        [{"index": 0, "embedding": [0, 0, 0]}],
        [{"index": 0, "embedding": [1, 0]}],
        [{"index": 1, "embedding": [1, 0, 0]}],
        [{"index": 0, "embedding": [1, 0, 0]}] * 2,
    ],
)
def test_configured_embedding_shape_does_not_relax_response_validation(data: object) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": data})),
        trust_env=False,
    ) as client:
        provider = OpenAIEmbeddingProvider(
            client, transport(), EmbeddingConfig(EMBEDDING_PROFILE, "/v1/embeddings", 1, 128)
        )
        with pytest.raises(ProviderError) as error:
            provider.embed(["query"], "query", context())
    assert error.value.code is ErrorCode.INVALID_RESPONSE


def test_chat_optional_json_mode_and_vendor_extra_body() -> None:
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert str(request.url) == "https://offline.example/custom/chat"
        assert "response_format" not in body
        assert "enable_thinking" not in body
        assert body["max_tokens"] == 128
        assert body["temperature"] == 0.5
        assert body["reasoning"] == {"effort": "low"}
        return httpx.Response(200, json=chat_response({"queries": [" query ", "query"]}))

    settings = ChatConfig(
        CHAT_PROFILE,
        "/custom/chat",
        128,
        max_tokens=128,
        temperature=0.5,
        json_mode=False,
        extra_body={"reasoning": {"effort": "low"}},
    )
    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        result = OpenAIChatProvider(client, transport(), settings).generate_queries(
            "tool", ("en",), 2, context()
        )
    assert result.queries == ("query",)


def test_chat_schema_repair_is_bounded_and_never_echoes_invalid_reply() -> None:
    requests: list[httpx.Request] = []
    replies = iter(
        [chat_response({"queries": ["q"], "extra": "invalid-output-marker"}), chat_response()]
    )

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=next(replies))

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        result = OpenAIChatProvider(
            client, transport(), ChatConfig(CHAT_PROFILE, "/v1/chat/completions", 128)
        ).generate_queries("tool", ("en",), 1, context())
    assert result.queries == ("find a tool",)
    assert result.prompt_version == "tool-queries-v2"
    assert len(requests) == 2
    assert requests[0].content != requests[1].content
    assert b"invalid-output-marker" not in requests[1].content


@pytest.mark.parametrize("failure", ["model", "usage", "schema"])
def test_chat_does_not_retry_identity_or_usage_but_limits_schema_to_two_calls(failure: str) -> None:
    requests: list[httpx.Request] = []
    payload = (
        chat_response({"queries": ["q"], "extra": True}) if failure == "schema" else chat_response()
    )
    if failure == "model":
        payload["model"] = "unapproved-model"
    if failure == "usage":
        payload["usage"] = {"total_tokens": -1}

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        with pytest.raises(ProviderError) as error:
            OpenAIChatProvider(
                client, transport(), ChatConfig(CHAT_PROFILE, "/v1/chat/completions", 128)
            ).generate_queries("tool", ("en",), 1, context())
    assert error.value.code is ErrorCode.INVALID_RESPONSE
    assert len(requests) == (2 if failure == "schema" else 1)


@pytest.mark.parametrize(
    "field",
    [
        "model",
        "input",
        "dimensions",
        "messages",
        "tools",
        "stream",
        "response_format",
        "temperature",
    ],
)
def test_extra_body_cannot_override_controlled_contract_fields(field: str) -> None:
    with pytest.raises(ValueError):
        EmbeddingConfig(EMBEDDING_PROFILE, "/v1/embeddings", 1, 128, extra_body={field: "override"})
    with pytest.raises(ValueError):
        ChatConfig(CHAT_PROFILE, "/v1/chat/completions", 128, extra_body={field: "override"})


def test_extra_body_is_deeply_frozen_and_source_mutation_does_not_change_requests() -> None:
    nested = {"extension": {"values": [1, 2]}}
    embedding = EmbeddingConfig(EMBEDDING_PROFILE, "/v1/embeddings", 1, 128, extra_body=nested)
    chat = ChatConfig(CHAT_PROFILE, "/v1/chat/completions", 128, extra_body=nested)
    nested["extension"]["values"].append(3)
    assert embedding.extra_body["extension"]["values"] == [1, 2]
    assert chat.extra_body["extension"]["values"] == [1, 2]
    embedding.extra_body["extension"]["values"][0] = 99
    assert embedding.extra_body["extension"]["values"] == [1, 2]
    with pytest.raises(FrozenInstanceError):
        embedding.max_batch = 100


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_positive_limits_fail_at_configuration_time(limit: object) -> None:
    with pytest.raises(ValueError):
        EmbeddingConfig(EMBEDDING_PROFILE, "/v1/embeddings", limit, 128)
    with pytest.raises(ValueError):
        ChatConfig(CHAT_PROFILE, "/v1/chat/completions", limit)


@pytest.mark.parametrize(
    "path", ["https://evil.example/path", "//evil.example/path", "/../escape", "/v1?query=1"]
)
def test_invalid_paths_fail_before_provider_creation(path: str) -> None:
    with pytest.raises(ValueError):
        EmbeddingConfig(EMBEDDING_PROFILE, path, 1, 128)
    with pytest.raises(ValueError):
        ChatConfig(CHAT_PROFILE, path, 128)


def test_bailian_presets_are_data_and_keep_original_profile_and_fields() -> None:
    embedding = bailian_embedding_config("configured-v1", "config-2")
    assert embedding.profile == ModelProfile(
        "text-embedding-v4", "configured-v1", 1024, config_version="config-2"
    )
    assert embedding.path == "/compatible-mode/v1/embeddings"
    assert (embedding.max_batch, embedding.max_text_bytes) == (10, 8192)
    assert embedding.send_dimensions and embedding.encoding_format == "float"
    assert embedding.extra_body == {}
    chat = bailian_chat_config("config-2")
    assert chat.profile == ModelProfile(
        "qwen-plus-2025-12-01", "qwen-plus-2025-12-01", config_version="config-2"
    )
    assert chat.path == "/compatible-mode/v1/chat/completions"
    assert chat.max_text_bytes == 8192
    assert chat.extra_body == {"enable_thinking": False}

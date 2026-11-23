"""Regression coverage for the portable retrieval edge cases."""

import time
from dataclasses import replace

import httpx
import pytest

from semantic_core._http import HTTPConfig, validate_path
from semantic_core.openai_compatible import (
    ChatConfig,
    EmbeddingConfig,
    OpenAIChatProvider,
    OpenAIEmbeddingProvider,
    bailian_chat_config,
    bailian_embedding_config,
)
from semantic_core.providers import ModelProfile, RequestContext, RerankDocument
from semantic_core.rerank import (
    HTTPRerankProvider,
    RerankCodec,
    RerankConfig,
    bailian_rerank_config,
)


@pytest.mark.parametrize("key", ["Model", "INPUT", "Messages", "Response_Format"])
@pytest.mark.parametrize("kind", ["embedding", "chat"])
def test_protected_extensions_reject_case_variants(key, kind):
    with pytest.raises(ValueError, match="protected"):
        if kind == "embedding":
            EmbeddingConfig(
                ModelProfile("e", "v1", 2), "/embeddings", 10, 1024, extra_body={key: []}
            )
        else:
            ChatConfig(ModelProfile("c", "v1"), "/chat/completions", 1024, extra_body={key: []})


@pytest.mark.parametrize(
    "path",
    [
        "/.",
        "/..",
        "/a/./b",
        "/a/../b",
        "/%252e%252e/admin",
        "/%25252e/admin",
        "/.%252e/admin",
        "/%252e%252e%252fadmin",
        "/..;/admin",
        "/.;mode=1/admin",
        "/.%3bmode=1/admin",
        "/..%253bmode=1/admin",
        "/%252e%253bmode=1/admin",
    ],
)
def test_paths_reject_repeated_encoding_and_dot_segments(path):
    with pytest.raises(ValueError):
        validate_path(path)
    with pytest.raises(ValueError):
        HTTPConfig("https://fixture.invalid" + path, ("fixture.invalid",))


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/openid-configuration",
        "/v1/.internal/embeddings",
        "/a/.hidden",
        "/...foo/embeddings",
        "/%252ewell-known/openid-configuration",
        "/.well-known;version=1/openid-configuration",
    ],
)
def test_named_dot_segments_are_valid_paths_and_base_prefixes(path):
    validate_path(path)
    base_url = "https://fixture.invalid" + path
    assert HTTPConfig(base_url, ("fixture.invalid",)).base_url == base_url


@pytest.mark.parametrize(
    "path",
    [
        "/%252fexternal.invalid",
        "/safe/%255cadmin",
        "/safe/%2500admin",
        "/safe/%250aadmin",
        "/safe/%257fadmin",
    ],
)
def test_encoded_origin_backslash_and_controls_remain_rejected(path):
    with pytest.raises(ValueError):
        validate_path(path)
    with pytest.raises(ValueError):
        HTTPConfig("https://fixture.invalid" + path, ("fixture.invalid",))


@pytest.mark.parametrize("kind", ["embedding", "chat", "rerank"])
def test_dot_named_routes_work_through_every_http_provider(kind):
    path = "/.internal/" + kind
    calls = []
    payloads = {
        "embedding": {"data": [{"index": 0, "embedding": [3, 4]}]},
        "chat": {
            "choices": [
                {"finish_reason": "stop", "message": {"content": '{"queries":["find orders"]}'}}
            ]
        },
        "rerank": {"results": [{"index": 0, "relevance_score": 0.9}]},
    }

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=payloads[kind])

    transport = HTTPConfig("https://fixture.invalid/.well-known", ("fixture.invalid",))
    context = RequestContext("dot-named-route", time.monotonic() + 10)
    with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client:
        if kind == "embedding":
            provider = OpenAIEmbeddingProvider(
                client,
                transport,
                EmbeddingConfig(ModelProfile("fixture", "v1", 2), path, 10, 1024),
            )
            assert provider.embed(["orders"], "query", context).vectors.shape == (1, 2)
        elif kind == "chat":
            provider = OpenAIChatProvider(
                client, transport, ChatConfig(ModelProfile("fixture", "v1"), path, 1024)
            )
            assert provider.generate_queries("orders", ("en",), 1, context).queries == (
                "find orders",
            )
        else:
            provider = HTTPRerankProvider(
                client,
                transport,
                RerankConfig(ModelProfile("fixture", "v1"), RerankCodec(path)),
            )
            assert (
                provider.rerank("orders", (RerankDocument("orders", "find orders"),), 1, context)
                .items[0]
                .id
                == "orders"
            )
    assert calls == ["https://fixture.invalid/.well-known" + path]


@pytest.mark.parametrize("kind", ["embedding", "chat"])
def test_configuration_hash_agrees_with_nested_json_mapping_equality(kind):
    source = {"nested": {"a": [1, {"b": True}], "c": None}, "stop": ["\\n"]}
    if kind == "embedding":
        config = EmbeddingConfig(
            ModelProfile("e", "v1", 2), "/embeddings", 10, 1024, extra_body=source
        )
    else:
        config = ChatConfig(ModelProfile("c", "v1"), "/chat/completions", 1024, extra_body=source)
    equivalent = replace(
        config, extra_body={"stop": ["\\n"], "nested": {"c": None, "a": [1, {"b": True}]}}
    )
    assert config == equivalent
    assert hash(config) == hash(equivalent)
    before = hash(config)
    source["stop"].append("changed")
    assert hash(config) == before
    assert {config: "cached"}[equivalent] == "cached"


@pytest.mark.parametrize("kind", ["embedding", "chat", "rerank"])
def test_bailian_presets_require_origin_base_at_library_boundary(kind):
    transport = HTTPConfig("https://fixture.invalid/v1", ("fixture.invalid",))
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("sent"))) as client:
        with pytest.raises(ValueError, match="origin"):
            if kind == "embedding":
                OpenAIEmbeddingProvider(client, transport, bailian_embedding_config("v1"))
            elif kind == "chat":
                OpenAIChatProvider(client, transport, bailian_chat_config())
            else:
                HTTPRerankProvider(client, transport, bailian_rerank_config("qwen3-rerank", "v1"))

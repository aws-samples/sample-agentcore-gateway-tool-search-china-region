"""HTTP adapter fixtures exercise only httpx.MockTransport."""

import json
import time
from collections.abc import Callable
from functools import partial

import httpx
import numpy as np
import pytest

from semantic_core.http_factories import (
    OriginHTTPConfig,
)
from semantic_core.http_factories import (
    create_chat_provider as raw_chat_provider,
)
from semantic_core.http_factories import (
    create_embedding_provider as raw_embedding_provider,
)
from semantic_core.http_factories import (
    create_rerank_provider as raw_rerank_provider,
)
from semantic_core.providers import (
    ErrorCode,
    ModelProfile,
    ProviderError,
    RequestContext,
    RerankDocument,
)
from semantic_core.rerank import RerankCodec

create_embedding_provider = partial(raw_embedding_provider, model_id="example-embedding-model")
create_chat_provider = partial(
    raw_chat_provider, model_id="example-chat-model-v1", revision="example-chat-model-v1"
)


def create_rerank_provider(client, config, *, model_id, revision, **kwargs):
    codec = RerankCodec(
        "/v1/rerank",
        envelope="nested-output" if model_id == "example-nested-rerank-model" else "flat",
    )
    return raw_rerank_provider(
        client, config, model_id=model_id, revision=revision, codec=codec, **kwargs
    )


HOST = "offline.example"


def context() -> RequestContext:
    return RequestContext("offline-contract", time.monotonic() + 10)


def config() -> OriginHTTPConfig:
    return OriginHTTPConfig(f"https://{HOST}", (HOST,))


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def embedding_payload() -> dict[str, object]:
    return {
        "model": "example-embedding-model",
        "data": [
            {"index": 1, "embedding": [4.0] + [0.0] * 1023},
            {"index": 0, "embedding": [0.0, 3.0] + [0.0] * 1022},
        ],
        "usage": {"total_tokens": 4},
    }


def test_embedding_request_shape_input_order_and_normalization() -> None:
    requests: list[httpx.Request] = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=embedding_payload())

    with client(handler) as http:
        provider = create_embedding_provider(http, config(), revision="configured-v1")
        result = provider.embed(["first", "second"], "document", context())
    assert requests[0].url.path == "/v1/embeddings"
    assert json.loads(requests[0].content) == {
        "model": "example-embedding-model",
        "input": ["first", "second"],
        "dimensions": 1024,
        "encoding_format": "float",
    }
    assert requests[0].headers["x-request-id"] == "offline-contract"
    assert 0 < requests[0].extensions["timeout"]["read"] <= 1.0
    assert result.profile == ModelProfile("example-embedding-model", "configured-v1", 1024)
    np.testing.assert_array_equal(result.vectors[:, :2], [[0, 1], [1, 0]])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(model="unexpected-model"),
        lambda p: p.update(revision="unexpected-revision"),
        lambda p: p.update(config_version="unexpected-configuration"),
        lambda p: p.update(data=[{"index": 0, "embedding": [0.0] * 1024}] * 2),
        lambda p: p.update(data=[{"index": 0, "embedding": [1.0]}]),
        lambda p: p.update(data=[{"index": 2, "embedding": [1.0] * 1024}] * 2),
        lambda p: p.update(usage={"total_tokens": -1}),
        lambda p: p.update(usage={"total_tokens": "private response body"}),
    ],
)
def test_embedding_malformed_output_is_rejected(mutation: Callable) -> None:
    payload = embedding_payload()
    mutation(payload)
    with client(lambda _: httpx.Response(200, json=payload)) as http:
        provider = create_embedding_provider(http, config(), revision="configured-v1")
        with pytest.raises(ProviderError) as error:
            provider.embed(["first", "second"], "query", context())
    assert error.value.code is ErrorCode.INVALID_RESPONSE
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, ErrorCode.INVALID_REQUEST),
        (401, ErrorCode.AUTHENTICATION),
        (403, ErrorCode.AUTHENTICATION),
        (429, ErrorCode.RATE_LIMITED),
        (500, ErrorCode.UNAVAILABLE),
        (503, ErrorCode.UNAVAILABLE),
        (302, ErrorCode.INVALID_RESPONSE),
    ],
)
def test_http_errors_are_safe_and_do_not_retry(status: int, expected: ErrorCode) -> None:
    requests: list[httpx.Request] = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status, text="private upstream body", headers={"Location": "https://evil.test"}
        )

    with client(handler) as http:
        provider = create_embedding_provider(http, config(), revision="configured-v1")
        with pytest.raises(ProviderError) as error:
            provider.embed(["one"], "query", context())
    assert len(requests) == 1
    assert error.value.code is expected
    assert "private" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_errors_are_classified(failure: type[httpx.RequestError]) -> None:
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure("private body", request=request)

    with client(handler) as http:
        provider = create_embedding_provider(http, config(), revision="configured-v1")
        with pytest.raises(ProviderError) as error:
            provider.embed(["one"], "query", context())
    assert error.value.code is (
        ErrorCode.TIMEOUT if failure is httpx.ReadTimeout else ErrorCode.UNAVAILABLE
    )
    assert "private" not in str(error.value)
    assert error.value.__cause__ is None


def test_expired_deadline_and_embedding_budget_do_not_send_requests() -> None:
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def never(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected request to {request.url.host}")

    with client(never) as http:
        provider = create_embedding_provider(http, config(), revision="configured-v1")
        for texts, ctx, code in [
            (["one"], RequestContext("expired", 0), ErrorCode.TIMEOUT),
            (["one"] * 11, context(), ErrorCode.INVALID_REQUEST),
            (["x" * 8193], context(), ErrorCode.INVALID_REQUEST),
        ]:
            with pytest.raises(ProviderError) as error:
                provider.embed(texts, "query", ctx)
            assert error.value.code is code


def test_response_size_is_bounded_and_body_is_not_exposed() -> None:
    small_config = OriginHTTPConfig(f"https://{HOST}", (HOST,), max_response_bytes=32)
    with client(lambda _: httpx.Response(200, text="private" * 100)) as http:
        provider = create_embedding_provider(http, small_config, revision="configured-v1")
        with pytest.raises(ProviderError) as error:
            provider.embed(["one"], "query", context())
    assert error.value.code is ErrorCode.INVALID_RESPONSE
    assert "private" not in str(error.value)


@pytest.mark.parametrize("content", [b"not JSON", b'["not an object"]', b"\xff"])
def test_invalid_http_json_is_classified(content: bytes) -> None:
    with client(lambda _: httpx.Response(200, content=content)) as http:
        provider = create_embedding_provider(http, config(), revision="configured-v1")
        with pytest.raises(ProviderError) as error:
            provider.embed(["one"], "query", context())
    assert error.value.code is ErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize(
    "base_url",
    [
        "http://offline.example",
        "https://other.example",
        "https://offline.example/secret",
        "https://user:password@offline.example",
        "https://offline.example?query=1",
        "https://offline.example:444",
        "https://offline.example/#fragment",
    ],
)
def test_endpoint_must_match_exact_explicit_https_allowlist(base_url: str) -> None:
    with pytest.raises(ValueError):
        OriginHTTPConfig(base_url, (HOST,))


@pytest.mark.parametrize(
    "provider_kind", ["embedding", "example-nested-rerank-model", "example-rerank-model", "llm"]
)
@pytest.mark.parametrize("alias_enabled", [False, True])
def test_response_model_aliases_are_explicit_and_preserve_profile(
    provider_kind: str, alias_enabled: bool
) -> None:
    alias = "fixture-approved-response"
    settings = OriginHTTPConfig(
        f"https://{HOST}", (HOST,), response_model_aliases=(alias,) if alias_enabled else ()
    )
    if provider_kind == "embedding":
        payload = embedding_payload()
    elif provider_kind == "llm":
        payload = llm_payload({"queries": ["valid query"]})
    else:
        results = [{"index": 0, "relevance_score": 0.7}]
        payload = (
            {"output": {"results": results}}
            if provider_kind == "example-nested-rerank-model"
            else {"results": results}
        )
    payload["model"] = alias

    with client(lambda _: httpx.Response(200, json=payload)) as http:
        if provider_kind == "embedding":
            provider = create_embedding_provider(http, settings, revision="configured-v1")
            call = partial(provider.embed, ["first", "second"], "query", context())
        elif provider_kind == "llm":
            provider = create_chat_provider(http, settings)
            call = partial(provider.generate_queries, "description", ("en",), 1, context())
        else:
            provider = create_rerank_provider(
                http, settings, model_id=provider_kind, revision="configured-v1"
            )
            call = partial(provider.rerank, "q", (RerankDocument("a", "a"),), 1, context())
        if alias_enabled:
            result = call()
            assert result.profile == provider.describe()
            assert result.profile.model_id != alias
        else:
            with pytest.raises(ProviderError) as error:
                call()
            assert error.value.code is ErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize(
    "aliases",
    [
        ["alias"],
        "alias",
        ("",),
        (" alias",),
        ("alias ",),
        ("alias*",),
        ("alias?",),
        ("alias", "alias"),
        ("alias\n",),
        (1,),
    ],
)
def test_response_model_aliases_require_an_exact_immutable_allowlist(aliases: object) -> None:
    with pytest.raises(ValueError):
        OriginHTTPConfig(f"https://{HOST}", (HOST,), response_model_aliases=aliases)


@pytest.mark.parametrize(
    "identity",
    [
        {"model": "approved-alias-extra"},
        {"model": "approved-alias", "revision": "different-revision"},
        {"model": "approved-alias", "config_version": "different-config"},
    ],
)
def test_response_model_aliases_do_not_enable_prefixes_or_relax_versions(
    identity: dict[str, object],
) -> None:
    payload = embedding_payload()
    payload.update(identity)
    settings = OriginHTTPConfig(
        f"https://{HOST}", (HOST,), response_model_aliases=("approved-alias",)
    )
    with client(lambda _: httpx.Response(200, json=payload)) as http:
        with pytest.raises(ProviderError) as error:
            create_embedding_provider(http, settings, revision="configured-v1").embed(
                ["first", "second"], "query", context()
            )
    assert error.value.code is ErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize("model", ["example-nested-rerank-model", "example-rerank-model"])
def test_rerank_native_and_flat_shapes(model: str) -> None:
    requests: list[httpx.Request] = []
    expected_url = f"https://{HOST}" + (
        "/v1/rerank" if model == "example-nested-rerank-model" else "/v1/rerank"
    )
    scores = [
        {"index": 1, "relevance_score": 0.8},
        {"index": 0, "relevance_score": 0.3},
    ]

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == expected_url
        requests.append(request)
        body = (
            {"output": {"results": scores}, "usage": {"total_tokens": 5}}
            if model == "example-nested-rerank-model"
            else {"model": model, "results": scores, "usage": {"total_tokens": 5}}
        )
        return httpx.Response(200, json=body)

    documents = (RerankDocument("b", "cancel"), RerankDocument("a", "order"))
    with client(handler) as http:
        provider = create_rerank_provider(http, config(), model_id=model, revision="v1")
        result = provider.rerank("order", documents, 2, context())
    assert [item.id for item in result.items] == ["a", "b"]
    payload = json.loads(requests[0].content)
    if model == "example-nested-rerank-model":
        assert requests[0].url.path == "/v1/rerank"
        assert payload == {
            "model": model,
            "input": {"query": "order", "documents": ["cancel", "order"]},
            "parameters": {"top_n": 2, "return_documents": False},
        }
    else:
        assert requests[0].url.path == "/v1/rerank"
        assert payload == {
            "model": model,
            "query": "order",
            "documents": ["cancel", "order"],
            "top_n": 2,
            "return_documents": False,
        }
    assert b'"id"' not in requests[0].content


@pytest.mark.parametrize(
    "scores",
    [
        [{"index": 0, "relevance_score": 0.5}] * 2,
        [{"index": 2, "relevance_score": 0.5}],
        [{"index": 0, "relevance_score": 0.5}],
        [
            {"index": 0, "relevance_score": 0.1},
            {"index": 1, "relevance_score": 0.9},
        ],
        [
            {"index": 0, "relevance_score": "private"},
            {"index": 1, "relevance_score": 0.1},
        ],
    ],
)
def test_rerank_rejects_duplicate_missing_injected_or_unsorted_results(
    scores: list[dict[str, object]],
) -> None:
    with client(lambda _: httpx.Response(200, json={"results": scores})) as http:
        provider = create_rerank_provider(
            http, config(), model_id="example-rerank-model", revision="v1"
        )
        with pytest.raises(ProviderError) as error:
            provider.rerank("q", (RerankDocument("a", "a"), RerankDocument("b", "b")), 2, context())
    assert error.value.code is ErrorCode.INVALID_RESPONSE


def test_rerank_equal_scores_keep_original_candidate_order() -> None:
    scores = [
        {"index": 1, "relevance_score": 0.5},
        {"index": 0, "relevance_score": 0.5},
    ]
    with client(lambda _: httpx.Response(200, json={"results": scores})) as http:
        provider = create_rerank_provider(
            http, config(), model_id="example-rerank-model", revision="v1"
        )
        result = provider.rerank(
            "q", (RerankDocument("b", "b"), RerankDocument("a", "a")), 2, context()
        )
    assert [item.id for item in result.items] == ["b", "a"]


@pytest.mark.parametrize("score", ["NaN", "Infinity", "-Infinity", "true"])
def test_rerank_nonfinite_and_boolean_scores_are_rejected(score: str) -> None:
    content = '{"results":[{"index":0,"relevance_score":' + score + "}]}"
    with client(lambda _: httpx.Response(200, content=content)) as http:
        provider = create_rerank_provider(
            http, config(), model_id="example-rerank-model", revision="v1"
        )
        with pytest.raises(ProviderError) as error:
            provider.rerank("q", (RerankDocument("a", "a"),), 1, context())
    assert error.value.code is ErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("query", "documents"),
    [
        ("x" * 257, (RerankDocument("a", "a"),)),
        ("q", (RerankDocument("a", "x" * 513),)),
        ("q", tuple(RerankDocument(str(i), "a") for i in range(31))),
        ("q", (RerankDocument("a", "a"), RerankDocument("a", "b"))),
    ],
)
def test_rerank_budget_checked_before_external_io(
    query: str, documents: tuple[RerankDocument, ...]
) -> None:
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def never(_: httpx.Request) -> httpx.Response:
        pytest.fail("budget failure must not send a request")

    with client(never) as http:
        provider = create_rerank_provider(
            http, config(), model_id="example-nested-rerank-model", revision="v1"
        )
        with pytest.raises(ProviderError) as error:
            provider.rerank(query, documents, len(documents), context())
    assert error.value.code is ErrorCode.INVALID_REQUEST


def llm_payload(content: object) -> dict[str, object]:
    return {
        "model": "example-chat-model-v1",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(content)},
            }
        ],
        "usage": {"total_tokens": 10},
    }


@pytest.mark.parametrize("provider_kind", ["embedding", "native-rerank", "flat-rerank", "llm"])
def test_usage_details_and_extensions_do_not_break_valid_results(provider_kind: str) -> None:
    requests: list[httpx.Request] = []
    expected_usage = {
        "input_tokens": 9,
        "output_tokens": 3,
        "prompt_tokens": 9,
        "completion_tokens": 3,
        "total_tokens": 12,
    }
    usage = {
        **expected_usage,
        "prompt_tokens_details": {"cached_tokens": 2},
        "completion_tokens_details": {"reasoning_tokens": 1},
        "supplier_future_counter": 99,
        "supplier_metadata": {"untrusted_text": "fixture-only private metadata"},
        "supplier_extension": ["unused"],
    }

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if provider_kind == "embedding":
            payload = embedding_payload()
        elif provider_kind == "llm":
            payload = llm_payload({"queries": ["valid query"]})
        else:
            results = [{"index": 0, "relevance_score": 0.7}]
            payload = (
                {"output": {"results": results}}
                if provider_kind == "native-rerank"
                else {"results": results}
            )
        payload["usage"] = usage
        return httpx.Response(200, json=payload)

    with client(handler) as http:
        if provider_kind == "embedding":
            result = create_embedding_provider(http, config(), revision="configured-v1").embed(
                ["first", "second"], "query", context()
            )
            assert result.vectors.shape == (2, 1024)
        elif provider_kind == "llm":
            result = create_chat_provider(http, config()).generate_queries(
                "description", ("en",), 1, context()
            )
            assert result.queries == ("valid query",)
        else:
            model = (
                "example-nested-rerank-model"
                if provider_kind == "native-rerank"
                else "example-rerank-model"
            )
            result = create_rerank_provider(
                http, config(), model_id=model, revision="configured-v1"
            ).rerank("q", (RerankDocument("a", "a"),), 1, context())
            assert [item.id for item in result.items] == ["a"]
    assert result.usage == expected_usage
    assert len(requests) == 1


@pytest.mark.parametrize("unknown_value", [None, True, 3, 1.2, "unused", [], {"tokens": 1}])
def test_usage_unknown_fields_are_ignored_even_without_known_counts(unknown_value: object) -> None:
    payload = embedding_payload()
    payload["usage"] = {"supplier_extension": unknown_value}
    with client(lambda _: httpx.Response(200, json=payload)) as http:
        result = create_embedding_provider(http, config(), revision="configured-v1").embed(
            ["first", "second"], "query", context()
        )
    assert result.usage == {}


@pytest.mark.parametrize("invalid_count", [-1, True, 1.5, "12", None, [], {"tokens": 1}])
def test_usage_known_scalar_counts_remain_validated(invalid_count: object) -> None:
    payload = embedding_payload()
    payload["usage"] = {
        "total_tokens": invalid_count,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    with client(lambda _: httpx.Response(200, json=payload)) as http:
        with pytest.raises(ProviderError) as error:
            create_embedding_provider(http, config(), revision="configured-v1").embed(
                ["first", "second"], "query", context()
            )
    assert error.value.code is ErrorCode.INVALID_RESPONSE


def test_llm_uses_json_object_and_local_schema_with_deduplication() -> None:
    requests: list[httpx.Request] = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json=llm_payload({"queries": [" cancel order ", "cancel order", "查询"]})
        )

    with client(handler) as http:
        provider = create_chat_provider(http, config())
        result = provider.generate_queries("untrusted input", ("zh", "en"), 3, context())
    assert result.queries == ("cancel order", "查询")
    payload = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/chat/completions"
    assert payload["response_format"] == {"type": "json_object"}
    assert "enable_thinking" not in payload
    assert "JSON" in payload["messages"][0]["content"]
    assert "untrusted input" not in payload["messages"][0]["content"]
    assert "untrusted input" in payload["messages"][1]["content"]
    assert "tools" not in payload
    assert result.prompt_version


@pytest.mark.parametrize(
    "content",
    [
        {"queries": ["a"], "extra": "private"},
        {"queries": ["x" * 257]},
        {"queries": ["a"] * 7},
        {"queries": [""]},
        {"queries": "not an array"},
    ],
)
def test_llm_retries_invalid_json_at_most_once(content: object) -> None:
    requests: list[httpx.Request] = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=llm_payload(content))

    with client(handler) as http:
        provider = create_chat_provider(http, config())
        with pytest.raises(ProviderError) as error:
            provider.generate_queries("description", ("zh",), 6, context())
    assert len(requests) == 2
    assert error.value.code is ErrorCode.INVALID_RESPONSE
    assert "private" not in str(error.value)


def test_llm_second_attempt_can_succeed() -> None:
    responses = iter(
        [
            llm_payload({"queries": ["bad"], "unknown": True}),
            llm_payload({"queries": ["valid query"]}),
        ]
    )
    with client(lambda _: httpx.Response(200, json=next(responses))) as http:
        result = create_chat_provider(http, config()).generate_queries(
            "description", ("en",), 1, context()
        )
    assert result.queries == ("valid query",)


@pytest.mark.parametrize("failure_kind", ["json", "schema"])
def test_llm_schema_repair_changes_prompt_without_echoing_invalid_output(
    failure_kind: str,
) -> None:
    requests: list[httpx.Request] = []
    invalid_reply = "fixture-invalid-reply-marker"
    first = llm_payload(
        {"queries": ["valid text"], "unexpected": invalid_reply} if failure_kind == "schema" else {}
    )
    if failure_kind == "json":
        first["choices"] = [{"finish_reason": "stop", "message": {"content": invalid_reply}}]
    responses = iter([first, llm_payload({"queries": ["valid query"]})])

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=next(responses))

    with client(handler) as http:
        result = create_chat_provider(http, config()).generate_queries(
            "untrusted original description", ("en",), 1, context()
        )
    assert result.queries == ("valid query",)
    assert len(requests) == 2
    initial, repair = [json.loads(request.content) for request in requests]
    assert repair != initial
    assert repair["messages"][0]["content"].startswith(initial["messages"][0]["content"])
    assert repair["messages"][1:] == initial["messages"][1:]
    assert invalid_reply not in requests[1].content.decode()
    assert result.prompt_version == "tool-queries-v2"


@pytest.mark.parametrize(
    "failure_kind",
    [
        "model",
        "revision",
        "config_version",
        "usage",
        "http-json",
        "http-status",
        "choices",
        "nontext",
        "tool-call",
        "truncated",
    ],
)
def test_llm_schema_repair_does_not_retry_noncontent_failures(failure_kind: str) -> None:
    requests: list[httpx.Request] = []
    payload = llm_payload({"queries": ["valid query"]})
    if failure_kind in ("model", "revision", "config_version"):
        payload[failure_kind] = "unexpected-identity"
    elif failure_kind == "usage":
        payload["usage"] = {"total_tokens": -1}
    elif failure_kind == "choices":
        payload["choices"] = []
    elif failure_kind in ("nontext", "tool-call", "truncated"):
        message = {"content": None if failure_kind == "nontext" else '{"queries":["q"]}'}
        if failure_kind == "tool-call":
            message["tool_calls"] = [{"name": "unrequested-tool"}]
        payload["choices"] = [
            {
                "finish_reason": "length" if failure_kind == "truncated" else "stop",
                "message": message,
            }
        ]

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure_kind == "http-json":
            return httpx.Response(200, content=b"invalid HTTP JSON envelope")
        if failure_kind == "http-status":
            return httpx.Response(503)
        return httpx.Response(200, json=payload)

    with client(handler) as http:
        with pytest.raises(ProviderError):
            create_chat_provider(http, config()).generate_queries(
                "description", ("en",), 1, context()
            )
    assert len(requests) == 1


def test_llm_does_not_retry_authentication_errors() -> None:
    requests: list[httpx.Request] = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401)

    with client(handler) as http:
        with pytest.raises(ProviderError) as error:
            create_chat_provider(http, config()).generate_queries(
                "description", ("en",), 1, context()
            )
    assert error.value.code is ErrorCode.AUTHENTICATION
    assert len(requests) == 1


@pytest.mark.parametrize(
    "choice",
    [
        {"finish_reason": "length", "message": {"content": '{"queries":["a"]}'}},
        {"finish_reason": "stop", "message": {"content": None}},
        {
            "finish_reason": "stop",
            "message": {"content": '{"queries":["a"]}', "tool_calls": [{"name": "evil"}]},
        },
    ],
)
def test_llm_rejects_truncated_nontext_and_tool_responses(
    choice: dict[str, object],
) -> None:
    with client(lambda _: httpx.Response(200, json={"choices": [choice]})) as http:
        with pytest.raises(ProviderError) as error:
            create_chat_provider(http, config()).generate_queries(
                "description", ("en",), 1, context()
            )
    assert error.value.code is ErrorCode.INVALID_RESPONSE

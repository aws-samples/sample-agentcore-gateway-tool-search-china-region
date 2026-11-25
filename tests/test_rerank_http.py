"""Portable rerank contracts use mock transport and synthetic data only."""

from __future__ import annotations

import json
import time
from dataclasses import FrozenInstanceError, replace
from typing import Literal, cast

import httpx
import pytest

from semantic_core._http import HTTPConfig
from semantic_core.providers import (
    ErrorCode,
    ModelProfile,
    ProviderError,
    RequestContext,
    RerankDocument,
    RerankProvider,
)
from semantic_core.rerank import (
    HTTPRerankProvider,
    RerankCodec,
    RerankConfig,
    configured_rerank_config,
)

HOST = "rerank.example.invalid"
DOCUMENTS = (RerankDocument("local-a", "cancel order"), RerankDocument("local-b", "order status"))


def context() -> RequestContext:
    return RequestContext("portable-rerank", time.monotonic() + 5)


def config(codec: RerankCodec | None = None) -> RerankConfig:
    return RerankConfig(ModelProfile("portable-rerank-v1", "fixture-v1"), codec or RerankCodec())


def transport() -> HTTPConfig:
    return HTTPConfig(f"https://{HOST}", (HOST,))


def test_portable_model_can_be_configured() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 0, "relevance_score": 0.9},
                        {"index": 1, "relevance_score": 0.3},
                    ]
                },
            )
        ),
        trust_env=False,
    ) as http:
        provider = HTTPRerankProvider(http, transport(), config())
        assert isinstance(provider, RerankProvider)
        result = provider.rerank("cancel", DOCUMENTS, 2, context())
    assert [item.id for item in result.items] == ["local-a", "local-b"]
    assert result.profile == config().profile


def test_portable_flat_endpoint_is_explicit() -> None:
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"https://{HOST}/v1/rerank"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.3},
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http:
        provider = HTTPRerankProvider(http, transport(), config())
        provider.rerank("cancel", DOCUMENTS, 2, context())


def test_portable_score_field_is_configurable() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"results": [{"index": 0, "score": 0.9}, {"index": 1, "score": 0.3}]},
            )
        ),
        trust_env=False,
    ) as http:
        provider = HTTPRerankProvider(http, transport(), config(RerankCodec(score_field="score")))
        result = provider.rerank("cancel", DOCUMENTS, 2, context())
    assert [item.score for item in result.items] == [0.9, 0.3]


@pytest.mark.parametrize("score_field", ["relevance_score", "score"])
def test_flat_wire_contract_appends_prefix_and_uses_only_explicit_headers(score_field: str) -> None:
    requests: list[httpx.Request] = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == f"https://{HOST}:8443/model-proxy/v1/rerank"
        assert request.method == "POST"
        assert request.headers["X-Rerank-Token"] == "Fixture synthetic-token"
        assert request.headers["X-Request-ID"] == "portable-rerank"
        assert json.loads(request.content) == {
            "model": "portable-rerank-v1",
            "query": "cancel",
            "documents": ["cancel order", "order status"],
            "top_n": 2,
            "return_documents": False,
        }
        assert b"local-a" not in request.content and b"local-b" not in request.content
        return httpx.Response(
            200,
            json={
                "model": "portable-rerank-v1",
                "results": [{"index": 1, score_field: 0.9}, {"index": 0, score_field: 0.5}],
                "usage": {"total_tokens": 9, "vendor_detail": {"cached": 3}},
            },
        )

    settings = HTTPConfig(
        f"https://{HOST}:8443/model-proxy",
        (HOST,),
        auth_header_name="X-Rerank-Token",
        auth_header_format="Fixture {token}",
    )
    codec = RerankCodec(score_field=cast(Literal["relevance_score", "score"], score_field))
    with httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://unused.example.invalid",
        trust_env=False,
    ) as http:
        result = HTTPRerankProvider(
            http, settings, config(codec), credential="synthetic-token"
        ).rerank("cancel", DOCUMENTS, 2, context())
    assert len(requests) == 1
    assert [item.id for item in result.items] == ["local-b", "local-a"]
    assert result.usage == {"total_tokens": 9}


@pytest.mark.parametrize("model", ["example-nested-rerank-model", "example-rerank-model"])
def test_configured_presets_preserve_exact_request_and_response_contract(model: str) -> None:
    native = model == "example-nested-rerank-model"
    settings = configured_rerank_config(
        model,
        "revision-v2",
        "config-v3",
        codec=RerankCodec("/v1/rerank", envelope="nested-output" if native else "flat"),
        max_total_bytes=30_000 if native else 120_000,
    )
    expected_path = "/v1/rerank" if native else "/v1/rerank"
    results = [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.4}]

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"https://{HOST}{expected_path}"
        expected: dict[str, object] = (
            {
                "model": model,
                "input": {"query": "order", "documents": ["cancel order", "order status"]},
                "parameters": {"top_n": 2, "return_documents": False},
            }
            if native
            else {
                "model": model,
                "query": "order",
                "documents": ["cancel order", "order status"],
                "top_n": 2,
                "return_documents": False,
            }
        )
        assert json.loads(request.content) == expected
        payload = {"output": {"results": results}} if native else {"results": results}
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http:
        result = HTTPRerankProvider(http, transport(), settings).rerank(
            "order", DOCUMENTS, 2, context()
        )
    assert [item.id for item in result.items] == ["local-b", "local-a"]
    assert result.profile == ModelProfile(model, "revision-v2", config_version="config-v3")
    assert settings.max_documents == 30
    assert settings.max_query_bytes == 256
    assert settings.max_document_bytes == 512
    assert settings.max_total_bytes == (30_000 if native else 120_000)


def test_configured_factory_requires_codec_and_accepts_any_model_name() -> None:
    with pytest.raises(TypeError, match="codec"):
        configured_rerank_config("other-model", "fixture-v1")
    assert (
        configured_rerank_config("other-model", "fixture-v1", codec=RerankCodec()).profile.model_id
        == "other-model"
    )
    assert RerankConfig(ModelProfile("other-model", "fixture-v1"), RerankCodec()).profile.model_id


def test_native_codec_also_accepts_an_independent_model_path_and_score_key() -> None:
    codec = RerankCodec(path="/custom/rank", envelope="nested-output", score_field="score")

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"https://{HOST}/custom/rank"
        assert json.loads(request.content) == {
            "model": "portable-rerank-v1",
            "input": {"query": "q", "documents": ["cancel order", "order status"]},
            "parameters": {"top_n": 2, "return_documents": False},
        }
        return httpx.Response(
            200,
            json={"output": {"results": [{"index": 0, "score": 0.9}, {"index": 1, "score": 0.3}]}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http:
        result = HTTPRerankProvider(http, transport(), config(codec)).rerank(
            "q", DOCUMENTS, 2, context()
        )
    assert [item.id for item in result.items] == ["local-a", "local-b"]


@pytest.mark.parametrize(
    ("field", "limit", "query", "texts"),
    [
        ("max_documents", 1, "q", ("a", "b")),
        ("max_query_bytes", 5, "中文", ("a", "b")),
        ("max_document_bytes", 5, "q", ("订单", "b")),
        ("max_total_bytes", 5, "q", ("ab", "cd")),
    ],
)
def test_configured_byte_and_count_budgets_reject_before_http(
    field: str, limit: int, query: str, texts: tuple[str, ...]
) -> None:
    requests: list[httpx.Request] = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def never(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    settings = replace(config(), **{field: limit})
    documents = tuple(RerankDocument(f"local-{i}", text) for i, text in enumerate(texts))
    with httpx.Client(transport=httpx.MockTransport(never), trust_env=False) as http:
        with pytest.raises(ProviderError) as error:
            HTTPRerankProvider(http, transport(), settings).rerank(query, documents, 2, context())
    assert error.value.code is ErrorCode.INVALID_REQUEST
    assert requests == []


def test_total_byte_budget_includes_query_once_per_document_at_exact_boundary() -> None:
    settings = replace(
        config(), max_documents=2, max_query_bytes=1, max_document_bytes=2, max_total_bytes=6
    )
    documents = (RerankDocument("a", "ab"), RerankDocument("b", "cd"))
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 0, "relevance_score": 0.9},
                        {"index": 1, "relevance_score": 0.3},
                    ]
                },
            )
        ),
        trust_env=False,
    ) as http:
        result = HTTPRerankProvider(http, transport(), settings).rerank(
            "q", documents, 2, context()
        )
    assert len(result.items) == 2


def test_generic_document_limit_is_configurable_without_configured_limit() -> None:
    documents = tuple(RerankDocument(f"local-{i}", "text") for i in range(31))
    results = [{"index": i, "relevance_score": 31 - i} for i in range(31)]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": results})),
        trust_env=False,
    ) as http:
        result = HTTPRerankProvider(http, transport(), replace(config(), max_documents=31)).rerank(
            "q", documents, 31, context()
        )
    assert len(result.items) == 31


@pytest.mark.parametrize(
    "results",
    [
        [],
        [{"index": 0, "relevance_score": 0.5}],
        [{"index": 0, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.4}],
        [{"index": -1, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.4}],
        [{"index": 2, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.4}],
        [{"index": True, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.4}],
        [{"index": 1.0, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.4}],
        [{"index": "1", "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.4}],
        [{"index": 1, "relevance_score": float("nan")}, {"index": 0, "relevance_score": 0.4}],
        [{"index": 1, "relevance_score": float("inf")}, {"index": 0, "relevance_score": 0.4}],
        [{"index": 1, "relevance_score": True}, {"index": 0, "relevance_score": 0.4}],
        [{"index": 1, "relevance_score": "0.5"}, {"index": 0, "relevance_score": 0.4}],
        [{"index": 1, "relevance_score": 0.3}, {"index": 0, "relevance_score": 0.9}],
        [{"index": 1, "score": 0.9}, {"index": 0, "score": 0.3}],
    ],
)
def test_invalid_rerank_rows_are_rejected_without_partial_results(results: object) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=json.dumps({"results": results}).encode())
        ),
        trust_env=False,
    ) as http:
        with pytest.raises(ProviderError) as error:
            HTTPRerankProvider(http, transport(), config()).rerank("q", DOCUMENTS, 2, context())
    assert error.value.code is ErrorCode.INVALID_RESPONSE


def test_negative_ties_keep_input_order_and_supplier_ids_are_never_used() -> None:
    results = [
        {"index": 1, "relevance_score": -0.5, "id": "injected", "document": {"name": "injected"}},
        {"index": 0, "relevance_score": -0.5, "id": "injected"},
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": results})),
        trust_env=False,
    ) as http:
        result = HTTPRerankProvider(http, transport(), config()).rerank(
            "q", DOCUMENTS, 2, context()
        )
    assert [item.id for item in result.items] == ["local-a", "local-b"]
    assert [item.score for item in result.items] == [-0.5, -0.5]


@pytest.mark.parametrize(
    ("codec", "response"),
    [
        (RerankCodec(), {"output": {"results": []}}),
        (RerankCodec(envelope="nested-output"), {"results": []}),
        (RerankCodec(envelope="nested-output"), {"output": []}),
        (RerankCodec(), {"results": {}}),
    ],
)
def test_response_envelope_must_match_explicit_codec(
    codec: RerankCodec, response: dict[str, object]
) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
        trust_env=False,
    ) as http:
        with pytest.raises(ProviderError) as error:
            HTTPRerankProvider(http, transport(), config(codec)).rerank(
                "q", DOCUMENTS, 2, context()
            )
    assert error.value.code is ErrorCode.INVALID_RESPONSE


def test_full_candidate_top_n_is_required_before_http() -> None:
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def never(_: httpx.Request) -> httpx.Response:
        pytest.fail("partial-candidate request must not be sent")

    with httpx.Client(transport=httpx.MockTransport(never), trust_env=False) as http:
        with pytest.raises(ProviderError) as error:
            HTTPRerankProvider(http, transport(), config()).rerank("q", DOCUMENTS, 1, context())
    assert error.value.code is ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "3"])
@pytest.mark.parametrize(
    "field", ["max_documents", "max_query_bytes", "max_document_bytes", "max_total_bytes"]
)
def test_budget_configuration_requires_positive_integers(field: str, limit: object) -> None:
    with pytest.raises(ValueError):
        replace(config(), **{field: limit})


@pytest.mark.parametrize(
    "path",
    [
        "https://other.invalid/x",
        "//other.invalid/x",
        "v1/rerank",
        "/x?q=1",
        "/v1/../rerank",
        "/x\n",
    ],
)
def test_codec_path_cannot_change_origin_or_smuggle_query(path: str) -> None:
    with pytest.raises(ValueError):
        RerankCodec(path=path)


def test_codec_and_budget_configuration_are_immutable() -> None:
    codec, settings = RerankCodec(), config()
    with pytest.raises(FrozenInstanceError):
        codec.path = "/changed"
    with pytest.raises(FrozenInstanceError):
        settings.max_documents = 100
    with pytest.raises(ValueError):
        RerankCodec(envelope=cast(Literal["flat"], "unknown"))
    with pytest.raises(ValueError):
        RerankCodec(score_field=cast(Literal["score"], "unknown"))

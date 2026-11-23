"""Fusion and abstention regressions, including authorization and degradation."""

import math
import time
from dataclasses import replace

import numpy as np
import pytest

from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord
from semantic_core.providers import (
    EmbeddingResult,
    ErrorCode,
    ModelProfile,
    ProviderError,
    RequestContext,
    RerankItem,
    RerankResult,
)
from semantic_core.search import AuthorizationError, SearchConfig, SearchEngine
from semantic_core.snapshot import IndexSnapshot
from semantic_core.vectors import VectorIndex

GATEWAY = GatewayIdentity("000000000000", "cn-north-1", "fixture")
PROFILE = ModelProfile("fixture-embedding", "1", 2)


def tool(name, description):
    return ToolRecord.from_descriptor(
        ToolIdentity(GATEWAY, "target", "1", name),
        {
            "name": name,
            "description": description,
            "inputSchema": {"type": "object", "properties": {}},
        },
    )


def fixture(scores=(0.2, 0.3, 0.9), parents=None, tools=None):
    tools = tools or (
        tool("a", "cancel order"),
        tool("b", "billing"),
        tool("c", "inventory"),
    )
    parents = list(range(len(scores))) if parents is None else parents
    vectors = [[score, math.sqrt(1 - score**2)] for score in scores]
    snapshot = IndexSnapshot.build(
        GATEWAY,
        tools,
        vectors=VectorIndex(vectors, parents, len(tools), 2),
        model_profile=PROFILE,
    )
    return snapshot, frozenset(record.tool_id for record in tools)


class Embedding:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []

    def describe(self):
        return PROFILE

    def embed(self, texts, purpose, context):
        self.calls.append((texts, purpose, context))
        if self.failure:
            raise self.failure
        return EmbeddingResult(np.array([[1, 0]], dtype=np.float32), PROFILE)


class Reranker:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []

    def describe(self):
        return ModelProfile("fixture-rerank", "1")

    def rerank(self, query, documents, top_n, context):
        self.calls.append(tuple(documents))
        if self.failure:
            raise self.failure
        # M4: arbitrary negative, request-local scores remain valid rankings.
        return RerankResult(
            tuple(RerankItem(document.id, -10000.0) for document in reversed(documents)),
            self.describe(),
        )


def context():
    return RequestContext("tuning-fixture", time.monotonic() + 10)


def names(result):
    return [hit.tool.name for hit in result.hits]


def test_tuning_is_opt_in_and_default_rrf_is_unchanged():
    config = SearchConfig()
    assert config.fusion_strategy == "rrf"
    assert config.min_vector_similarity is None
    assert config.min_vector_margin is None
    assert config.rerank_enabled is False
    snapshot, allowed = fixture()
    assert names(SearchEngine(snapshot, Embedding()).search("cancel", allowed, context())) == [
        "a",
        "c",
        "b",
    ]


@pytest.mark.parametrize(
    ("change", "expected", "mode"),
    [
        ({"fusion_strategy": "vector"}, ["c", "b", "a"], "vector"),
        ({"lexical_weight": 0.1}, ["c", "b", "a"], "rrf"),
        (
            {"fusion_strategy": "vector_bm25_fallback"},
            ["c", "b", "a"],
            "vector_bm25_fallback",
        ),
        (
            {"fusion_strategy": "vector_bm25_fallback", "bm25_fallback_similarity": 0.95},
            ["a"],
            "bm25",
        ),
    ],
)
def test_fusion_strategy_controls_rank_and_low_confidence_fallback(change, expected, mode):
    snapshot, allowed = fixture()
    result = SearchEngine(snapshot, Embedding(), config=SearchConfig(**change)).search(
        "cancel", allowed, context()
    )
    assert names(result) == expected
    assert result.mode == mode
    assert result.ranking_mode == mode


def test_fallback_without_positive_lexical_matches_keeps_vector_ranking():
    snapshot, allowed = fixture()
    config = SearchConfig(fusion_strategy="vector_bm25_fallback", bm25_fallback_similarity=0.95)
    result = SearchEngine(snapshot, Embedding(), config=config).search(
        "unfindable", allowed, context()
    )
    assert names(result) == ["c", "b", "a"]


def test_fallback_fills_vectorless_tools_only_from_positive_bm25():
    snapshot, allowed = fixture(scores=(0.9,), parents=[2])
    config = SearchConfig(fusion_strategy="vector_bm25_fallback")
    result = SearchEngine(snapshot, Embedding(), config=config).search("cancel", allowed, context())
    assert names(result) == ["c", "a"]


@pytest.mark.parametrize("strategy", ["rrf", "vector", "vector_bm25_fallback"])
def test_cosine_gate_is_not_bypassed_by_fusion_or_rerank(strategy):
    snapshot, allowed = fixture()
    reranker = Reranker()
    config = SearchConfig(
        fusion_strategy=strategy,
        min_vector_similarity=0.95,
        bm25_fallback_similarity=0.99,
        rerank_enabled=True,
    )
    result = SearchEngine(snapshot, Embedding(), reranker, config).search(
        "cancel", allowed, context()
    )
    assert result.hits == ()
    assert reranker.calls == []


@pytest.mark.parametrize(
    ("similarity", "margin", "accepted"),
    [(0.8, None, True), (0.95, None, False), (None, 0.5, True), (None, 0.7, False)],
)
def test_vector_confidence_accepts_or_abstains_per_request(similarity, margin, accepted):
    snapshot, allowed = fixture()
    config = SearchConfig(
        fusion_strategy="vector",
        min_vector_similarity=similarity,
        min_vector_margin=margin,
    )
    result = SearchEngine(snapshot, Embedding(), config=config).search("cancel", allowed, context())
    assert bool(result.hits) is accepted


def test_confidence_distribution_ignores_unauthorized_parent_scores():
    records = tuple(tool(f"tool{i}", "cancel") for i in range(5))
    snapshot, _ = fixture(scores=(0.99, 0.98, 0.9, 0.5, 0.1), tools=records)
    allowed = frozenset(record.tool_id for record in records[2:])
    reranker = Reranker()
    config = SearchConfig(fusion_strategy="vector", min_vector_margin=0.3, rerank_enabled=True)
    result = SearchEngine(snapshot, Embedding(), reranker, config).search(
        "cancel", allowed, context()
    )
    assert {hit.tool.tool_id for hit in result.hits} == allowed
    assert {document.id for document in reranker.calls[0]} == allowed


def test_relative_margin_uses_all_authorized_scores_before_candidate_limit():
    snapshot, allowed = fixture()
    config = SearchConfig(fusion_strategy="vector", recall_k=1, min_vector_margin=0.5)
    result = SearchEngine(snapshot, Embedding(), config=config).search("cancel", allowed, context())
    assert names(result) == ["c"]


@pytest.mark.parametrize("margin", [0.0, 0.01])
def test_single_authorized_score_has_zero_relative_margin(margin):
    snapshot, _ = fixture(scores=(1.0,), parents=[2])
    allowed = frozenset({snapshot.tools[2].tool_id})
    config = SearchConfig(
        fusion_strategy="vector", min_vector_similarity=1.0, min_vector_margin=margin
    )
    result = SearchEngine(snapshot, Embedding(), config=config).search("cancel", allowed, context())
    assert bool(result.hits) is (margin == 0.0)


def test_no_finite_authorized_vectors_abstains_when_gate_enabled():
    snapshot, _ = fixture(scores=(0.9,), parents=[2])
    allowed = frozenset({snapshot.tools[0].tool_id})
    config = SearchConfig(fusion_strategy="vector_bm25_fallback", min_vector_similarity=-1.0)
    result = SearchEngine(snapshot, Embedding(), config=config).search("cancel", allowed, context())
    assert result.hits == ()


def test_empty_authorization_does_not_call_providers_with_tuning_enabled():
    snapshot, _ = fixture()
    embedding, reranker = Embedding(), Reranker()
    config = SearchConfig(
        fusion_strategy="vector",
        min_vector_similarity=0.5,
        min_vector_margin=0.1,
        rerank_enabled=True,
    )
    result = SearchEngine(snapshot, embedding, reranker, config).search(
        "cancel", frozenset(), context()
    )
    assert result.hits == ()
    assert embedding.calls == reranker.calls == []


@pytest.mark.parametrize("revoke_before_egress", [True, False])
def test_revocation_recomputes_confidence_and_never_sends_revoked_candidates(
    revoke_before_egress,
):
    snapshot, allowed = fixture()
    reduced = allowed - {snapshot.tools[2].tool_id}
    authorization = iter([reduced, reduced] if revoke_before_egress else [allowed, reduced])
    reranker = Reranker()
    config = SearchConfig(fusion_strategy="vector", min_vector_margin=0.5, rerank_enabled=True)
    result = SearchEngine(snapshot, Embedding(), reranker, config).search(
        "cancel", allowed, context(), reauthorize=lambda: next(authorization)
    )
    assert result.hits == ()
    assert bool(reranker.calls) is (not revoke_before_egress)


def test_authorization_failure_still_fails_closed_with_confidence_gate():
    snapshot, allowed = fixture()
    reranker = Reranker()

    def unavailable():
        raise RuntimeError("authorization unavailable")

    with pytest.raises(AuthorizationError):
        SearchEngine(
            snapshot, Embedding(), reranker, SearchConfig(min_vector_similarity=0.5)
        ).search("cancel", allowed, context(), reauthorize=unavailable)
    assert reranker.calls == []


@pytest.mark.parametrize("strategy", ["rrf", "vector", "vector_bm25_fallback"])
def test_embedding_failure_keeps_existing_bm25_degradation_with_gates(strategy):
    snapshot, allowed = fixture()
    reranker = Reranker()
    config = SearchConfig(
        fusion_strategy=strategy,
        min_vector_similarity=1.0,
        min_vector_margin=2.0,
        rerank_enabled=True,
    )
    expected = SearchEngine(snapshot).search("cancel", allowed, context())
    actual = SearchEngine(
        snapshot, Embedding(ProviderError(ErrorCode.UNAVAILABLE)), reranker, config
    ).search("cancel", allowed, context())
    assert actual == expected
    assert reranker.calls == []


def test_vector_rerank_failure_preserves_base_and_scores_have_no_absolute_gate():
    snapshot, allowed = fixture()
    config = SearchConfig(fusion_strategy="vector", min_vector_similarity=0.8)
    base = SearchEngine(snapshot, Embedding(), config=config).search("cancel", allowed, context())
    failed = SearchEngine(
        snapshot,
        Embedding(),
        Reranker(ProviderError(ErrorCode.UNAVAILABLE)),
        replace(config, rerank_enabled=True),
    ).search("cancel", allowed, context())
    reranked = SearchEngine(
        snapshot, Embedding(), Reranker(), replace(config, rerank_enabled=True)
    ).search("cancel", allowed, context())
    assert failed == base
    assert names(reranked) == names(base)
    assert reranked.mode == "rerank"


@pytest.mark.parametrize("scores", [np.array(0.5), np.array([[0.2, 0.3, 0.9]])])
def test_invalid_index_score_shape_fails_closed_instead_of_degrading(monkeypatch, scores):
    snapshot, allowed = fixture()
    monkeypatch.setattr(snapshot.vectors, "scores", lambda *_: scores)
    with pytest.raises(ValueError, match="index scores"):
        SearchEngine(snapshot, Embedding(), config=SearchConfig(min_vector_margin=0.1)).search(
            "cancel", allowed, context()
        )


@pytest.mark.parametrize(
    "change",
    [
        {"fusion_strategy": "weighted_sum"},
        {"fusion_strategy": True},
        {"min_vector_similarity": True},
        {"min_vector_similarity": float("nan")},
        {"min_vector_similarity": 1.01},
        {"min_vector_similarity": -1.01},
        {"min_vector_margin": True},
        {"min_vector_margin": -0.01},
        {"min_vector_margin": 2.01},
        {"min_vector_margin": float("inf")},
        {"bm25_fallback_similarity": None},
        {"bm25_fallback_similarity": True},
        {"bm25_fallback_similarity": float("nan")},
        {"bm25_fallback_similarity": -1.01},
        {"bm25_fallback_similarity": 1.01},
    ],
)
def test_invalid_tuning_configuration_is_rejected(change):
    with pytest.raises(ValueError):
        SearchConfig(**change)

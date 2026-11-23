"""Regressions for truthful telemetry and model-failure degradation."""

import json
from dataclasses import replace

import pytest
from test_search_tuning import Embedding, Reranker, context, fixture, names

from semantic_core.providers import ErrorCode, ProviderError
from semantic_core.search import SearchConfig, SearchEngine, SearchHit, SearchResult
from semantic_service import runtime


@pytest.mark.parametrize(
    ("config", "expected_mode"),
    [
        (SearchConfig(fusion_strategy="vector"), "vector"),
        (SearchConfig(fusion_strategy="vector_bm25_fallback"), "vector_bm25_fallback"),
        (
            SearchConfig(fusion_strategy="vector_bm25_fallback", bm25_fallback_similarity=0.95),
            "bm25",
        ),
    ],
)
def test_mode_reports_the_ranking_that_produced_the_hits(config, expected_mode):
    snapshot, allowed = fixture()
    result = SearchEngine(snapshot, Embedding(), config=config).search("cancel", allowed, context())
    assert result.mode == expected_mode
    assert result.ranking_mode == expected_mode


@pytest.mark.parametrize("mode", ["bm25", "rrf", "vector", "vector_bm25_fallback", "rerank"])
def test_result_infers_only_omitted_ranking_mode(mode):
    result = SearchResult((), mode)
    assert result.ranking_mode == mode


@pytest.mark.parametrize(
    ("mode", "ranking_mode"),
    [
        ("rrf", "bm25"),
        ("rrf", "rerank"),
        ("rrf", "vector"),
        ("vector", "rrf"),
        ("bm25", "vector"),
        ("rerank", "vector"),
    ],
)
def test_result_rejects_inconsistent_explicit_modes(mode, ranking_mode):
    with pytest.raises(ValueError, match="mode"):
        SearchResult((), mode, ranking_mode)


@pytest.mark.parametrize("mode", ["invalid", "", None, True])
def test_result_rejects_unknown_modes(mode):
    with pytest.raises(ValueError, match="mode"):
        SearchResult((), mode)


@pytest.mark.parametrize("strategy", ["rrf", "vector", "vector_bm25_fallback"])
def test_gate_rejection_is_explicit_and_different_from_empty_authorization(strategy):
    snapshot, allowed = fixture()
    engine = SearchEngine(
        snapshot,
        Embedding(),
        config=SearchConfig(fusion_strategy=strategy, min_vector_similarity=0.95),
    )
    rejected = engine.search("cancel", allowed, context())
    empty_acl = engine.search("cancel", frozenset(), context())
    assert rejected.hits == empty_acl.hits == ()
    assert rejected.abstained is True
    assert rejected.mode == strategy
    assert empty_acl.abstained is False
    assert empty_acl.mode == "bm25"


def test_nonempty_result_cannot_claim_abstention():
    snapshot, _ = fixture()
    with pytest.raises(ValueError, match="abstain"):
        SearchResult((SearchHit(snapshot.tools[0]),), "vector", abstained=True)


@pytest.mark.parametrize("mode", ["bm25", "rerank"])
def test_modes_that_do_not_run_confidence_gates_cannot_claim_abstention(mode):
    with pytest.raises(ValueError, match="abstain"):
        SearchResult((), mode, abstained=True)


@pytest.mark.parametrize("abstained", [0, 1, None, "true"])
def test_abstained_is_a_strict_boolean(abstained):
    with pytest.raises(ValueError, match="abstain"):
        SearchResult((), "vector", abstained=abstained)


@pytest.mark.parametrize("revoke_before_egress", [True, False])
def test_revocation_recomputes_abstention_and_discards_successful_rerank_mode(
    revoke_before_egress,
):
    snapshot, allowed = fixture()
    reduced = allowed - {snapshot.tools[2].tool_id}
    views = iter([reduced, reduced] if revoke_before_egress else [allowed, reduced])
    reranker = Reranker()
    result = SearchEngine(
        snapshot,
        Embedding(),
        reranker,
        SearchConfig(fusion_strategy="vector", min_vector_similarity=0.8, rerank_enabled=True),
    ).search("cancel", allowed, context(), reauthorize=lambda: next(views))
    assert result.hits == ()
    assert result.mode == result.ranking_mode == "vector"
    assert result.abstained is True
    assert bool(reranker.calls) is (not revoke_before_egress)


@pytest.mark.parametrize("strategy", ["rrf", "vector", "vector_bm25_fallback"])
@pytest.mark.parametrize("code", list(ErrorCode))
def test_embedding_failure_keeps_authorized_bm25_without_claiming_abstention(strategy, code):
    snapshot, all_allowed = fixture()
    allowed = all_allowed - {snapshot.tools[0].tool_id}
    reranker = Reranker()
    result = SearchEngine(
        snapshot,
        Embedding(ProviderError(code)),
        reranker,
        SearchConfig(
            fusion_strategy=strategy,
            min_vector_similarity=1.0,
            min_vector_margin=2.0,
            rerank_enabled=True,
        ),
    ).search("cancel", allowed, context())
    assert result == SearchEngine(snapshot).search("cancel", allowed, context())
    assert result.mode == result.ranking_mode == "bm25"
    assert result.abstained is False
    assert {hit.tool.tool_id for hit in result.hits} <= allowed
    assert reranker.calls == []


@pytest.mark.parametrize("strategy", ["rrf", "vector", "vector_bm25_fallback"])
@pytest.mark.parametrize("code", list(ErrorCode))
def test_rerank_failure_retains_exact_authorized_base_ranking_and_telemetry(strategy, code):
    snapshot, all_allowed = fixture()
    allowed = all_allowed - {snapshot.tools[1].tool_id}
    config = SearchConfig(fusion_strategy=strategy, min_vector_similarity=0.8)
    base = SearchEngine(snapshot, Embedding(), config=config).search("cancel", allowed, context())
    failed = SearchEngine(
        snapshot,
        Embedding(),
        Reranker(ProviderError(code)),
        replace(config, rerank_enabled=True),
    ).search("cancel", allowed, context())
    assert failed == base
    assert failed.mode == failed.ranking_mode == strategy
    assert failed.abstained is False
    assert names(failed) == names(base)
    assert {hit.tool.tool_id for hit in failed.hits} <= allowed


@pytest.mark.parametrize(
    ("mode", "abstained"),
    [
        ("vector", True),
        ("vector", False),
        ("vector_bm25_fallback", False),
        ("bm25", False),
        ("rrf", True),
        ("rerank", False),
    ],
)
def test_runtime_telemetry_emits_only_mode_and_abstention(mode, abstained, capsys):
    runtime._retrieval_mode(mode, abstained)
    assert json.loads(capsys.readouterr().out) == {
        "operation": "retrieval",
        "mode": mode,
        "abstained": abstained,
    }

"""Request-local retrieval diagnostics preserve ranking and public results."""

import time
from dataclasses import FrozenInstanceError, asdict, fields, replace

import numpy as np
import pytest
from test_search_tuning import Embedding, Reranker, context, fixture, names

from semantic_core.providers import ErrorCode, ProviderError, RequestContext
from semantic_core.search import AuthorizationError, SearchConfig, SearchEngine, SearchResult


def test_diagnostics_are_immutable_and_leave_result_shape_and_equality_unchanged():
    snapshot, allowed = fixture()
    engine = SearchEngine(snapshot, Embedding())
    events = []
    result = engine.search("cancel", allowed, context(), diagnostics=events.append)
    assert result == engine.search("cancel", allowed, context())
    assert [field.name for field in fields(SearchResult)] == [
        "hits",
        "mode",
        "ranking_mode",
        "abstained",
    ]
    assert len(events) == 1
    observed = asdict(events[0])
    timing = observed.pop("embedding_timing")
    assert timing["configured_timeout_ms"] == 1000
    assert 0 < timing["stage_budget_ms"] <= 1000
    assert timing["search_remaining_ms"] >= timing["stage_budget_ms"]
    assert timing["attempts"] == ()
    assert timing["attempts_truncated"] is timing["diagnostics_failed"] is False
    assert observed == {
        "configured_fusion": "rrf",
        "ranking_mode": "rrf",
        "empty_result": False,
        "below_threshold": False,
        "degraded": False,
        "vector_availability": "available",
        "rerank_state": "disabled",
        "embedding_error": None,
        "lambda_deadline_ignored": False,
    }
    with pytest.raises(FrozenInstanceError):
        events[0].degraded = True


@pytest.mark.parametrize("strategy", ["rrf", "vector", "vector_bm25_fallback"])
def test_configured_fusion_and_actual_ranking_are_separate(strategy):
    snapshot, allowed = fixture()
    events = []
    config = SearchConfig(fusion_strategy=strategy)
    result = SearchEngine(snapshot, Embedding(), config=config).search(
        "cancel", allowed, context(), diagnostics=events.append
    )
    assert events[0].configured_fusion == strategy
    assert events[0].ranking_mode == result.mode == strategy
    assert events[0].degraded is False


def test_healthy_lexical_fallback_is_not_degradation():
    snapshot, allowed = fixture()
    events = []
    result = SearchEngine(
        snapshot,
        Embedding(),
        config=SearchConfig(fusion_strategy="vector_bm25_fallback", bm25_fallback_similarity=0.95),
    ).search("cancel", allowed, context(), diagnostics=events.append)
    assert names(result) == ["a"]
    assert events[0].configured_fusion == "vector_bm25_fallback"
    assert events[0].ranking_mode == "bm25"
    assert events[0].vector_availability == "available"
    assert events[0].degraded is False


def test_intentionally_disabled_embedding_is_not_degradation():
    snapshot, allowed = fixture()
    events = []
    result = SearchEngine(snapshot).search("cancel", allowed, context(), diagnostics=events.append)
    assert result.mode == events[0].ranking_mode == "bm25"
    assert events[0].vector_availability == "disabled"
    assert events[0].degraded is False


@pytest.mark.parametrize("reason", ["missing_index", "profile_mismatch", "deadline"])
def test_unavailable_vectors_are_distinct_from_disabled_or_failed_provider(reason):
    snapshot, allowed = fixture()
    embedding = Embedding()
    events = []
    request = context()
    if reason == "missing_index":
        snapshot = replace(snapshot, vectors=None, model_profile=None)
    elif reason == "profile_mismatch":
        snapshot = replace(
            snapshot, model_profile=replace(snapshot.model_profile, revision="other")
        )
    else:
        request = RequestContext("expired-embedding", time.monotonic() - 1)
    result = SearchEngine(snapshot, embedding).search(
        "cancel", allowed, request, diagnostics=events.append
    )
    assert result.mode == "bm25"
    assert events[0].vector_availability == "unavailable"
    assert events[0].degraded is True
    assert embedding.calls == []


@pytest.mark.parametrize("code", list(ErrorCode))
def test_embedding_failure_records_degradation_without_threshold_refusal(code):
    snapshot, allowed = fixture()
    events = []
    reranker = Reranker()
    config = SearchConfig(
        fusion_strategy="vector_bm25_fallback",
        min_vector_similarity=1.0,
        min_vector_margin=2.0,
        rerank_enabled=True,
    )
    result = SearchEngine(snapshot, Embedding(ProviderError(code)), reranker, config).search(
        "cancel", allowed, context(), diagnostics=events.append
    )
    assert result == SearchEngine(snapshot).search("cancel", allowed, context())
    assert events[0].vector_availability == "failed"
    assert events[0].degraded is True
    assert events[0].below_threshold is False
    assert events[0].empty_result is False
    assert events[0].rerank_state == "skipped"
    assert reranker.calls == []


@pytest.mark.parametrize(
    "config",
    [
        SearchConfig(fusion_strategy="vector", min_vector_similarity=0.95),
        SearchConfig(fusion_strategy="vector", min_vector_margin=0.7),
    ],
)
def test_both_thresholds_report_refusal_and_empty_result(config):
    snapshot, allowed = fixture()
    events = []
    config = replace(config, rerank_enabled=True)
    result = SearchEngine(snapshot, Embedding(), Reranker(), config).search(
        "cancel", allowed, context(), diagnostics=events.append
    )
    assert result.abstained is True
    assert events[0].ranking_mode == "vector"
    assert events[0].empty_result is True
    assert events[0].below_threshold is True
    assert events[0].degraded is False
    assert events[0].rerank_state == "skipped"


def test_empty_authorization_records_empty_without_refusal_or_provider_attempt():
    snapshot, _ = fixture()
    events = []
    embedding, reranker = Embedding(), Reranker()
    result = SearchEngine(snapshot, embedding, reranker, SearchConfig(rerank_enabled=True)).search(
        "cancel", frozenset(), context(), diagnostics=events.append
    )
    assert result.hits == ()
    assert events[0].ranking_mode is None
    assert events[0].empty_result is True
    assert events[0].below_threshold is False
    assert events[0].degraded is False
    assert events[0].vector_availability == "not_attempted"
    assert events[0].rerank_state == "skipped"
    assert embedding.calls == reranker.calls == []


@pytest.mark.parametrize("late", [False, True])
def test_full_revocation_reports_only_a_ranking_that_completed(late):
    snapshot, allowed = fixture()
    views = iter([allowed, frozenset()] if late else [frozenset(), frozenset()])
    events = []
    result = SearchEngine(snapshot, Embedding()).search(
        "cancel",
        allowed,
        context(),
        reauthorize=lambda: next(views),
        diagnostics=events.append,
    )
    assert result.mode == "bm25"
    assert result.hits == ()
    assert events[0].ranking_mode == ("rrf" if late else None)
    assert events[0].empty_result is True


@pytest.mark.parametrize("enabled", [False, True])
def test_rerank_switch_records_applied_or_disabled(enabled):
    snapshot, allowed = fixture()
    events = []
    reranker = Reranker()
    result = SearchEngine(
        snapshot, Embedding(), reranker, SearchConfig(rerank_enabled=enabled)
    ).search("cancel", allowed, context(), diagnostics=events.append)
    assert events[0].rerank_state == ("applied" if enabled else "disabled")
    assert events[0].ranking_mode == result.mode == ("rerank" if enabled else "rrf")
    assert events[0].degraded is False
    assert bool(reranker.calls) is enabled


@pytest.mark.parametrize("strategy", ["rrf", "vector", "vector_bm25_fallback"])
def test_failed_rerank_reports_degradation_and_keeps_exact_base_order(strategy):
    snapshot, allowed = fixture(scores=(0.9,), parents=[2])
    events = []
    config = SearchConfig(fusion_strategy=strategy)
    expected = SearchEngine(snapshot, Embedding(), config=config).search(
        "cancel", allowed, context()
    )
    actual = SearchEngine(
        snapshot,
        Embedding(),
        Reranker(RuntimeError("never expose provider details")),
        replace(config, rerank_enabled=True),
    ).search("cancel", allowed, context(), diagnostics=events.append)
    assert actual == expected
    assert names(actual) == names(expected)
    assert events[0].ranking_mode == strategy
    assert events[0].rerank_state == "failed"
    assert events[0].degraded is True
    assert "never expose" not in repr(events)


def test_refusal_degradation_and_empty_result_can_coexist_after_revocation():
    snapshot, allowed = fixture()
    events = []
    views = iter([allowed, allowed - {snapshot.tools[2].tool_id}])
    result = SearchEngine(
        snapshot,
        Embedding(),
        Reranker(ProviderError(ErrorCode.UNAVAILABLE)),
        SearchConfig(fusion_strategy="vector", min_vector_similarity=0.8, rerank_enabled=True),
    ).search(
        "cancel",
        allowed,
        context(),
        reauthorize=lambda: next(views),
        diagnostics=events.append,
    )
    assert result.abstained is True
    assert events[0].ranking_mode == "vector"
    assert events[0].rerank_state == "failed"
    assert events[0].below_threshold is True
    assert events[0].empty_result is True
    assert events[0].degraded is True


@pytest.mark.parametrize("reason", ["no_provider", "text_budget", "deadline"])
def test_skipped_rerank_does_not_count_as_provider_failure(reason, monkeypatch):
    snapshot, allowed = fixture()
    reranker = Reranker()
    events = []
    # Lexical and embedding stages remain healthy. Exhaust the deadline only
    # after embedding has passed both of its existing budget checks.
    original_scores = snapshot.vectors.scores
    request = context()
    if reason == "deadline":
        now = time.monotonic()
        request = RequestContext("local-deadline", now + 10)

        def expire_after_vector_scoring(*args):
            scores = original_scores(*args)
            monkeypatch.setattr(time, "monotonic", lambda: now + 11)
            return scores

        monkeypatch.setattr(snapshot.vectors, "scores", expire_after_vector_scoring)
    result = SearchEngine(
        snapshot,
        Embedding(),
        None if reason == "no_provider" else reranker,
        SearchConfig(rerank_enabled=True),
    ).search(
        "cancel" if reason != "text_budget" else "cancel " * 50,
        allowed,
        request,
        diagnostics=events.append,
    )
    assert result.mode == "rrf"
    assert events[0].rerank_state == "skipped"
    assert events[0].degraded is False
    assert reranker.calls == []


def test_post_call_timeout_is_failed_rerank(monkeypatch):
    snapshot, allowed = fixture()
    events = []
    now = time.monotonic()
    reranker = Reranker()
    original = reranker.rerank

    def late_rerank(*args):
        result = original(*args)
        monkeypatch.setattr(time, "monotonic", lambda: now + 100)
        return result

    monkeypatch.setattr(reranker, "rerank", late_rerank)
    result = SearchEngine(
        snapshot, Embedding(), reranker, SearchConfig(rerank_enabled=True)
    ).search(
        "cancel",
        allowed,
        RequestContext("late-rerank", now + 10),
        diagnostics=events.append,
    )
    assert result.mode == "rrf"
    assert events[0].rerank_state == "failed"
    assert events[0].degraded is True
    assert len(reranker.calls) == 1


@pytest.mark.parametrize("reason", ["invalid_query", "invalid_acl", "index", "authorization"])
def test_pre_ranking_failures_emit_once_with_no_completed_ranking(reason, monkeypatch):
    snapshot, allowed = fixture()
    events = []
    query = "" if reason == "invalid_query" else "cancel"
    authorization = None
    if reason == "authorization":

        def authorization():
            raise RuntimeError("sensitive ACL error")

    if reason == "index":
        monkeypatch.setattr(snapshot.vectors, "scores", lambda *_: np.array([[0.1]]))
    with pytest.raises((ValueError, AuthorizationError)):
        SearchEngine(snapshot, Embedding()).search(
            query,
            None if reason == "invalid_acl" else allowed,
            context(),
            reauthorize=authorization,
            diagnostics=events.append,
        )
    assert len(events) == 1
    assert events[0].ranking_mode is None
    assert events[0].empty_result is False
    assert events[0].below_threshold is False
    assert events[0].degraded is False
    assert "sensitive" not in repr(events)


def test_late_authorization_failure_retains_completed_ranking_diagnostics():
    snapshot, allowed = fixture()
    events = []
    checks = 0

    def authorization():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("policy unavailable")
        return allowed

    with pytest.raises(AuthorizationError):
        SearchEngine(snapshot, Embedding(), Reranker(), SearchConfig(rerank_enabled=True)).search(
            "cancel",
            allowed,
            context(),
            reauthorize=authorization,
            diagnostics=events.append,
        )
    assert len(events) == 1
    assert events[0].ranking_mode == "rerank"
    assert events[0].rerank_state == "applied"
    assert events[0].empty_result is False
    assert events[0].degraded is False


def test_cached_engine_collects_fresh_diagnostics_per_search():
    snapshot, allowed = fixture()
    engine = SearchEngine(snapshot, Embedding())
    events = []
    engine.search("cancel", allowed, context(), diagnostics=events.append)
    engine.search("cancel", frozenset(), context(), diagnostics=events.append)
    assert len(events) == 2
    assert events[0] is not events[1]
    assert events[0].empty_result is False
    assert events[1].empty_result is True
    assert events[0].vector_availability == "available"
    assert events[1].vector_availability == "not_attempted"


def test_callback_failure_does_not_change_result_or_fail_closed_error():
    snapshot, allowed = fixture()
    engine = SearchEngine(snapshot, Embedding())

    def broken_callback(event):
        raise RuntimeError("telemetry unavailable")

    assert engine.search(
        "cancel", allowed, context(), diagnostics=broken_callback
    ) == engine.search("cancel", allowed, context())
    with pytest.raises(ValueError, match="query"):
        engine.search("", allowed, context(), diagnostics=broken_callback)

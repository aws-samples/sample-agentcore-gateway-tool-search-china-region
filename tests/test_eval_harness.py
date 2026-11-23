"""End-to-end evaluation contracts, independent of a remote model."""

from dataclasses import replace

import pytest

from semantic_core.providers import MockEmbeddingProvider
from semantic_eval.dataset import generate_dataset
from semantic_eval.harness import CallBudget, MeteredEmbedding, evaluate, run_knee
from semantic_eval.selection import MockSelector


def test_all_ablations_keep_authorization_and_query_denominators() -> None:
    data = generate_dataset()
    data = replace(data, queries=data.queries[:4])
    result = evaluate(data, bootstrap_samples=20)
    runs = result["runs"]
    assert len(runs) == 8
    assert {(row["configuration"]["method"], row["configuration"]["examples"]) for row in runs} == {
        (method, examples)
        for method in ("bm25", "vector", "rrf", "rrf_rerank")
        for examples in (False, True)
    }
    for row in runs:
        assert row["metrics"]["n_queries"] == 4
        assert row["metrics"]["overall"]["n_unauthorized_results"] == 0
    assert result["metadata"]["synthetic"] is True
    assert result["metadata"]["quality_acceptance"] is False


def test_provider_budget_prevents_an_extra_request() -> None:
    from semantic_core.providers import ProviderError, RequestContext

    budget = CallBudget(limit=1)
    provider = MeteredEmbedding(MockEmbeddingProvider(), budget)
    import time

    context = RequestContext("test", time.monotonic() + 5)
    provider.embed(["one"], "query", context)
    with pytest.raises(ProviderError):
        provider.embed(["two"], "query", context)
    assert budget.calls == {"embedding": 1}


def test_knee_compares_paired_queries_and_truth_is_present_at_every_size() -> None:
    data = generate_dataset()
    report = run_knee(
        data, MockSelector(), sizes=(20, 50), queries_per_size=3, bootstrap_samples=20
    )
    assert report["synthetic"] is True
    assert report["quality_acceptance"] is False
    rows = report["runs"]
    assert len(rows) == 4
    for count in (20, 50):
        full, search = [row for row in rows if row["tool_count"] == count]
        assert full["query_ids"] == search["query_ids"]
        assert full["n"] == search["n"] == 3
        assert full["candidate_tools"]["min"] == count
        assert search["candidate_tools"]["max"] <= 5
        assert full["truth_present_count"] == search["truth_present_count"] == 3


def test_knee_rejects_excess_query_budget() -> None:
    with pytest.raises(ValueError, match="50"):
        run_knee(generate_dataset(), MockSelector(), queries_per_size=51)

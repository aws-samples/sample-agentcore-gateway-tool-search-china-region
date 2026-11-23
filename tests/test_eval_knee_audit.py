"""Knee output must support independent scoring and identify retrieval fallback."""

from collections.abc import Sequence

from semantic_core.providers import (
    EmbeddingResult,
    ErrorCode,
    MockEmbeddingProvider,
    ProviderError,
    Purpose,
    RequestContext,
)
from semantic_eval.dataset import generate_dataset
from semantic_eval.harness import run_knee
from semantic_eval.reporting import render_knee_markdown
from semantic_eval.selection import MockSelector


class QueryFailureEmbedding(MockEmbeddingProvider):
    def embed(
        self, texts: Sequence[str], purpose: Purpose, context: RequestContext
    ) -> EmbeddingResult:
        if purpose == "query":
            raise ProviderError(ErrorCode.UNAVAILABLE)
        return super().embed(texts, purpose, context)


def test_knee_records_ids_needed_to_recompute_accuracy_and_retrieval_modes() -> None:
    report = run_knee(
        generate_dataset(),
        MockSelector(),
        sizes=(20,),
        queries_per_size=2,
        bootstrap_samples=10,
    )
    full, search = report["runs"]
    assert full["search_mode_counts"] == {"not_applicable": 2}
    assert search["search_mode_counts"] == {"rrf": 2}
    expected_by_query = {row["query_id"]: row["expected_id"] for row in full["observations"]}
    for run in (full, search):
        correct_count = 0
        for row in run["observations"]:
            assert row["expected_id"] == expected_by_query[row["query_id"]]
            assert len(row["candidate_ids"]) == row["candidate_count"]
            assert len(row["candidate_ids"]) == len(set(row["candidate_ids"]))
            assert row["selected_id"] is None or row["selected_id"] in row["candidate_ids"]
            computed = row["selected_id"] == row["expected_id"] and row["error"] is None
            assert row["correct"] == computed
            correct_count += computed
            if run["arm"] == "full_load":
                assert row["expected_id"] in row["candidate_ids"]
                assert row["search_mode"] == "not_applicable"
            else:
                assert row["search_mode"] == "rrf"
        assert run["accuracy"]["value"] == correct_count / run["n"]


def test_knee_fallback_is_visible_in_json_and_markdown() -> None:
    report = run_knee(
        generate_dataset(),
        MockSelector(),
        sizes=(20,),
        queries_per_size=2,
        embedding=QueryFailureEmbedding(),
        bootstrap_samples=10,
    )
    full, search = report["runs"]
    assert full["search_mode_counts"] == {"not_applicable": 2}
    assert search["search_mode_counts"] == {"bm25": 2}
    assert all(row["search_mode"] == "bm25" for row in search["observations"])
    # BM25 is the retrieval core's successful degradation path, not a dropped query.
    assert search["n"] == 2
    assert search["n_errors"] == 0
    rendered = render_knee_markdown(report)
    assert "Search modes" in rendered
    assert "bm25" in rendered
    assert "not_applicable" in rendered
    assert "fallback" in rendered

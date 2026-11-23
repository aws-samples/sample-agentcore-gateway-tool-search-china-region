"""Offline reports reject mismatched pairs and preserve error denominators."""

from __future__ import annotations

import runpy
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from semantic_eval.dataset import EvalQuery, generate_dataset
from semantic_eval.metrics import Observation, summarize

SCRIPT = Path(__file__).parents[1] / "reports/tuning/summarize_tuning.py"


def _run(name: str, queries: tuple[EvalQuery, ...], *, error: bool = False) -> dict[str, Any]:
    observations = [
        Observation(
            query.id,
            query.language,
            query.permission_group,
            query.relevant_ids,
            query.allowed_ids,
            tuple(set(query.relevant_ids) & set(query.allowed_ids))[:5],
            10.0,
            "invalid_response" if error else None,
        )
        for query in queries
    ]
    return {
        "name": name,
        "config": {},
        "summary": summarize(observations, bootstrap_samples=5),
        "mode_counts": {"vector": len(queries)},
        "observations": [asdict(row) for row in observations],
    }


def test_comparison_requires_identical_pairs_and_counts_provider_errors_as_zero() -> None:
    functions = runpy.run_path(str(SCRIPT))
    queries = tuple(
        query
        for query in generate_dataset().queries
        if 1 <= len(set(query.relevant_ids) & set(query.allowed_ids)) <= 5
    )[:3]
    baseline = _run("baseline", queries)
    candidate = _run("candidate", queries, error=True)
    comparison = functions["compare_retrieval"](baseline, candidate, queries)
    assert comparison["overall"]["m17"]["recall_at_5"]["value"] == -1.0
    assert comparison["overall"]["m17"]["recall_at_5"]["n_queries"] == 3
    candidate["observations"] = candidate["observations"][:-1]
    with pytest.raises(ValueError, match="query IDs"):
        functions["compare_retrieval"](baseline, candidate, queries)


def test_knee_recomputes_accuracy_and_requires_pairing() -> None:
    functions = runpy.run_path(str(SCRIPT))
    query = next(
        query
        for query in generate_dataset().queries
        if len(query.relevant_ids) == 1 and set(query.relevant_ids) <= set(query.allowed_ids)
    )
    rows = [
        {
            "tool_count": 20,
            "arm": arm,
            "query_id": query.id,
            "language": query.language,
            "correct": True,
            "selected_id": query.relevant_ids[0],
            "expected_id": query.relevant_ids[0],
            "candidate_ids": [query.relevant_ids[0]],
            "candidate_count": 1,
            "search_mode": "vector" if arm != "full_load" else "not_applicable",
            "error": None,
            "latency_ms": 20.0,
            "search_ms": 5.0,
            "usage": {"prompt_tokens": 100 if arm == "full_load" else 25},
        }
        for arm in ("full_load", "search_first", "search_rerank")
    ]
    rendered, details = functions["summarize_knee"](rows, (query,), sizes=(20,))
    assert "合成数据" in rendered
    assert "75.0%" in rendered
    assert details["runs"][0]["accuracy"]["value"] == 1.0
    rows[-1]["correct"] = False
    with pytest.raises(ValueError, match="accuracy"):
        functions["summarize_knee"](rows, (query,), sizes=(20,))


def test_empty_and_large_cohorts_are_preserved_in_comparison_details() -> None:
    functions = runpy.run_path(str(SCRIPT))
    queries = generate_dataset().queries[:17]
    run = _run("same", queries)
    comparison = functions["compare_retrieval"](run, run, queries)
    assert set(comparison["overall"]) >= {"m17", "empty", "more_than_5"}
    assert comparison["overall"]["empty"]["false_recommendation_rate"]["value"] == 0
    assert comparison["overall"]["more_than_5"]["recall_at_5"]["n_queries"] > 0
    assert comparison["by_language"]
    assert comparison["by_permission"]
    assert comparison["by_relevant_count"]

from __future__ import annotations

from typing import Any

import pytest

from semantic_eval.metrics import Observation, estimate, summarize
from semantic_eval.reporting import render_knee_markdown, render_markdown


def observation(
    query_id: str,
    *,
    relevant: tuple[str, ...],
    allowed: tuple[str, ...] | None = None,
    retrieved: tuple[str, ...] = (),
    language: str = "zh",
    permission_group: str = "full",
    latency_ms: float = 10.0,
    error: str | None = None,
) -> Observation:
    return Observation(
        query_id=query_id,
        language=language,
        permission_group=permission_group,
        relevant_ids=relevant,
        allowed_ids=allowed if allowed is not None else relevant,
        retrieved_ids=retrieved,
        latency_ms=latency_ms,
        error=error,
    )


def test_primary_recall_uses_authorized_relevance_and_excludes_other_sizes() -> None:
    summary: Any = summarize(
        [
            observation("one", relevant=("a", "denied"), allowed=("a",), retrieved=("a",)),
            observation("two", relevant=("a", "b"), retrieved=("a",)),
            observation(
                "many",
                relevant=("a", "b", "c", "d", "e", "f"),
                retrieved=("a", "b", "c", "d", "e", "f"),
            ),
            observation("none", relevant=(), allowed=("x",), retrieved=("x",)),
        ],
        bootstrap_samples=100,
    )
    overall = summary["overall"]
    assert overall["n"] == 4
    assert overall["primary"]["n"] == 2
    assert overall["primary"]["proportion"] == 0.5
    assert overall["primary"]["recall_at_5"]["value"] == 0.75
    assert overall["primary"]["hit_at_5"]["value"] == 1
    assert overall["more_than_5"]["n"] == 1
    assert overall["more_than_5"]["recall_at_5"]["value"] == 5 / 6
    assert overall["more_than_5"]["theoretical_recall_ceiling"]["value"] == 5 / 6
    assert overall["more_than_5"]["hit_at_5"]["value"] == 1
    assert overall["empty"]["false_recommendation_rate"]["value"] == 1
    assert overall["empty"]["correct_empty_response_rate"]["value"] == 0
    assert set(summary["relevant_count"]) == {"0", "1", "2", "6"}


def test_mrr_is_truncated_at_five_and_macro_averaged() -> None:
    summary: Any = summarize(
        [
            observation(
                "second",
                relevant=("a",),
                allowed=("a", "x"),
                retrieved=("x", "a"),
            ),
            observation(
                "sixth",
                relevant=("a",),
                allowed=("a", "b", "c", "d", "e", "f"),
                retrieved=("b", "c", "d", "e", "f", "a"),
            ),
        ],
        bootstrap_samples=50,
    )
    assert summary["overall"]["primary"]["mrr_at_5"]["value"] == 0.25
    assert summary["overall"]["primary"]["hit_at_5"]["value"] == 0.5
    assert "rank <= 5" in summary["definitions"]["mrr_at_5"]


def test_groups_include_denominators_proportions_failures_and_leakage() -> None:
    summary: Any = summarize(
        [
            observation(
                "success",
                relevant=("a",),
                retrieved=("a", "forbidden"),
                language="en",
                permission_group="restricted",
                latency_ms=20,
            ),
            observation(
                "failed",
                relevant=("a",),
                retrieved=("a",),
                language="zh",
                error="provider_error",
                latency_ms=100,
            ),
            observation(
                "empty-failed", relevant=(), language="mixed", error="provider_error", latency_ms=60
            ),
        ],
        bootstrap_samples=100,
    )
    assert summary["n_queries"] == 3
    assert summary["by_language"]["en"]["n"] == 1
    assert summary["by_language"]["en"]["proportion"] == pytest.approx(1 / 3)
    assert summary["by_permission"]["full"]["n"] == 2
    assert summary["overall"]["n_errors"] == 2
    assert summary["overall"]["n_unauthorized_results"] == 1
    assert summary["overall"]["n_queries_with_unauthorized_results"] == 1
    assert summary["overall"]["primary"]["recall_at_5"]["value"] == 0.5
    assert summary["overall"]["empty"]["correct_empty_response_rate"]["value"] == 0
    assert summary["overall"]["latency_ms"]["p50"]["value"] == 60
    assert summary["overall"]["latency_ms"]["p95"]["value"] == 90 + 6


def test_bootstrap_is_seeded_and_reports_null_for_missing_denominator() -> None:
    rows = [
        observation(str(i), relevant=("a",), retrieved=("a",) if i % 2 else (), latency_ms=i)
        for i in range(20)
    ]
    first: Any = summarize(rows, seed=17, bootstrap_samples=100)
    second = summarize(rows, seed=17, bootstrap_samples=100)
    assert first == second
    estimate = first["overall"]["primary"]["recall_at_5"]
    assert estimate["n"] == 20
    assert estimate["ci95"][0] <= estimate["value"] <= estimate["ci95"][1]
    assert first["overall"]["more_than_5"]["recall_at_5"] == {
        "value": None,
        "ci95": [None, None],
        "n": 0,
    }
    assert first["bootstrap"] == {
        "method": "bootstrap_percentile",
        "confidence": 0.95,
        "seed": 17,
        "samples": 100,
        "unit": "query",
    }


def test_empty_input_is_explicit_instead_of_manufacturing_perfect_scores() -> None:
    summary: Any = summarize([], bootstrap_samples=10)
    assert summary["n_queries"] == 0
    assert summary["overall"]["n"] == 0
    assert summary["overall"]["proportion"] == 0
    assert summary["overall"]["primary"]["recall_at_5"]["value"] is None
    assert summary["overall"]["latency_ms"]["p50"]["value"] is None
    assert summary["by_language"] == {}


@pytest.mark.parametrize("field", ["relevant_ids", "allowed_ids", "retrieved_ids"])
def test_duplicate_ids_are_rejected(field: str) -> None:
    values: dict[str, Any] = {
        "query_id": "q",
        "language": "zh",
        "permission_group": "full",
        "relevant_ids": ("a",),
        "allowed_ids": ("a",),
        "retrieved_ids": ("a",),
        "latency_ms": 1.0,
    }
    values[field] = ("a", "a")
    with pytest.raises(ValueError, match="duplicate"):
        summarize([Observation(**values)])


@pytest.mark.parametrize("latency_ms", [-1.0, float("nan"), float("inf")])
def test_invalid_latency_is_rejected(latency_ms: float) -> None:
    with pytest.raises(ValueError, match="latency"):
        summarize([observation("q", relevant=("a",), latency_ms=latency_ms)])


def test_bootstrap_requires_at_least_one_sample() -> None:
    with pytest.raises(ValueError, match="bootstrap_samples"):
        summarize([], bootstrap_samples=0)


def test_public_estimate_supports_accuracy_tokens_and_latency() -> None:
    accuracy = estimate([0.0, 1.0, 1.0], bootstrap_samples=50)
    latency = estimate([10.0, 30.0], statistic="p95", bootstrap_samples=50)
    assert accuracy["value"] == pytest.approx(2 / 3)
    assert accuracy["n"] == 3
    assert latency["value"] == 29
    assert estimate([], bootstrap_samples=50)["value"] is None
    with pytest.raises(ValueError, match="finite"):
        estimate([float("nan")])


def test_markdown_labels_synthetic_scope_and_all_groups() -> None:
    report: dict[str, object] = {
        "metadata": {
            "data_kind": "synthetic",
            "mode": "offline",
            "dataset_version": "synthetic-v1",
        },
        "runs": [
            {
                "configuration": {"method": "rrf", "examples": False},
                "metrics": summarize(
                    [
                        observation("q1", relevant=("a",), language="zh"),
                        observation(
                            "q2",
                            relevant=("a", "b", "c", "d", "e", "f"),
                            language="mixed",
                            permission_group="restricted",
                        ),
                        observation("q3", relevant=(), language="en"),
                    ],
                    bootstrap_samples=10,
                ),
            }
        ],
    }
    rendered = render_markdown(report)
    assert "合成数据" in rendered
    assert "不代表真实质量" in rendered
    assert "质量验收延期" in rendered
    assert "MRR@5" in rendered
    assert "Recall@5" in rendered
    assert "95%" in rendered
    assert "mixed" in rendered
    assert "restricted" in rendered
    assert "False recommendation" in rendered
    assert "p95" in rendered
    assert "theoretical" in rendered
    assert "denominator" in rendered


def test_markdown_escapes_dynamic_table_cells() -> None:
    report: dict[str, object] = {
        "metadata": {"data_kind": "synthetic", "mode": "offline"},
        "runs": [
            {
                "configuration": {"method": "bm25", "examples": False},
                "metrics": summarize(
                    [observation("q", relevant=("a",), language="zh|<img>\n")],
                    bootstrap_samples=10,
                ),
            }
        ],
    }
    rendered = render_markdown(report)
    assert "zh\\|&lt;img&gt; " in rendered
    assert "<img>" not in rendered


def test_real_provider_report_keeps_synthetic_limit_without_offline_title() -> None:
    report: dict[str, object] = {"metadata": {"mode": "real"}, "runs": []}
    rendered = render_markdown(report)
    assert "合成数据" in rendered
    assert "不代表真实质量" in rendered
    assert "离线回归基线" not in rendered


def test_knee_report_separates_measured_and_estimated_tokens_with_denominators() -> None:
    report: dict[str, object] = {
        "synthetic": True,
        "seed": 17,
        "protocol": "Paired catalogs; retrieval included.",
        "token_scope": "Model usage and estimated prompt are separate.",
        "runs": [
            {
                "tool_count": size,
                "arm": arm,
                "n": 2,
                "accuracy": estimate([0.0, 1.0], bootstrap_samples=10),
                "n_errors": 1,
                "latency_ms": {
                    "p50": estimate([10.0, 30.0], statistic="p50", bootstrap_samples=10),
                    "p95": estimate([10.0, 30.0], statistic="p95", bootstrap_samples=10),
                },
                "candidate_tools": {"min": 5 if arm == "search_first" else size, "max": size},
                "mean_reported_tokens": {
                    "prompt_tokens": estimate([1000.0] if measured else [], bootstrap_samples=10),
                    "completion_tokens": estimate([2.0] if measured else [], bootstrap_samples=10),
                    "total_tokens": estimate([1002.0] if measured else [], bootstrap_samples=10),
                },
                "mean_estimated_prompt_tokens": estimate(
                    [] if measured else [200.0], bootstrap_samples=10
                ),
            }
            for size, arm, measured in ((20, "full_load", True), (500, "search_first", False))
        ],
    }
    rendered = render_knee_markdown(report)
    assert "合成数据" in rendered
    assert "质量验收延期" in rendered
    assert "Paired catalogs; retrieval included." in rendered
    assert "95%" in rendered
    assert "| 20 | full_load |" in rendered
    assert "| 500 | search_first |" in rendered
    assert "Accuracy" in rendered
    assert "Errors" in rendered
    assert "p95" in rendered
    assert "Provider-reported" in rendered
    assert "Mock-estimated" in rendered
    assert "UTF-8 bytes/4" in rendered
    assert "1000.0000" in rendered
    assert "200.0000" in rendered
    assert "N/A" in rendered
    assert "n=1" in rendered

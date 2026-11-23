"""Paired deltas respect duplicate-text dependence and preserve every query."""

from __future__ import annotations

from dataclasses import replace

import pytest

from semantic_eval.dataset import generate_dataset
from semantic_eval.tuning import grouped_paired_delta


def test_text_group_bootstrap_keeps_replicates_together_and_macro_point_estimate() -> None:
    query = generate_dataset().queries[3]
    replicated = tuple(replace(query, id=f"replica-{index}") for index in range(50))
    other = replace(query, id="other", text="A distinct intent")
    queries = (*replicated, other)
    baseline = {item.id: 0.0 for item in queries}
    candidate = {item.id: float(item.id != "other") for item in queries}
    result = grouped_paired_delta(queries, baseline, candidate, bootstrap_samples=1000)
    assert result["value"] == 50 / 51
    assert result["ci95"] == [0.0, 1.0]
    assert result["n_queries"] == 51
    assert result["n_text_groups"] == 2


def test_delta_is_paired_seeded_and_order_independent() -> None:
    queries = generate_dataset().queries[:12]
    baseline = {query.id: index / 11 for index, query in enumerate(queries)}
    candidate = {query.id: baseline[query.id] + 0.1 for query in queries}
    result = grouped_paired_delta(queries, baseline, candidate)
    assert result == grouped_paired_delta(tuple(reversed(queries)), baseline, candidate)
    assert result["value"] == pytest.approx(0.1)
    assert result["ci95"] == pytest.approx([0.1, 0.1])
    assert result["bootstrap"]["seed"] == 20261011
    assert result["bootstrap"]["unit"] == "normalized_query_text_group"


def test_empty_cohort_reports_unknown_estimates_and_no_groups() -> None:
    result = grouped_paired_delta((), {}, {})
    assert result["value"] is None
    assert result["ci95"] == [None, None]
    assert result["n_queries"] == result["n_text_groups"] == 0


def test_delta_rejects_unpaired_ids_duplicate_queries_and_nonfinite_values() -> None:
    query = generate_dataset().queries[3]
    with pytest.raises(ValueError, match="exact"):
        grouped_paired_delta((query,), {query.id: 0.0}, {"other": 1.0})
    with pytest.raises(ValueError, match="duplicate"):
        grouped_paired_delta((query, query), {query.id: 0.0}, {query.id: 1.0})
    with pytest.raises(ValueError, match="finite"):
        grouped_paired_delta((query,), {query.id: 0.0}, {query.id: float("inf")})


@pytest.mark.parametrize("samples", [0, -1, True])
def test_delta_rejects_invalid_bootstrap_sample_count(samples: int) -> None:
    with pytest.raises(ValueError, match="bootstrap"):
        grouped_paired_delta((), {}, {}, bootstrap_samples=samples)

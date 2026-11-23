"""Freeze grouped primary-cohort dev/test allocation before looking at tuning results."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace

import pytest

from semantic_eval.dataset import Dataset, EvalQuery, generate_dataset
from semantic_eval.tuning import (
    DEFAULT_SPLIT_SEED,
    paired_knee_queries,
    query_group_id,
    split_dataset,
)


def _cohorts(queries: tuple[EvalQuery, ...]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for query in queries:
        relevant = len(set(query.allowed_ids) & set(query.relevant_ids))
        counts["empty" if relevant == 0 else "m17" if relevant <= 5 else "more_than_5"] += 1
    return counts


def test_split_accounts_for_every_query_without_id_or_text_group_leakage() -> None:
    original = generate_dataset()
    dev, test, manifest = split_dataset(original)
    dev_ids = {query.id for query in dev.queries}
    test_ids = {query.id for query in test.queries}
    assert dev_ids.isdisjoint(test_ids)
    assert dev_ids | test_ids == {query.id for query in original.queries}
    assert {query_group_id(query.text) for query in dev.queries}.isdisjoint(
        query_group_id(query.text) for query in test.queries
    )
    assert dev.tools == test.tools == original.tools
    assert len(dev.queries) + len(test.queries) == len(original.queries)
    assert manifest["seed"] == DEFAULT_SPLIT_SEED == 20261011
    assert manifest["quality_acceptance"] is False
    assert manifest["dev_query_ids"] == [query.id for query in dev.queries]
    assert manifest["test_query_ids"] == [query.id for query in test.queries]
    assert json.loads(json.dumps(manifest)) == manifest


def test_split_is_deterministic_independent_of_input_order_and_seeded() -> None:
    original = generate_dataset()
    first = split_dataset(original)
    assert split_dataset(original) == first
    reordered = replace(original, queries=tuple(reversed(original.queries)))
    assert split_dataset(reordered) == first
    assert split_dataset(original, seed=DEFAULT_SPLIT_SEED + 1)[0] != first[0]
    dev, test, manifest = first
    assert len(str(manifest["dev_dataset_sha256"])) == 64
    assert len(str(manifest["test_dataset_sha256"])) == 64
    assert manifest["dev_dataset_sha256"] != manifest["test_dataset_sha256"]
    assert list(dev.queries) == sorted(dev.queries, key=lambda query: query.id)
    assert list(test.queries) == sorted(test.queries, key=lambda query: query.id)


def test_split_preserves_m17_empty_and_large_cohorts_and_discloses_rare_strata() -> None:
    dev, test, manifest = split_dataset(generate_dataset())
    for partition in (dev, test):
        assert set(_cohorts(partition.queries)) == {"m17", "empty", "more_than_5"}
        assert {query.language for query in partition.queries} == {"zh", "en", "mixed"}
        assert {query.permission_group for query in partition.queries} == {
            "0%",
            "1%",
            "10%",
            "100%",
        }
    strata = manifest["strata"]
    assert isinstance(strata, list)
    assert len(strata) == 51
    for stratum in strata:
        assert stratum["dev_count"] + stratum["test_count"] == stratum["source_count"]
        assert stratum["test_deviation"] == (stratum["test_count"] - stratum["target_test_count"])
        assert stratum["authorized_relevant_bucket"] in range(7)
        if not stratum["dev_count"] or not stratum["test_count"]:
            assert stratum["deviation_reason"]
    outside = [
        stratum
        for stratum in strata
        if stratum["permission_group"] == "100%" and stratum["authorized_relevant_bucket"] == 0
    ]
    assert len(outside) == 3
    assert all(stratum["text_group_count"] == 1 for stratum in outside)
    assert all(stratum["deviation_reason"] == "indivisible_text_group" for stratum in outside)


def test_normalized_duplicate_text_stays_together_across_distinct_ids() -> None:
    original = generate_dataset()
    query = original.queries[3]
    duplicate = replace(query, id="query-duplicate", text=" \t" + query.text.upper() + "\n")
    expanded = replace(original, queries=(*original.queries, duplicate))
    dev, test, _ = split_dataset(expanded)
    assert query_group_id(query.text) == query_group_id(duplicate.text)
    assert any(
        query in partition.queries and duplicate in partition.queries for partition in (dev, test)
    )


def test_knee_uses_only_test_singletons_and_pairs_exact_queries_across_all_sizes() -> None:
    dev, test, _ = split_dataset(generate_dataset())
    chosen = paired_knee_queries(test)
    assert chosen == paired_knee_queries(test)
    assert chosen == paired_knee_queries(replace(test, queries=tuple(reversed(test.queries))))
    assert 1 <= len(chosen) <= 30
    assert len({query_group_id(query.text) for query in chosen}) == len(chosen)
    available = {
        query_group_id(query.text)
        for query in test.queries
        if len(query.relevant_ids) == 1 and set(query.relevant_ids) <= set(query.allowed_ids)
    }
    assert len(chosen) == min(30, len(available))
    assert set(query.id for query in chosen) <= {query.id for query in test.queries}
    assert {query_group_id(query.text) for query in chosen}.isdisjoint(
        query_group_id(query.text) for query in dev.queries
    )
    for query in chosen:
        assert len(query.relevant_ids) == 1
        assert set(query.relevant_ids) <= set(query.allowed_ids)
    assert {query.language for query in chosen} == {"zh", "en", "mixed"}
    # A caller reuses these same queries for each size and each arm.
    assert len(chosen) * len((20, 50, 100, 200, 500)) * 3 <= 450


@pytest.mark.parametrize("count", [0, 31, True])
def test_knee_rejects_counts_outside_the_fixed_three_arm_call_budget(count: int) -> None:
    with pytest.raises(ValueError):
        paired_knee_queries(generate_dataset(), count=count)


def test_knee_rejects_no_authorized_singleton_truth() -> None:
    original = generate_dataset()
    empty = replace(original, queries=())
    with pytest.raises(ValueError, match="singleton"):
        paired_knee_queries(empty)


@pytest.mark.parametrize("fraction", [0, 1, -0.1, float("nan"), True])
def test_split_rejects_invalid_fractions(fraction: float) -> None:
    with pytest.raises(ValueError):
        split_dataset(generate_dataset(), test_fraction=fraction)


def test_split_rejects_duplicate_ids_or_insufficient_text_groups() -> None:
    original = generate_dataset()
    with pytest.raises(ValueError, match="duplicate"):
        split_dataset(replace(original, queries=(*original.queries, original.queries[0])))
    with pytest.raises(ValueError, match="text groups"):
        split_dataset(Dataset(original.seed, original.tools, (original.queries[0],)))

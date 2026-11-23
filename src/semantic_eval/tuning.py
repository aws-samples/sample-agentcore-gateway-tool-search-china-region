"""Deterministic metadata-only tuning splits for the synthetic regression corpus.

This is a partition of previously observed synthetic data, not a new human
holdout. Text groups stay intact even when that prevents exact primary-cohort stratification.
No retrieval result or provider score is consulted by this module.
"""

from __future__ import annotations

import hashlib
import math
import random
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict

import numpy as np

from semantic_core.catalog import canonical_json
from semantic_eval.dataset import Dataset, EvalQuery

DEFAULT_SPLIT_SEED = 20261011
_Stratum = tuple[str, str, int]


def query_group_id(text: str) -> str:
    """Identify equivalent case, Unicode and whitespace variants across Gateways."""
    normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _stratum(query: EvalQuery) -> _Stratum:
    count = len(set(query.relevant_ids) & set(query.allowed_ids))
    return query.language, query.permission_group, min(count, 6)


def _dataset_hash(data: Dataset) -> str:
    return hashlib.sha256(canonical_json(asdict(data))).hexdigest()


def _cohorts(queries: tuple[EvalQuery, ...]) -> dict[str, int]:
    counts = {"m17": 0, "empty": 0, "more_than_5": 0}
    for query in queries:
        count = _stratum(query)[2]
        counts["empty" if count == 0 else "m17" if count <= 5 else "more_than_5"] += 1
    return counts


def _allocate(
    groups: dict[str, tuple[EvalQuery, ...]],
    totals: Counter[_Stratum],
    fraction: float,
    seed: int,
) -> set[str]:
    """Minimize normalized stratum and total count deviation using text groups.

    Seeded restarts and local flips use metadata only. Keeping a group intact
    takes priority over balance; all residual deviations are included in output.
    """
    group_counts = {
        key: Counter(_stratum(query) for query in queries) for key, queries in groups.items()
    }
    total = sum(totals.values())
    target = total * fraction
    targets = {key: value * fraction for key, value in totals.items()}
    rng = random.Random(seed)  # nosec B311
    best: set[str] | None = None
    best_cost = math.inf
    chosen: set[str] = set()
    counts: Counter[_Stratum] = Counter()
    chosen_count = 0

    def delta(key: str, direction: int) -> float:
        size = direction * len(groups[key])
        result = ((chosen_count + size - target) ** 2 - (chosen_count - target) ** 2) / total
        for stratum, amount in group_counts[key].items():
            before = counts[stratum] - targets[stratum]
            result += ((before + direction * amount) ** 2 - before**2) / totals[stratum]
        return result

    def flip(key: str, direction: int) -> None:
        nonlocal chosen_count
        if direction == 1:
            chosen.add(key)
        else:
            chosen.remove(key)
        chosen_count += direction * len(groups[key])
        for stratum, amount in group_counts[key].items():
            counts[stratum] += direction * amount

    for _ in range(24):
        order = sorted(groups)
        rng.shuffle(order)
        # Larger indivisible groups are allocated first; shuffled ties use the seed.
        order.sort(key=lambda key: -len(groups[key]))
        chosen = set()
        counts = Counter()
        chosen_count = 0
        for key in order:
            if delta(key, 1) < 0:
                flip(key, 1)
        changed = True
        while changed:
            changed = False
            rng.shuffle(order)
            for key in order:
                direction = -1 if key in chosen else 1
                if delta(key, direction) < -1e-12:
                    flip(key, direction)
                    changed = True
        cost = (chosen_count - target) ** 2 / total + sum(
            (counts[key] - targets[key]) ** 2 / totals[key] for key in totals
        )
        if chosen and len(chosen) < len(groups) and cost < best_cost:
            best, best_cost = chosen, cost
    if best is None:
        # Extremely small fractions can favor an empty partition mathematically.
        # A split still requires at least one whole text group in each partition.
        first = min(groups, key=lambda key: (len(groups[key]), key))
        best = {first}
    return best


def split_dataset(
    data: Dataset,
    *,
    seed: int = DEFAULT_SPLIT_SEED,
    test_fraction: float = 0.5,
) -> tuple[Dataset, Dataset, dict[str, object]]:
    """Return ``(dev, test, manifest)`` with tools retained in both partitions.

    The fixed primary-cohort stratum is language, permission group, and authorized relevant
    cardinality capped at six (six represents more than five). Output ordering
    and hashes are independent of input query order. The JSON-safe manifest is
    intended to be persisted before the first live dev result is observed.
    """
    if type(seed) is not int:
        raise ValueError("split seed must be an integer")
    if (
        isinstance(test_fraction, bool)
        or not math.isfinite(test_fraction)
        or not 0 < test_fraction < 1
    ):
        raise ValueError("test_fraction must be finite and between zero and one")
    queries = tuple(sorted(data.queries, key=lambda query: query.id))
    if len({query.id for query in queries}) != len(queries):
        raise ValueError("duplicate query IDs")
    grouped: dict[str, list[EvalQuery]] = defaultdict(list)
    for query in queries:
        grouped[query_group_id(query.text)].append(query)
    if len(grouped) < 2:
        raise ValueError("split requires at least two distinct query text groups")
    groups = {key: tuple(value) for key, value in sorted(grouped.items())}
    totals = Counter(_stratum(query) for query in queries)
    selected = _allocate(groups, totals, test_fraction, seed)
    test_queries = tuple(query for query in queries if query_group_id(query.text) in selected)
    dev_queries = tuple(query for query in queries if query_group_id(query.text) not in selected)
    dev = Dataset(data.seed, data.tools, dev_queries)
    test = Dataset(data.seed, data.tools, test_queries)
    dev_counts = Counter(_stratum(query) for query in dev_queries)
    test_counts = Counter(_stratum(query) for query in test_queries)
    strata: list[dict[str, object]] = []
    for key in sorted(totals):
        text_groups = {query_group_id(query.text) for query in queries if _stratum(query) == key}
        target = totals[key] * test_fraction
        deviation = test_counts[key] - target
        strata.append(
            {
                "language": key[0],
                "permission_group": key[1],
                "authorized_relevant_bucket": key[2],
                "source_count": totals[key],
                "text_group_count": len(text_groups),
                "dev_count": dev_counts[key],
                "test_count": test_counts[key],
                "target_test_count": target,
                "test_deviation": deviation,
                "deviation_reason": (
                    "indivisible_text_group"
                    if len(text_groups) == 1
                    else "grouped_stratification_or_integer_rounding"
                    if deviation
                    else None
                ),
            }
        )
    manifest: dict[str, object] = {
        "schema_version": "synthetic-tuning-split-v1",
        "synthetic": True,
        "quality_acceptance": False,
        "notice": "合成数据，不代表质量验收",
        "seed": seed,
        "dataset_seed": data.seed,
        "test_fraction": test_fraction,
        "stratification": ["language", "permission_group", "min(authorized_relevant_count,6)"],
        "grouping": "SHA256(NFKC query text, casefolded, whitespace collapsed); ignore Gateway",
        "allocation": "24 seeded metadata-only greedy restarts with improving single-group flips",
        "source_dataset_sha256": _dataset_hash(Dataset(data.seed, data.tools, queries)),
        "dev_dataset_sha256": _dataset_hash(dev),
        "test_dataset_sha256": _dataset_hash(test),
        "source_query_count": len(queries),
        "dev_query_ids": [query.id for query in dev_queries],
        "test_query_ids": [query.id for query in test_queries],
        "dev_text_group_ids": sorted(set(groups) - selected),
        "test_text_group_ids": sorted(selected),
        "cohorts": {"dev": _cohorts(dev_queries), "test": _cohorts(test_queries)},
        "strata": strata,
        "limitations": [
            "The source fixture and earlier synthetic results were already observed; this is "
            "not a previously unseen or human-labeled holdout.",
            "Text grouping prevents normalized duplicate leakage, but shared templates, "
            "semantic intents and the tool catalog remain shared across partitions.",
            "Indivisible text groups take priority over exact primary-cohort stratum balance; each "
            "deviation and any absent stratum are reported explicitly.",
        ],
    }
    return dev, test, manifest


def paired_knee_queries(
    test: Dataset,
    *,
    count: int = 30,
    seed: int = DEFAULT_SPLIT_SEED,
) -> tuple[EvalQuery, ...]:
    """Choose up to 30 distinct test text groups, paired across sizes and arms.

    Callers pass only the held-out test Dataset and reuse this exact tuple for
    20/50/100/200/500 catalogs and three arms (at most 450 selection chat calls).
    Singleton *semantic* truth ensures unambiguous identity after catalog remapping.
    """
    if type(count) is not int or not 1 <= count <= 30:
        raise ValueError("knee count must be 1..30 for the fixed three-arm budget")
    if type(seed) is not int:
        raise ValueError("knee seed must be an integer")
    eligible = [
        query
        for query in sorted(test.queries, key=lambda query: query.id)
        if len(query.relevant_ids) == 1 and set(query.relevant_ids) <= set(query.allowed_ids)
    ]
    rng = random.Random(seed)  # nosec B311
    rng.shuffle(eligible)
    by_language: dict[str, list[EvalQuery]] = defaultdict(list)
    seen: set[str] = set()
    for query in eligible:
        group = query_group_id(query.text)
        if group not in seen:
            by_language[query.language].append(query)
            seen.add(group)
    output: list[EvalQuery] = []
    while len(output) < count and any(by_language.values()):
        for language in sorted(by_language):
            if by_language[language] and len(output) < count:
                output.append(by_language[language].pop())
    if not output:
        raise ValueError("knee experiment requires singleton authorized truth")
    return tuple(output)


def grouped_paired_delta(
    queries: Sequence[EvalQuery],
    baseline: Mapping[str, float],
    candidate: Mapping[str, float],
    *,
    seed: int = DEFAULT_SPLIT_SEED,
    bootstrap_samples: int = 2000,
) -> dict[str, object]:
    """Return candidate-minus-baseline query-macro change and a paired group CI.

    Resample normalized query text groups, retaining all query pairs in a sampled
    group; each replicate divides its summed query deltas by its query count.
    Thus differently sized groups retain the declared query-macro estimand.
    These descriptive intervals do not adjust for dev model selection or shared
    templates, and cannot establish performance on a real-world population.
    """
    if type(seed) is not int:
        raise ValueError("bootstrap seed must be an integer")
    if type(bootstrap_samples) is not int or bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be a positive integer")
    identifiers = {query.id for query in queries}
    if len(identifiers) != len(queries):
        raise ValueError("duplicate query IDs")
    if set(baseline) != identifiers or set(candidate) != identifiers:
        raise ValueError("paired values require exact query ID coverage")
    if any(
        not math.isfinite(value) for values in (baseline, candidate) for value in values.values()
    ):
        raise ValueError("paired values must be finite")
    groups: dict[str, list[float]] = defaultdict(list)
    for query in sorted(queries, key=lambda query: query.id):
        groups[query_group_id(query.text)].append(candidate[query.id] - baseline[query.id])
    result: dict[str, object] = {
        "value": None,
        "ci95": [None, None],
        "n_queries": len(queries),
        "n_text_groups": len(groups),
        "bootstrap": {
            "method": "paired_cluster_percentile",
            "unit": "normalized_query_text_group",
            "confidence": 0.95,
            "samples": bootstrap_samples,
            "seed": seed,
            "estimand": "query-macro candidate minus baseline",
        },
    }
    if not groups:
        return result
    sums = np.asarray([sum(groups[key]) for key in sorted(groups)], dtype=np.float64)
    counts = np.asarray([len(groups[key]) for key in sorted(groups)], dtype=np.int64)
    sampled = np.random.default_rng(seed).integers(
        len(groups), size=(bootstrap_samples, len(groups))
    )
    replicates = sums[sampled].sum(axis=1) / counts[sampled].sum(axis=1)
    result["value"] = float(sums.sum() / counts.sum())
    result["ci95"] = [
        float(value) for value in np.quantile(replicates, [0.025, 0.975], method="linear")
    ]
    return result

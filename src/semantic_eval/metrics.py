"""Query-level retrieval metrics for the design's primary-cohort denominators."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict

import numpy as np


@dataclass(frozen=True, slots=True)
class Observation:
    """One attempted query; relevance is intersected with the caller's ACL.

    ``error`` marks an unsuccessful query, which contributes zero retrieval
    quality while retaining its observed latency. Raw results are still checked
    for authorization leaks, even when an error was recorded.
    """

    query_id: str
    language: str
    permission_group: str
    relevant_ids: tuple[str, ...]
    allowed_ids: tuple[str, ...]
    retrieved_ids: tuple[str, ...]
    latency_ms: float
    error: str | None = None


class Estimate(TypedDict):
    value: float | None
    ci95: list[float | None]
    n: int


@dataclass(frozen=True, slots=True)
class _Row:
    observation: Observation
    relevant_count: int
    recall: float
    hit: float
    reciprocal_rank: float
    ceiling: float
    false_recommendation: float
    correct_empty_response: float
    unauthorized_count: int


def _row(observation: Observation) -> _Row:
    for field in ("relevant_ids", "allowed_ids", "retrieved_ids"):
        identifiers: tuple[str, ...] = getattr(observation, field)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(f"duplicate tool IDs in {field} for query {observation.query_id}")
    if not math.isfinite(observation.latency_ms) or observation.latency_ms < 0:
        raise ValueError("latency_ms must be finite and nonnegative")
    authorized = set(observation.allowed_ids)
    relevant = set(observation.relevant_ids) & authorized
    retrieved = observation.retrieved_ids[:5] if observation.error is None else ()
    hits = set(retrieved) & relevant
    first_rank = next(
        (rank for rank, tool_id in enumerate(retrieved, start=1) if tool_id in relevant), None
    )
    count = len(relevant)
    return _Row(
        observation=observation,
        relevant_count=count,
        recall=len(hits) / count if count else 0.0,
        hit=float(bool(hits)),
        reciprocal_rank=1 / first_rank if first_rank is not None else 0.0,
        ceiling=min(5 / count, 1.0) if count else 0.0,
        false_recommendation=float(bool(retrieved)),
        correct_empty_response=float(not retrieved and observation.error is None),
        unauthorized_count=sum(
            identifier not in authorized for identifier in observation.retrieved_ids
        ),
    )


def _estimate(
    values: Sequence[float],
    rng: np.random.Generator,
    bootstrap_samples: int,
    statistic: Literal["mean", "p50", "p95"] = "mean",
) -> Estimate:
    count = len(values)
    if not count:
        return {"value": None, "ci95": [None, None], "n": 0}
    array = np.asarray(values, dtype=np.float64)
    if statistic == "mean":
        value = float(array.mean())
    else:
        value = float(np.quantile(array, 0.5 if statistic == "p50" else 0.95, method="linear"))
    if count == 1 or np.all(array == array[0]):
        return {"value": value, "ci95": [value, value], "n": count}
    # Resample whole query observations within this metric's declared cohort.
    samples = rng.choice(array, size=(bootstrap_samples, count), replace=True)
    if statistic == "mean":
        statistics = samples.mean(axis=1)
    else:
        statistics = np.quantile(
            samples, 0.5 if statistic == "p50" else 0.95, axis=1, method="linear"
        )
    bounds = np.quantile(statistics, [0.025, 0.975], method="linear")
    return {"value": value, "ci95": [float(bounds[0]), float(bounds[1])], "n": count}


def estimate(
    values: Sequence[float],
    *,
    statistic: Literal["mean", "p50", "p95"] = "mean",
    seed: int = 20261010,
    bootstrap_samples: int = 1000,
) -> dict[str, object]:
    """Estimate a mean or percentile with a deterministic sample-bootstrap CI."""
    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be at least 1")
    if statistic not in ("mean", "p50", "p95"):
        raise ValueError("statistic must be mean, p50 or p95")
    if any(not math.isfinite(value) for value in values):
        raise ValueError("estimate values must be finite")
    result = _estimate(values, np.random.default_rng(seed), bootstrap_samples, statistic)
    return {"value": result["value"], "ci95": result["ci95"], "n": result["n"]}


def _cohort(
    rows: Sequence[_Row],
    group_count: int,
    rng: np.random.Generator,
    bootstrap_samples: int,
    *,
    include_ceiling: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "n": len(rows),
        "proportion": len(rows) / group_count if group_count else 0.0,
        "recall_at_5": _estimate([row.recall for row in rows], rng, bootstrap_samples),
        "hit_at_5": _estimate([row.hit for row in rows], rng, bootstrap_samples),
        "mrr_at_5": _estimate([row.reciprocal_rank for row in rows], rng, bootstrap_samples),
    }
    if include_ceiling:
        result["theoretical_recall_ceiling"] = _estimate(
            [row.ceiling for row in rows], rng, bootstrap_samples
        )
    return result


def _group(
    rows: Sequence[_Row],
    total_count: int,
    rng: np.random.Generator,
    bootstrap_samples: int,
) -> dict[str, object]:
    count = len(rows)
    empty = [row for row in rows if row.relevant_count == 0]
    latencies = [row.observation.latency_ms for row in rows]
    return {
        "n": count,
        "proportion": count / total_count if total_count else 0.0,
        "n_errors": sum(row.observation.error is not None for row in rows),
        "n_unauthorized_results": sum(row.unauthorized_count for row in rows),
        "n_queries_with_unauthorized_results": sum(row.unauthorized_count > 0 for row in rows),
        "error_rate": _estimate(
            [float(row.observation.error is not None) for row in rows], rng, bootstrap_samples
        ),
        "unauthorized_query_rate": _estimate(
            [float(row.unauthorized_count > 0) for row in rows], rng, bootstrap_samples
        ),
        "latency_ms": {
            "p50": _estimate(latencies, rng, bootstrap_samples, "p50"),
            "p95": _estimate(latencies, rng, bootstrap_samples, "p95"),
        },
        "primary": _cohort(
            [row for row in rows if 1 <= row.relevant_count <= 5], count, rng, bootstrap_samples
        ),
        "more_than_5": _cohort(
            [row for row in rows if row.relevant_count > 5],
            count,
            rng,
            bootstrap_samples,
            include_ceiling=True,
        ),
        "empty": {
            "n": len(empty),
            "proportion": len(empty) / count if count else 0.0,
            "false_recommendation_rate": _estimate(
                [row.false_recommendation for row in empty], rng, bootstrap_samples
            ),
            "correct_empty_response_rate": _estimate(
                [row.correct_empty_response for row in empty], rng, bootstrap_samples
            ),
        },
    }


def summarize(
    observations: Sequence[Observation],
    *,
    seed: int = 20261010,
    bootstrap_samples: int = 1000,
) -> dict[str, object]:
    """Return macro metrics, primary-cohort cohorts and query-bootstrap percentile intervals.

    Empty cohorts have null estimates and intervals rather than perfect scores.
    Percentile intervals describe variation over this supplied query sample;
    they do not correct synthetic-label bias or establish a quality acceptance.
    """
    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be at least 1")
    rows = [_row(observation) for observation in observations]
    if len({row.observation.query_id for row in rows}) != len(rows):
        raise ValueError("duplicate query IDs")
    rng = np.random.default_rng(seed)
    count = len(rows)
    by_language: dict[str, list[_Row]] = defaultdict(list)
    by_permission: dict[str, list[_Row]] = defaultdict(list)
    by_count: dict[int, list[_Row]] = defaultdict(list)
    for row in rows:
        by_language[row.observation.language].append(row)
        by_permission[row.observation.permission_group].append(row)
        by_count[row.relevant_count].append(row)
    return {
        "n_queries": count,
        "bootstrap": {
            "method": "bootstrap_percentile",
            "confidence": 0.95,
            "seed": seed,
            "samples": bootstrap_samples,
            "unit": "query",
        },
        "definitions": {
            "relevant_authorized": "set(relevant_ids) intersect set(allowed_ids)",
            "recall_at_5": (
                "Macro |top5 intersect relevant_authorized| / |relevant_authorized|; "
                "primary denominator contains only queries with 1..5 authorized relevant tools"
            ),
            "hit_at_5": "Macro indicator of at least one authorized relevant tool in top5",
            "mrr_at_5": (
                "Truncated MRR@5: reciprocal first authorized relevant rank when rank <= 5, "
                "otherwise 0; macro average within the declared nonempty cohort"
            ),
            "false_recommendation_rate": (
                "Fraction of zero-authorized-relevant queries returning any top5 result; "
                "request errors are also reported separately and never counted as correct empty"
            ),
            "correct_empty_response_rate": (
                "Fraction of zero-authorized-relevant queries with no result and no request error; "
                "this retrieval metric does not measure an Agent's refusal quality"
            ),
            "errors": "Retained in denominators and latency; treated as zero retrieval quality",
            "unauthorized_results": (
                "Counts every returned tool outside allowed_ids, including results after rank 5"
            ),
            "proportion": (
                "Language/permission/count group proportions use all queries; "
                "primary/more_than_5/empty proportions use their containing group"
            ),
            "confidence_interval": (
                "Seeded query-level bootstrap, percentile 95% intervals; linear sample quantiles; "
                "single-query and constant samples yield degenerate intervals; "
                "intervals do not remove synthetic-label bias"
            ),
        },
        "overall": _group(rows, count, rng, bootstrap_samples),
        "by_language": {
            key: _group(by_language[key], count, rng, bootstrap_samples)
            for key in sorted(by_language)
        },
        "by_permission": {
            key: _group(by_permission[key], count, rng, bootstrap_samples)
            for key in sorted(by_permission)
        },
        "relevant_count": {
            str(key): _group(by_count[key], count, rng, bootstrap_samples)
            for key in sorted(by_count)
        },
    }

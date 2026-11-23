"""Render dev selection and frozen test evidence without any provider calls.

Run with PYTHONPATH=src and --phase dev before reviewing final test output.
Detailed JSON retains every primary-cohort/empty/>5 cohort and language/permission/count
comparison. Paired CIs resample normalized query text groups, not duplicate IDs.
"""

from __future__ import annotations

import argparse
import html
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import numpy as np

from semantic_eval.dataset import EvalQuery, load_dataset
from semantic_eval.tuning import (
    grouped_paired_delta,
    paired_knee_queries,
    query_group_id,
    split_dataset,
)

ROOT = Path(__file__).resolve().parents[2]
NOTICE = "**合成数据，不代表质量验收。Rerank 默认保持关闭。**"
SEED = 20261011
SAMPLES = 2000
Run = dict[str, Any]
Row = dict[str, Any]


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("report input must be a JSON object")
    return cast(dict[str, Any], value)


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _fmt(value: object, *, scale: float = 1, digits: int = 1) -> str:
    return "n/a" if value is None else f"{float(cast(float, value)) * scale:.{digits}f}"


def _ci(value: Mapping[str, Any], *, scale: float = 100) -> str:
    low, high = value["ci95"]
    return (
        f"{_fmt(value['value'], scale=scale)} [{_fmt(low, scale=scale)}, {_fmt(high, scale=scale)}]"
    )


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    def line(values: Sequence[object]) -> str:
        return "| " + " | ".join(str(value).replace("|", "\\|") for value in values) + " |"

    return "\n".join([line(headers), line(["---"] * len(headers)), *(line(row) for row in rows)])


def _index(run: Run, queries: Sequence[EvalQuery]) -> dict[str, Row]:
    rows: list[Row] = run["observations"]
    indexed = {row["query_id"]: row for row in rows}
    if len(indexed) != len(rows) or set(indexed) != {query.id for query in queries}:
        raise ValueError("comparison requires unique, exactly paired query IDs")
    for query in queries:
        row = indexed[query.id]
        if (
            row["language"] != query.language
            or row["permission_group"] != query.permission_group
            or tuple(row["relevant_ids"]) != query.relevant_ids
            or tuple(row["allowed_ids"]) != query.allowed_ids
        ):
            raise ValueError("observation labels differ from frozen dataset")
        if len(set(row["retrieved_ids"])) != len(row["retrieved_ids"]):
            raise ValueError("duplicate retrieved IDs")
    return indexed


def _count(query: EvalQuery) -> int:
    return len(set(query.relevant_ids) & set(query.allowed_ids))


def empty_breakdown(run: Run, queries: Sequence[EvalQuery]) -> dict[str, Any]:
    """Keep trivial empty ACLs separate from the two real rejection tasks."""
    indexed = _index(run, queries)
    empty = [query for query in queries if _count(query) == 0]
    cohorts = {
        "all_empty": empty,
        "empty_acl": [query for query in empty if not query.allowed_ids],
        "nonempty_acl_empty": [query for query in empty if query.allowed_ids],
        "outside_catalog": [
            query for query in empty if query.allowed_ids and not query.relevant_ids
        ],
        "permission_negative": [
            query for query in empty if query.allowed_ids and query.relevant_ids
        ],
    }
    result: dict[str, Any] = {}
    for name, selected in cohorts.items():
        false = sum(
            bool(indexed[query.id]["retrieved_ids"]) and indexed[query.id]["error"] is None
            for query in selected
        )
        result[name] = {
            "n": len(selected),
            "unique_texts": len({query_group_id(query.text) for query in selected}),
            "false_recommendations": false,
            "false_rate": false / len(selected) if selected else None,
            "errors": sum(indexed[query.id]["error"] is not None for query in selected),
            "correct_empty": sum(
                not indexed[query.id]["retrieved_ids"] and indexed[query.id]["error"] is None
                for query in selected
            ),
        }
    return result


def _empty_table(runs: Sequence[Run], queries: Sequence[EvalQuery]) -> str:
    rows = []
    for run in runs:
        for cohort, values in empty_breakdown(run, queries).items():
            rows.append(
                [
                    run["name"],
                    cohort,
                    values["n"],
                    values["unique_texts"],
                    values["false_recommendations"],
                    _fmt(values["false_rate"], scale=100),
                    values["errors"],
                ]
            )
    return _table(("Arm", "Empty cohort", "n", "Unique texts", "False", "False %", "Errors"), rows)


def _value(row: Row, query: EvalQuery, metric: str) -> float:
    relevant = set(query.relevant_ids) & set(query.allowed_ids)
    retrieved = row["retrieved_ids"][:5] if row["error"] is None else []
    hits = relevant & set(retrieved)
    if metric == "recall_at_5":
        return len(hits) / len(relevant) if relevant else 0.0
    if metric == "hit_at_5":
        return float(bool(hits))
    if metric == "mrr_at_5":
        return next((1 / rank for rank, key in enumerate(retrieved, 1) if key in relevant), 0.0)
    if metric == "false_recommendation_rate":
        return float(bool(retrieved))
    if metric == "correct_empty_response_rate":
        return float(not retrieved and row["error"] is None)
    if metric == "latency_ms":
        return float(row["latency_ms"])
    raise ValueError(f"unknown metric: {metric}")


def _cohort_delta(
    baseline: Mapping[str, Row],
    candidate: Mapping[str, Row],
    queries: Sequence[EvalQuery],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, selected, metrics in (
        (
            "m17",
            [query for query in queries if 1 <= _count(query) <= 5],
            ("recall_at_5", "hit_at_5", "mrr_at_5"),
        ),
        (
            "empty",
            [query for query in queries if _count(query) == 0],
            ("false_recommendation_rate", "correct_empty_response_rate"),
        ),
        (
            "more_than_5",
            [query for query in queries if _count(query) > 5],
            ("recall_at_5", "hit_at_5", "mrr_at_5"),
        ),
        ("all", list(queries), ("latency_ms",)),
    ):
        result[name] = {
            metric: grouped_paired_delta(
                selected,
                {query.id: _value(baseline[query.id], query, metric) for query in selected},
                {query.id: _value(candidate[query.id], query, metric) for query in selected},
                bootstrap_samples=SAMPLES,
            )
            for metric in metrics
        }
    return result


def compare_retrieval(
    baseline: Run, candidate: Run, queries: Sequence[EvalQuery]
) -> dict[str, Any]:
    """Independently recompute paired query metrics with cluster bootstrap CIs."""
    left, right = _index(baseline, queries), _index(candidate, queries)
    return {
        "baseline": baseline["name"],
        "candidate": candidate["name"],
        "overall": _cohort_delta(left, right, queries),
        "by_language": {
            language: _cohort_delta(
                left, right, [query for query in queries if query.language == language]
            )
            for language in sorted({query.language for query in queries})
        },
        "by_permission": {
            group: _cohort_delta(
                left, right, [query for query in queries if query.permission_group == group]
            )
            for group in sorted({query.permission_group for query in queries})
        },
        "by_relevant_count": {
            str(count): _cohort_delta(
                left, right, [query for query in queries if _count(query) == count]
            )
            for count in sorted({_count(query) for query in queries})
        },
    }


def _retrieval_table(runs: Sequence[Run]) -> str:
    rows = []
    for run in runs:
        overall = run["summary"]["overall"]
        primary = overall["primary"]
        rows.append(
            [
                run["name"],
                f"{primary['n']}/{overall['empty']['n']}/{overall['more_than_5']['n']}",
                _fmt(primary["recall_at_5"]["value"], scale=100),
                _fmt(primary["hit_at_5"]["value"], scale=100),
                _fmt(primary["mrr_at_5"]["value"], scale=100),
                _fmt(overall["empty"]["false_recommendation_rate"]["value"], scale=100),
                _fmt(overall["more_than_5"]["recall_at_5"]["value"], scale=100),
                "/".join(_fmt(overall["latency_ms"][key]["value"]) for key in ("p50", "p95")),
                overall["n_errors"],
                ", ".join(f"{key}:{value}" for key, value in sorted(run["mode_counts"].items())),
            ]
        )
    return _table(
        (
            "Arm",
            "n primary-cohort/empty/>5",
            "Recall@5 %",
            "Hit@5 %",
            "MRR@5 x100",
            "Empty false %",
            ">5 Recall %",
            "Reconstructed p50/p95 ms",
            "Errors",
            "Actual modes",
        ),
        rows,
    )


def _delta_table(comparisons: Sequence[Mapping[str, Any]]) -> str:
    rows = []
    for comparison in comparisons:
        overall = comparison["overall"]
        rows.append(
            [
                f"{comparison['candidate']} - {comparison['baseline']}",
                overall["m17"]["recall_at_5"]["n_text_groups"],
                *(_ci(overall["m17"][key]) for key in ("recall_at_5", "hit_at_5", "mrr_at_5")),
                _ci(overall["empty"]["false_recommendation_rate"]),
                _ci(overall["all"]["latency_ms"], scale=1),
            ]
        )
    return _table(
        (
            "Paired change",
            "primary-cohort text groups",
            "Recall pp [95% CI]",
            "Hit pp [95% CI]",
            "MRR x100 [95% CI]",
            "Empty false pp [95% CI]",
            "Mean latency ms [95% CI]",
        ),
        rows,
    )


def _method_note() -> str:
    return (
        ("Paired changes use 2,000 seeded percentile bootstrap resamples of normalized query "
        + "text groups; all observations for a sampled group stay together. Point estimates "
        + "remain query-macro averages. Intervals are descriptive, are not corrected for dev "
        + "selection/multiple comparisons, and cannot remove synthetic template or catalog bias. "
        + "Errors remain in denominators. Retrieval latency adds the shared live query-embedding "
        + "time to each arm's local/rerank time; it is a reconstructed comparison, not independently "
        + "repeated network latency. Index builds are excluded. Absolute summaries and all "
        + "language/permission/relevance-count cohorts remain in the companion analysis JSON.")
    )


def _historical_note() -> str:
    return (
        ("Historical evidence, not independent confirmation: before this split, sem-eval-real "
        + "already showed vector Recall@5 93.67% versus RRF 85.69%; 75 test texts had been "
        + "observed. The partition is at most semi-independent evidence about the threshold, "
        + "not an unbiased test of fusion selection. There are only 26 synthetic templates and "
        + "no exact tool-name or underscore-ID queries, so the vector advantage cannot be "
        + "generalized to identifier/abbreviation workloads. The old files have no externally "
        + "verifiable dev/test timestamps; internal claim hashes do not establish chronology. "
        + "Current defaults and the stored recommendation remain unchanged.")
    )


def _legacy_recommendation(recommendation: Mapping[str, Any]) -> bool:
    return not all(
        isinstance(recommendation.get(key), str) and recommendation[key]
        for key in ("dev_finished_at", "split_manifest_sha256")
    )


def _provenance_note(recommendation: Mapping[str, Any]) -> str:
    if _legacy_recommendation(recommendation):
        return _historical_note()
    return (
        ("This subsequent synthetic evaluation records dev_finished_at and a split-manifest "
        + "hash. The external capture reference is not authenticated by the harness; verify "
        + "the capture and its timing independently. These fields do not establish historical "
        + "chronology or make the previously observed synthetic corpus an unseen human holdout. "
        + "Template and catalog bias remain. Serving defaults are unchanged.")
    )


def _grid_note(runs: Sequence[Run], recommendation: Mapping[str, Any]) -> str:
    cutoffs = sorted(
        {
            float(run["name"].removeprefix("cosine_"))
            for run in runs
            if run["name"].startswith("cosine_")
        }
    )
    steps = sorted(
        {round(right - left, 10) for left, right in zip(cutoffs, cutoffs[1:], strict=False)}
    )
    if _legacy_recommendation(recommendation) and steps == [0.1]:
        return (
            ("The historical cosine grid has 0.1 spacing: its 0.6 cliff is not fine calibration. "
            + "Future runs include 0.05 steps, without changing this frozen recommendation.")
        )
    return (
        "Recorded cosine cutoffs: "
        + (", ".join(f"{value:g}" for value in cutoffs) or "none")
        + ". Adjacent grid steps: "
        + (", ".join(f"{value:g}" for value in steps) or "not applicable")
        + (". A finer evaluation grid does not establish calibration outside this model/revision "
        + "or change serving defaults.")
    )


def _test_scope_note(recommendation: Mapping[str, Any], *, ungated_control: bool) -> str:
    legacy = _legacy_recommendation(recommendation)
    opening = (
        ("This is an offline correction of the one completed test report. No model calls or "
        + "new tuning follow. ")
        if legacy
        else "This report summarizes the recorded test artifacts without further model calls. "
    )
    if ungated_control:
        return (
            opening + ("The recorded vector_ungated control is compared with recommended to assess "
            + "gate cost when their fusion settings agree; this control does not remove "
            + "synthetic selection or template bias.")
        )
    return opening + (
        ("The historical test omitted ungated vector, so fusion improvement "
        + "and threshold recall cost cannot be separated on test.")
        if legacy
        else "No ungated-vector arm was recorded, so this test cannot isolate gate cost."
    )


def _tradeoff_svg(runs: Sequence[Run], chosen_arm: str) -> str:
    """Draw the exact dev table values without adding plotting dependencies."""
    left, top, width, height = 88.0, 120.0, 844.0, 456.0
    series: dict[str, list[tuple[str, float, float]]] = {"cosine": [], "margin": []}
    baseline: tuple[str, float, float] | None = None
    metadata: list[dict[str, object]] = []
    for run in runs:
        overall = run["summary"]["overall"]
        recall = overall["primary"]["recall_at_5"]["value"]
        false_rate = overall["empty"]["false_recommendation_rate"]["value"]
        if recall is None or false_rate is None:
            continue
        point = (str(run["name"]), float(false_rate), float(recall))
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in point[1:]):
            raise ValueError("tradeoff rates must be finite and between zero and one")
        metadata.append(
            {
                "arm": point[0],
                "m17_recall_at_5": recall,
                "empty_false_recommendation_rate": false_rate,
            }
        )
        family = point[0].split("_", 1)[0]
        if family in series:
            series[family].append(point)
        elif point[0] == "ungated":
            baseline = point

    def position(point: tuple[str, float, float]) -> tuple[float, float]:
        return left + width * point[1], top + height * (1 - point[2])

    parts = [
        ('<svg xmlns="http://www.w3.org/2000/svg" width="1020" height="700" '
        + 'viewBox="0 0 1020 700" role="img" aria-labelledby="title desc">'),
        ('<title id="title">Dev threshold tradeoff: recall versus empty false '
        + "recommendations</title>"),
        ('<desc id="desc">Synthetic data, not quality acceptance. Lower empty false recommendation '
        + "and higher recall are better. Blue circles show cosine cutoffs; dashed orange squares "
        + "show within-request margin cutoffs. Labels are cutoff values; coincident points share "
        + "labels. The dark diamond is ungated and a black ring marks the selected recommendation. "
        + "Exact source rates are in each point title and the metadata; the full table is in "
        + "dev-report.md.</desc>"),
        "<metadata>" + html.escape(json.dumps(metadata, ensure_ascii=False)) + "</metadata>",
        '<rect width="1020" height="700" fill="#ffffff"/>',
        '<g font-family="Arial, Helvetica, sans-serif" fill="#172033">',
        '<text x="88" y="34" font-size="22" font-weight="700">Dev threshold tradeoff</text>',
        '<text x="88" y="61" font-size="15" fill="#9a3412">合成数据，不代表质量验收</text>',
        '<text x="88" y="100" font-size="14">primary-cohort Recall@5 (%) — higher is better</text>',
    ]
    for tick in range(0, 101, 20):
        x, y = left + width * tick / 100, top + height * (1 - tick / 100)
        parts.extend(
            [
                f'<path d="M{x:.2f},{top} V{top + height}" stroke="#e2e8f0"/>',
                f'<path d="M{left},{y:.2f} H{left + width}" stroke="#e2e8f0"/>',
                (f'<text x="{x:.2f}" y="{top + height + 24}" font-size="13" '
                + f'text-anchor="middle">{tick}</text>'),
                (f'<text x="{left - 14}" y="{y + 5:.2f}" font-size="13" '
                + f'text-anchor="end">{tick}</text>'),
            ]
        )
    parts.extend(
        [
            (f'<path d="M{left},{top} V{top + height} H{left + width}" '
            + 'stroke="#64748b" fill="none"/>'),
            ('<text x="510" y="632" font-size="14" text-anchor="middle">'
            + "Empty false recommendation (%) — lower is better</text>"),
        ]
    )
    for family, color, dash, legend_x in (
        ("cosine", "#1769aa", "", 540),
        ("margin", "#c65d12", ' stroke-dasharray="7 5"', 700),
    ):
        values = sorted(series[family], key=lambda point: float(point[0].split("_", 1)[1]))
        if values:
            path = ([baseline] if baseline is not None else []) + values
            coordinates = " ".join(f"{x:.2f},{y:.2f}" for x, y in map(position, path))
            parts.append(
                (f'<polyline points="{coordinates}" fill="none" stroke="{color}" '
                + f'stroke-width="2"{dash}/>')
            )
        parts.extend(
            [
                f'<path d="M{legend_x},57 h26" stroke="{color}" stroke-width="2"{dash}/>',
                f'<text x="{legend_x + 34}" y="62" font-size="13">{family}</text>',
            ]
        )

    occupied: list[tuple[float, float, float, float]] = []
    labeled: list[tuple[str, float, float, str, str, bool]] = []
    for family, color in (("cosine", "#1769aa"), ("margin", "#c65d12")):
        coincident: dict[tuple[float, float], list[str]] = {}
        for name, false_rate, recall in series[family]:
            coincident.setdefault((false_rate, recall), []).append(name)
        for (false_rate, recall), names in coincident.items():
            x, y = position((names[0], false_rate, recall))
            label = " / ".join(name.split("_", 1)[1] for name in names)
            title = (
                ", ".join(names)
                + f": Recall@5={recall * 100!r}%; empty false={false_rate * 100!r}%"
            )
            labeled.append((label, x, y, color, family, chosen_arm in names))
            parts.append("<g><title>" + html.escape(title) + "</title>")
            if family == "cosine":
                parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" fill="{color}"/>')
            else:
                parts.append(
                    (f'<rect x="{x - 4.5:.2f}" y="{y - 4.5:.2f}" width="9" height="9" '
                    + f'fill="{color}"/>')
                )
            parts.append("</g>")
    if baseline is not None:
        x, y = position(baseline)
        labeled.append(("ungated", x, y, "#334155", "ungated", chosen_arm == "ungated"))
        parts.append(
            (f'<path d="M{x:.2f},{y - 7:.2f} l7,7 l-7,7 l-7,-7 z" fill="#334155">'
            + f"<title>ungated: Recall@5={baseline[2] * 100!r}%; "
            + f"empty false={baseline[1] * 100!r}%</title></path>")
        )
    for label, x, y, color, family, selected in labeled:
        if selected:
            parts.append(
                (f'<circle cx="{x:.2f}" cy="{y:.2f}" r="10" fill="none" '
                + 'stroke="#111827" stroke-width="2"/>')
            )
        text_width = len(label) * 7.1 + 8
        offsets = (-17, 28, -39, 50, -61, 72, -83, 94)
        if family == "margin":
            offsets = (28, -17, 50, -39, 72, -61, 94, -83)
        placement = (min(x + 14, left + width - text_width), max(top + 16, y - 17))
        for dy in offsets:
            found = False
            for dx in (14, -text_width - 14):
                tx = min(max(left + 5, x + dx), left + width - text_width)
                ty = min(max(top + 16, y + dy), top + height - 6)
                box = (tx - 4, ty - 14, tx + text_width, ty + 4)
                if not any(
                    box[0] < other[2]
                    and box[2] > other[0]
                    and box[1] < other[3]
                    and box[3] > other[1]
                    for other in occupied
                ):
                    placement = (tx, ty)
                    occupied.append(box)
                    found = True
                    break
            if found:
                break
        tx, ty = placement
        parts.extend(
            [
                (f'<path d="M{x:.2f},{y:.2f} L{tx:.2f},{ty - 4:.2f}" '
                + f'stroke="{color}" stroke-opacity=".5" fill="none"/>'),
                (f'<rect x="{tx - 3:.2f}" y="{ty - 13:.2f}" width="{text_width:.2f}" '
                + 'height="18" rx="2" fill="#fff" fill-opacity=".94"/>'),
                (f'<text x="{tx:.2f}" y="{ty:.2f}" font-size="12" fill="{color}" '
                + ">") + html.escape(label) + "</text>",
            ]
        )
    parts.extend(
        [
            ('<text x="88" y="666" font-size="12" fill="#475569">'
            + "Labels = cutoff values; lines follow increasing cutoffs; black ring = recommendation."
            + "</text>"),
            ('<text x="88" y="687" font-size="12" fill="#475569">'
            + "Dev only. No absolute reranker-score cutoff. Source: dev-threshold.json.</text>"),
            "</g></svg>",
        ]
    )
    return "\n".join(parts) + "\n"


def summarize_dev(output: Path, queries: Sequence[EvalQuery]) -> None:
    groups = {
        name: _load(output / f"dev-{name}.json")["runs"]
        for name in ("fusion", "threshold", "rerank")
    }
    fusion = {run["name"]: run for run in groups["fusion"]}
    comparisons = {
        "fusion": [
            compare_retrieval(fusion["rrf"], run, queries)
            for run in groups["fusion"]
            if run["name"] != "rrf"
        ],
        "threshold": [
            compare_retrieval(groups["threshold"][0], run, queries)
            for run in groups["threshold"][1:]
        ],
        "rerank": [
            compare_retrieval(fusion[run["name"].removesuffix("_rerank")], run, queries)
            for run in groups["rerank"]
        ],
    }
    recommendation = _load(output / "recommendation.json")
    (output / "tradeoff.svg").write_text(
        _tradeoff_svg(groups["threshold"], recommendation["chosen_threshold_arm"]),
        encoding="utf-8",
    )
    lines = [
        "# Dev-only tuning report",
        "",
        NOTICE,
        "",
        (f"Frozen recommendation: fusion `{recommendation['chosen_fusion_arm']}`, "
        + f"threshold `{recommendation['chosen_threshold_arm']}`. "
        + "This recommendation does not change service defaults; rerank stays off."),
        "",
        recommendation["selection_rule"],
        "",
        _provenance_note(recommendation),
        "",
        _method_note(),
    ]
    for name in ("fusion", "threshold", "rerank"):
        lines.extend(
            [
                "",
                f"## {name.capitalize()}",
                "",
                _retrieval_table(groups[name]),
                "",
                _delta_table(comparisons[name]),
                "",
                _empty_table(groups[name], queries),
            ]
        )
        if name == "threshold":
            lines.extend(["", "[Threshold tradeoff curve (SVG)](tradeoff.svg)."])
    lines.extend(
        [
            "",
            ("The threshold table is the full predeclared tradeoff curve. Cosine cutoffs use "
            + "normalized embedding similarity and remain model/domain specific. Margin cutoffs "
            + "are within-request top1 minus authorized-score median gaps. No absolute reranker "
            + "score threshold is used. A candidate cutoff is accepted only when Recall, Hit, "
            + "and MRR each lose at most 1 percentage point versus the selected ungated fusion."),
            _grid_note(groups["threshold"], recommendation),
            "",
            "[Detailed cohort estimates and paired changes](dev-analysis.json).",
        ]
    )
    (output / "dev-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write(
        output / "dev-analysis.json",
        {
            "notice": NOTICE,
            "recommendation": recommendation,
            "runs": {
                name: [{"name": run["name"], "summary": run["summary"]} for run in runs]
                for name, runs in groups.items()
            },
            "comparisons": comparisons,
            "empty_breakdown": {
                name: {run["name"]: empty_breakdown(run, queries) for run in runs}
                for name, runs in groups.items()
            },
        },
    )


def _prompt_tokens(row: Row) -> int | None:
    for key in ("prompt_tokens", "input_tokens"):
        value = row["usage"].get(key)
        if type(value) is int and value >= 0:
            return value
    return None


def _token_reduction(
    baseline: Mapping[str, Row], candidate: Mapping[str, Row]
) -> dict[str, object]:
    pairs = []
    for key in sorted(baseline):
        left, right = _prompt_tokens(baseline[key]), _prompt_tokens(candidate[key])
        if left is not None and left > 0 and right is not None:
            pairs.append((left, right))
    if not pairs:
        return {"value": None, "ci95": [None, None], "n_pairs": 0}
    values = np.asarray(pairs, dtype=np.float64)
    indices = np.random.default_rng(SEED).integers(len(pairs), size=(SAMPLES, len(pairs)))
    resampled = values[indices].sum(axis=1)
    ratios = 1 - resampled[:, 1] / resampled[:, 0]
    return {
        "value": float(1 - values[:, 1].sum() / values[:, 0].sum()),
        "ci95": [float(value) for value in np.quantile(ratios, [0.025, 0.975])],
        "n_pairs": len(pairs),
    }


def knee_failure_breakdown(rows: Sequence[Row]) -> dict[str, Any]:
    """Attribute misses from retained candidates without guessing transport causes."""
    failed = [row for row in rows if not row["correct"]]
    retrieval = [
        row
        for row in failed
        if row["error"] is None and row["expected_id"] not in row["candidate_ids"]
    ]
    return {
        "retrieval_misses": len(retrieval),
        "retrieval_misses_by_language": dict(Counter(row["language"] for row in retrieval)),
        "selection_misses": sum(
            row["error"] is None and row["expected_id"] in row["candidate_ids"] for row in failed
        ),
        "errors": dict(Counter(row["error"] for row in failed if row["error"] is not None)),
        "invalid_response_completion_tokens": [
            row["usage"].get("completion_tokens")
            for row in failed
            if row["error"] == "invalid_response"
        ],
    }


def summarize_knee(
    rows: Sequence[Row],
    queries: Sequence[EvalQuery],
    *,
    sizes: Sequence[int] = (20, 50, 100, 200, 500),
) -> tuple[str, dict[str, Any]]:
    """Validate independently scoreable knee rows and summarize matched test arms."""
    arms = ("full_load", "search_first", "search_rerank")
    query_ids = {query.id for query in queries}
    if len(query_ids) != len(queries):
        raise ValueError("duplicate knee query IDs")
    by_query = {query.id: query for query in queries}
    if any(row["tool_count"] not in sizes or row["arm"] not in arms for row in rows):
        raise ValueError("unknown knee size or arm")
    runs: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    table = []
    for size in sizes:
        indexed: dict[str, dict[str, Row]] = {}
        for arm in arms:
            selected = [row for row in rows if row["tool_count"] == size and row["arm"] == arm]
            current = {row["query_id"]: row for row in selected}
            if len(selected) != len(current) or set(current) != query_ids:
                raise ValueError("knee arms require unique, exactly paired query IDs")
            for key, row in current.items():
                expected = by_query[key].relevant_ids[0]
                correct = row["selected_id"] == expected and row["error"] is None
                if row["correct"] != correct or row["expected_id"] != expected:
                    raise ValueError("knee accuracy disagrees with independently recomputed truth")
                if (
                    row["candidate_count"] != len(row["candidate_ids"])
                    or len(set(row["candidate_ids"])) != len(row["candidate_ids"])
                    or (
                        row["selected_id"] is not None
                        and row["selected_id"] not in row["candidate_ids"]
                    )
                ):
                    raise ValueError("invalid knee candidate IDs")
                if not math.isfinite(row["latency_ms"]) or row["latency_ms"] < 0:
                    raise ValueError("invalid knee latency")
            indexed[arm] = current
            accuracy = grouped_paired_delta(
                queries,
                {key: 0.0 for key in current},
                {key: float(row["correct"]) for key, row in current.items()},
            )
            latency = [row["latency_ms"] for row in selected]
            run: dict[str, Any] = {
                "tool_count": size,
                "arm": arm,
                "n": len(selected),
                "accuracy": accuracy,
                "n_errors": sum(row["error"] is not None for row in selected),
                "latency_ms": {
                    "p50": float(np.quantile(latency, 0.5)),
                    "p95": float(np.quantile(latency, 0.95)),
                },
                "mode_counts": dict(Counter(row["search_mode"] for row in selected)),
                "candidate_count": {
                    "min": min(row["candidate_count"] for row in selected),
                    "max": max(row["candidate_count"] for row in selected),
                },
                "by_language": {},
                "failure_breakdown": knee_failure_breakdown(selected),
            }
            for language in sorted({query.language for query in queries}):
                language_queries = [query for query in queries if query.language == language]
                run["by_language"][language] = grouped_paired_delta(
                    language_queries,
                    {query.id: 0.0 for query in language_queries},
                    {query.id: float(current[query.id]["correct"]) for query in language_queries},
                )
            runs.append(run)
            reduction = (
                _token_reduction(indexed["full_load"], current) if arm != "full_load" else None
            )
            table.append(
                [
                    size,
                    arm,
                    len(selected),
                    _ci(accuracy),
                    "/".join(_fmt(run["latency_ms"][key]) for key in ("p50", "p95")),
                    f"{_fmt(reduction['value'], scale=100)}% ({reduction['n_pairs']} pairs)"
                    if reduction
                    else "reference",
                    run["n_errors"],
                    ", ".join(
                        f"{key}:{value}" for key, value in sorted(run["mode_counts"].items())
                    ),
                ]
            )
            if arm != "full_load":
                changes.append(
                    {
                        "tool_count": size,
                        "candidate": arm,
                        "baseline": "full_load",
                        "accuracy": grouped_paired_delta(
                            queries,
                            {
                                key: float(row["correct"])
                                for key, row in indexed["full_load"].items()
                            },
                            {key: float(row["correct"]) for key, row in current.items()},
                        ),
                        "mean_latency_ms": grouped_paired_delta(
                            queries,
                            {
                                key: float(row["latency_ms"])
                                for key, row in indexed["full_load"].items()
                            },
                            {key: float(row["latency_ms"]) for key, row in current.items()},
                        ),
                        "prompt_token_reduction": reduction,
                    }
                )
    rendered = "\n".join(
        [
            NOTICE,
            "",
            _table(
                (
                    "Tools",
                    "Arm",
                    "n",
                    "Accuracy % [95% CI]",
                    "Live end-to-end p50/p95 ms",
                    "Prompt reduction",
                    "Errors",
                    "Actual modes",
                ),
                table,
            ),
            "",
            _table(
                (
                    "Tools",
                    "Paired change vs full load",
                    "Accuracy pp [95% CI]",
                    "Mean latency ms [95% CI]",
                    "Prompt reduction % [95% CI]",
                ),
                [
                    [
                        change["tool_count"],
                        change["candidate"],
                        _ci(change["accuracy"]),
                        _ci(change["mean_latency_ms"], scale=1),
                        _ci(change["prompt_token_reduction"]),
                    ]
                    for change in changes
                ],
            ),
            "",
            ("Queries and nested distractor catalogs are paired across all sizes and arms; "
            + "arm order is randomized deterministically per pair. Knee texts are unique, so the "
            + "text-group bootstrap has one query per cluster. Prompt reduction uses summed "
            + "provider-reported prompt tokens on complete pairs only; missing usage is not zero. "
            + "Accuracy is identity selection, with no tool execution. Provider/model weights "
            + "are not pinned; these five catalog sizes do not establish a production breakpoint."),
            "",
            ("The rerank arm is experimental and remains default-off. At 500 tools the "
            + "nonrerank candidate must be assessed separately from rerank. Candidate IDs and "
            + "error/usage observations, including retrieval versus selection misses, are "
            + "retained in the analysis JSON; an invalid response alone does not prove truncation."),
        ]
    )
    return rendered, {"runs": runs, "comparisons": changes}


def test_comparisons(indexed: Mapping[str, Run], queries: Sequence[EvalQuery]) -> list[Run]:
    """A future ungated arm isolates gate cost; never fabricate missing old measurements."""
    comparisons = [
        compare_retrieval(indexed["current_rrf"], indexed["recommended"], queries),
        compare_retrieval(indexed["recommended"], indexed["recommended_rerank"], queries),
    ]
    if "vector_ungated" in indexed:
        comparisons.append(
            compare_retrieval(indexed["vector_ungated"], indexed["recommended"], queries)
        )
    return comparisons


def _final_conclusions(
    indexed: Mapping[str, Run],
    queries: Sequence[EvalQuery],
    knee: Mapping[str, Any],
    recommendation: Mapping[str, Any],
) -> str:
    runs = {run["arm"]: run for run in knee["runs"] if run["tool_count"] == 500}
    search, full, rerank = (runs[arm] for arm in ("search_first", "full_load", "search_rerank"))
    change = next(
        row
        for row in knee["comparisons"]
        if row["tool_count"] == 500 and row["candidate"] == "search_first"
    )
    empty = empty_breakdown(indexed["recommended"], queries)
    permission, outside = empty["permission_negative"], empty["outside_catalog"]
    measured_profile = (
        ("Its historical vector + 0.6 results do not measure the newly advised ungated "
        + "fallback profile.")
        if _legacy_recommendation(recommendation)
        else ("Results apply to the recorded configuration and do not establish "
        + "unmeasured service profiles.")
    )
    return "\n\n".join(
        [
            ("At 500 tools, the frozen nonrerank search candidate achieves "
            + f"**{_fmt(search['accuracy']['value'], scale=100)}% versus "
            + f"{_fmt(full['accuracy']['value'], scale=100)}%** for full loading; "
            + f"paired change {_ci(change['accuracy'])} pp. "
            + "The experimental rerank arm achieves "
            + f"{_fmt(rerank['accuracy']['value'], scale=100)}%; "
            + "it remains default-off and must be assessed separately."),
            ("For the measured candidate, in-catalog but unauthorized queries have "
            + f"**{permission['false_recommendations']}/{permission['n']} "
            + f"({_fmt(permission['false_rate'], scale=100)}%)** false recommendations. "
            + f"Outside-catalog queries have {outside['false_recommendations']}/{outside['n']} "
            + f"across only **{outside['unique_texts']} unique texts**. "
            + "Empty-ACL queries are trivial cases and are reported separately below."),
            ("Current advice for a future service profile is **vector_bm25_fallback without "
            + "cosine or margin gates**, with rerank off. Adoption confidence is low to medium; "
            + "this preserves the lexical fallback while deferring cosine calibration until "
            + "independent human labels exist. Rerank evidence is medium-confidence and supports a "
            + "per-deployment controlled trial, not unconditional enablement. "
            + "Actual serving defaults remain unchanged; switching them requires a "
            + "**separate user-confirmed task**. The raw arm named `recommended` is the frozen "
            + "measurement configuration, not this revised service advice. ") + measured_profile,
        ]
    )


def summarize_test(output: Path, queries: Sequence[EvalQuery]) -> None:
    data = _load(output / "test-retrieval.json")
    recommendation = _load(output / "recommendation.json")
    runs: list[Run] = data["runs"]
    indexed = {run["name"]: run for run in runs}
    comparisons = test_comparisons(indexed, queries)
    knee = _load(output / "test-knee.json")
    if knee.get("complete") is not True:
        raise ValueError("final report requires complete frozen test knee results")
    selected_ids = set(knee["query_ids"])
    selected = [query for query in queries if query.id in selected_ids]
    rendered, knee_details = summarize_knee(knee["observations"], selected)
    lines = [
        "# Final frozen test report",
        "",
        NOTICE,
        "",
        _final_conclusions(indexed, queries, knee_details, recommendation),
        "",
        _test_scope_note(recommendation, ungated_control="vector_ungated" in indexed),
        "",
        _provenance_note(recommendation),
        "",
        _method_note(),
        "",
        "## Retrieval",
        "",
        _retrieval_table(runs),
        "",
        _delta_table(comparisons),
        "",
        _empty_table(runs, queries),
        "",
        "## Tool-count experiment",
        "",
        rendered,
        "",
        ("[Detailed cohorts and paired changes](test-analysis.json). "
        + "[Frozen split](split-manifest.json). [Prior-exposure audit](split-audit.json)."),
    ]
    (output / "final-test-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write(
        output / "test-analysis.json",
        {
            "notice": NOTICE,
            "retrieval": {
                "runs": [{"name": run["name"], "summary": run["summary"]} for run in runs],
                "comparisons": comparisons,
                "empty_breakdown": {run["name"]: empty_breakdown(run, queries) for run in runs},
            },
            "knee": knee_details,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports/tuning")
    parser.add_argument("--phase", choices=("dev", "test", "all"), required=True)
    args = parser.parse_args()
    data = load_dataset(ROOT / "data/synthetic-v1.json")
    dev, test, manifest = split_dataset(data)
    if _load(args.output_dir / "split-manifest.json") != manifest:
        raise ValueError("report split differs from the frozen manifest")
    if args.phase in ("dev", "all"):
        summarize_dev(args.output_dir, dev.queries)
    if args.phase in ("test", "all"):
        expected = {query.id for query in paired_knee_queries(test)}
        if set(_load(args.output_dir / "test-knee.json")["query_ids"]) != expected:
            raise ValueError("knee query selection differs from the frozen test subset")
        summarize_test(args.output_dir, test.queries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

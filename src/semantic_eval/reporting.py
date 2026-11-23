"""Render inspectable regression reports without drawing acceptance conclusions."""

from __future__ import annotations

import html
import json
from collections.abc import Mapping


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _cell(value: object) -> str:
    return html.escape(str(value)).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _estimate(value: object) -> str:
    estimate = _mapping(value)
    point = estimate.get("value")
    if not isinstance(point, int | float):
        return "N/A"
    interval = estimate.get("ci95")
    if (
        isinstance(interval, list)
        and len(interval) == 2
        and isinstance(interval[0], int | float)
        and isinstance(interval[1], int | float)
    ):
        return f"{point:.4f} [{interval[0]:.4f}, {interval[1]:.4f}]"
    return f"{point:.4f}"


def _percent(value: object) -> str:
    return f"{value:.1%}" if isinstance(value, int | float) else "N/A"


def _group_rows(label: str, value: object) -> list[str]:
    group = _mapping(value)
    n = group.get("n", 0)
    share = _percent(group.get("proportion"))
    rows = []
    for cohort_key, cohort_label in (
        ("primary", "1..5"),
        ("more_than_5", ">5"),
    ):
        cohort = _mapping(group.get(cohort_key))
        rows.append(
            f"| {_cell(label)} | {n} ({share}) | {cohort_label} | "
            + f"{cohort.get('n', 0)} ({_percent(cohort.get('proportion'))}) | "
            + f"{_estimate(cohort.get('recall_at_5'))} | "
            + f"{_estimate(cohort.get('hit_at_5'))} | "
            + f"{_estimate(cohort.get('mrr_at_5'))} | "
            + f"{_estimate(cohort.get('theoretical_recall_ceiling'))} |"
        )
    return rows


def _quality_table(groups: Mapping[str, object]) -> list[str]:
    lines = [
        (
            "| Group | Group n (% all queries) | Relevant authorized | "
            + "Metric denominator n (% group) | Recall@5 [95% CI] | Hit@5 [95% CI] | "
            + "MRR@5 [95% CI] | Recall theoretical ceiling [95% CI] |"
        ),
        "|---|---:|---|---:|---|---|---|---|",
    ]
    for name, group in groups.items():
        lines.extend(_group_rows(name, group))
    return lines


def _operational_table(groups: Mapping[str, object]) -> list[str]:
    lines = [
        (
            "| Group | Empty denominator n (% group) | False recommendation [95% CI] | "
            + "Correct empty response [95% CI] | Errors n / rate [95% CI] | "
            + "Unauthorized results / queries | Unauthorized query rate [95% CI] | "
            + "p50 ms [95% CI] | p95 ms [95% CI] |"
        ),
        "|---|---:|---|---|---|---:|---|---|---|",
    ]
    for label, value in groups.items():
        group = _mapping(value)
        empty = _mapping(group.get("empty"))
        latency = _mapping(group.get("latency_ms"))
        lines.append(
            f"| {_cell(label)} | {empty.get('n', 0)} "
            + f"({_percent(empty.get('proportion'))}) | "
            + f"{_estimate(empty.get('false_recommendation_rate'))} | "
            + f"{_estimate(empty.get('correct_empty_response_rate'))} | "
            + f"{group.get('n_errors', 0)} / {_estimate(group.get('error_rate'))} | "
            + f"{group.get('n_unauthorized_results', 0)} / "
            + f"{group.get('n_queries_with_unauthorized_results', 0)} | "
            + f"{_estimate(group.get('unauthorized_query_rate'))} | "
            + f"{_estimate(latency.get('p50'))} | {_estimate(latency.get('p95'))} |"
        )
    return lines


def render_markdown(report: dict[str, object]) -> str:
    """Render the harness report schema, retaining every metric group."""
    metadata = _mapping(report.get("metadata"))
    title = (
        "# 真实 Provider 合成实验 / Synthetic regression report"
        if metadata.get("mode") == "real"
        else "# 离线回归基线 / Synthetic regression report"
    )
    lines = [
        title,
        "",
        (
            "**合成数据：本报告只用于回归，不代表真实质量。质量验收延期；"
            + "不得据此宣称 Recall 达标或批准开启 rerank。**"
        ),
        "",
        (
            "Query and descriptor vocabularies are shared by construction, so the lexical arms "
            + "are a plumbing/ordering regression rather than a relevance measurement."
        ),
        "",
        (
            "Confidence intervals describe variation in the supplied synthetic query sample; "
            + "they do not validate real query relevance. Local retrieval timings exclude Gateway "
            + "and business-tool execution and are not end-to-end SLO evidence."
        ),
        "",
        "## Run metadata",
        "",
        "```json",
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "## Metric definitions",
        "",
        (
            "Recall@5 uses authorized relevance (semantic relevance intersected with the caller's "
            + "allowed tools). The primary macro denominator includes only "
            + "queries with 1..5 authorized "
            + "relevant tools. Queries with >5 tools are reported separately "
            + "with their theoretical "
            + "Recall ceiling, min(5 / relevant count, 1). Empty sets never enter Recall/Hit/MRR."
        ),
        "",
        (
            "Hit@5 is the fraction with at least one relevant top-five result. MRR@5 is truncated: "
            + "1 / first relevant rank for ranks 1..5, otherwise 0. Errors "
            + "contribute zero quality and "
            + "retain their measured latency. False recommendation counts any "
            + "nonempty response to an "
            + "empty-relevance query; correct empty response additionally "
            + "requires no request error. "
            + "These retrieval statistics do not measure an Agent's refusal quality."
        ),
        "",
        (
            "All intervals are seeded query-bootstrap percentile 95% CIs. Missing denominators are "
            + "N/A; one-query and constant samples yield degenerate intervals. "
            + "Group proportions refer "
            + "to all queries; cohort proportions refer to the containing "
            + "group. Small cohorts remain "
            + "visible. Unauthorized counts cover every returned position, "
            + "including positions after 5."
        ),
        "",
    ]
    runs = report.get("runs")
    if not isinstance(runs, list):
        runs = []
    for index, run_value in enumerate(runs, start=1):
        run = _mapping(run_value)
        configuration = _mapping(run.get("configuration"))
        metrics = _mapping(run.get("metrics"))
        method = _cell(configuration.get("method", "unknown"))
        examples = _cell(configuration.get("examples", False))
        lines.extend(
            [
                f"## {index}. {method}, examples={examples}",
                "",
                (
                    f"Queries: {metrics.get('n_queries', 0)}. "
                    + "Bootstrap: "
                    + f"{_cell(json.dumps(metrics.get('bootstrap', {}), sort_keys=True))}."
                ),
                "",
                "### Overall",
                "",
            ]
        )
        overall = {"all": metrics.get("overall")}
        lines.extend(_quality_table(overall))
        lines.append("")
        lines.extend(_operational_table(overall))
        lines.append("")
        for key, title in (
            ("by_language", "Language"),
            ("by_permission", "Permission"),
            ("relevant_count", "Authorized relevant-count distribution"),
        ):
            groups = _mapping(metrics.get(key))
            lines.extend([f"### {title}", ""])
            lines.extend(_quality_table(groups))
            lines.append("")
            lines.extend(_operational_table(groups))
            lines.append("")
        mode_counts = run.get("mode_counts")
        if mode_counts is not None:
            lines.extend(
                [
                    (
                        "Retrieval modes (including degradations): "
                        + f"{_cell(json.dumps(mode_counts, ensure_ascii=False, sort_keys=True))}."
                    ),
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _estimate_with_count(value: object) -> str:
    return f"{_estimate(value)} (n={_mapping(value).get('n', 0)})"


def render_knee_markdown(report: dict[str, object]) -> str:
    """Render every tool-count arm, keeping usage provenance and missingness."""
    metadata = {
        key: report[key]
        for key in (
            "seed",
            "queries_requested_per_size",
            "queries_actual_per_size",
            "metadata",
            "selector_profile",
            "model_profile",
        )
        if key in report
    }
    lines = [
        "# 工具数量与选择实验 / Synthetic tool-count experiment",
        "",
        (
            "**合成数据：本报告只用于回归或受控合成实验，不代表真实质量。质量验收延期；"
            + "不得据此宣称 Recall 达标或批准开启 rerank。**"
        ),
        "",
        (
            "These configured tool-count comparisons do not establish a production breakpoint. "
            + "Tool selection is evaluated without executing any business tool."
        ),
        "",
    ]
    if report.get("mode") == "mock":
        lines.extend(
            [
                (
                    "Selection accuracy is not an informative axis under the mock selector "
                    + "(it is a fixture, not a capability). The mock knee is a "
                    + "latency/retrieval-recall "
                    + "baseline only; the accuracy knee requires the real selector."
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## Protocol",
            "",
            _cell(report.get("protocol", "Protocol was not supplied by the runner.")),
            "",
            (
                "Accuracy is the fraction of all attempted queries selecting an "
                + "annotated authorized "
                + "relevant tool. Errors and absent selections count as incorrect "
                + "and stay in the latency "
                + "denominator. Intervals are seeded query-bootstrap percentile "
                + "95% CIs within each arm; "
                + "the intervals describe this synthetic sample. "
                + "Index construction and end-to-end Gateway "
                + "behavior are outside the timed comparison."
            ),
            "",
            (
                "The search-first arm is configured for RRF. "
                + "Search modes show the actual query counts, "
                + "including any BM25 fallback; full-load rows are not_applicable. "
                + "JSON observations retain selected, expected and candidate tool IDs "
                + "so accuracy can be independently recomputed."
            ),
            "",
            "```json",
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
            "## Accuracy and latency",
            "",
            (
                "| Tools | Arm | Query denominator n | Accuracy [95% CI] | Errors | "
                + "Candidates min..max | p50 ms [95% CI] | p95 ms [95% CI] | "
                + "Search modes (query counts) |"
            ),
            "|---:|---|---:|---|---:|---|---|---|---|",
        ]
    )
    run_values = report.get("runs")
    runs = [_mapping(value) for value in run_values] if isinstance(run_values, list) else []
    for run in runs:
        latency = _mapping(run.get("latency_ms"))
        candidates = _mapping(run.get("candidate_tools"))
        lines.append(
            f"| {_cell(run.get('tool_count', 'N/A'))} | {_cell(run.get('arm', 'unknown'))} | "
            + f"{_cell(run.get('n', 0))} | {_estimate(run.get('accuracy'))} | "
            + f"{_cell(run.get('n_errors', 0))} | {_cell(candidates.get('min', 'N/A'))}.."
            + f"{_cell(candidates.get('max', 'N/A'))} | {_estimate(latency.get('p50'))} | "
            + f"{_estimate(latency.get('p95'))} | "
            + f"{_cell(json.dumps(run.get('search_mode_counts', {}), sort_keys=True))} |"
        )
    lines.extend(
        [
            "",
            "## Token usage and estimation",
            "",
            _cell(report.get("token_scope", "Usage scope was not supplied by the runner.")),
            "",
            (
                "Provider-reported fields are measured chat usage returned by "
                + "the configured endpoint. "
                + "Each mean includes requests that reported that field, with its "
                + "own denominator n; "
                + "missing usage stays N/A and is never filled with zero. "
                + "Mock-estimated prompt usage is "
                + "the ceiling of serialized prompt UTF-8 bytes/4, not model "
                + "tokenization or a billing "
                + "measurement. Estimated and reported token columns must not be "
                + "compared as equivalent. "
                + "Search embedding usage is separate from chat usage."
            ),
            "",
            (
                "| Tools | Arm | Provider-reported mean prompt tokens [95% CI], n | "
                + "Provider-reported mean completion tokens [95% CI], n | "
                + "Provider-reported mean total tokens [95% CI], n | "
                + "Mock-estimated mean prompt units [95% CI], n |"
            ),
            "|---:|---|---|---|---|---|",
        ]
    )
    for run in runs:
        reported = _mapping(run.get("mean_reported_tokens"))
        lines.append(
            f"| {_cell(run.get('tool_count', 'N/A'))} | {_cell(run.get('arm', 'unknown'))} | "
            + f"{_estimate_with_count(reported.get('prompt_tokens'))} | "
            + f"{_estimate_with_count(reported.get('completion_tokens'))} | "
            + f"{_estimate_with_count(reported.get('total_tokens'))} | "
            + f"{_estimate_with_count(run.get('mean_estimated_prompt_tokens'))} |"
        )
    accounting = report.get("provider_accounting")
    if accounting is not None:
        lines.extend(
            [
                "",
                "## Provider accounting",
                "",
                (
                    "Counters retain the runner's accounting scope; a shared budget "
                    + "can also include "
                    + "retrieval ablations in the same invocation."
                ),
                "",
                "```json",
                json.dumps(accounting, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"

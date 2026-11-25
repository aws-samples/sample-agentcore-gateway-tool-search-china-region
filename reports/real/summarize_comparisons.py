"""Render paired synthetic real/mock comparisons without contacting a provider.

Run from any directory with Python 3.10+:
    python3 reports/real/summarize_comparisons.py

Only retrieval-comparison.md and knee-comparison.md are written. Source
identifiers, query text, credentials, hostnames, and error bodies are never
rendered. The supplied reference estimates are preserved; additional paired
estimates use a seeded, standard-library query bootstrap.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from statistics import fmean

DISCLAIMER = "这是合成数据上的真实模型参考结果，不代表质量验收，" + "也不构成开启 rerank 的依据。"
METHODS = ("bm25", "vector", "rrf")
SIZES = (20, 50, 100, 200, 500)
SEED = 20261010
SAMPLES = 1000


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def quantile(values, probability):
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def estimate(values, statistic="mean"):
    """Query-level percentile bootstrap; p50 is the median of paired deltas."""
    if not values:
        return {"value": None, "ci95": [None, None], "n": 0}
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Non-finite paired measurement")
    measure = fmean if statistic == "mean" else lambda rows: quantile(rows, 0.5)
    value = measure(values)
    if len(set(values)) == 1:
        return {"value": value, "ci95": [value, value], "n": len(values)}
    rng = random.Random(SEED)  # nosec B311
    samples = [measure(rng.choices(values, k=len(values))) for _ in range(SAMPLES)]
    return {
        "value": value,
        "ci95": [quantile(samples, 0.025), quantile(samples, 0.975)],
        "n": len(values),
    }


def token_reduction(pairs):
    """Percent reduction in summed prompt tokens on the same complete pairs."""
    valid = [
        (full["usage"]["prompt_tokens"], search["usage"]["prompt_tokens"])
        for full, search in pairs
        if full.get("usage_source") == search.get("usage_source") == "provider"
        and type(full.get("usage", {}).get("prompt_tokens")) is int
        and type(search.get("usage", {}).get("prompt_tokens")) is int
        and full["usage"]["prompt_tokens"] > 0
        and search["usage"]["prompt_tokens"] >= 0
    ]
    if not valid:
        return {"value": None, "ci95": [None, None], "n": 0}

    def reduction(rows):
        return 100 * (1 - sum(row[1] for row in rows) / sum(row[0] for row in rows))

    rng = random.Random(SEED)  # nosec B311
    samples = [reduction(rng.choices(valid, k=len(valid))) for _ in range(SAMPLES)]
    return {
        "value": reduction(valid),
        "ci95": [quantile(samples, 0.025), quantile(samples, 0.975)],
        "n": len(valid),
    }


def fmt(value, digits=3, signed=False):
    if value is None:
        return "n/a"
    return f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}"


def ci(item, digits=3, signed=False):
    value = fmt(item["value"], digits, signed)
    low, high = (fmt(bound, digits, signed) for bound in item["ci95"])
    return f"{value} [{low}, {high}]"


def table(headers, rows):
    def line(cells):
        return "| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |"

    return "\n".join([line(headers), line(["---"] * len(headers)), *map(line, rows)])


def paired(left, right):
    left_rows = {row["query_id"]: row for row in left["observations"]}
    right_rows = {row["query_id"]: row for row in right["observations"]}
    if (
        len(left_rows) != len(left["observations"])
        or len(right_rows) != len(right["observations"])
        or left_rows.keys() != right_rows.keys()
    ):
        raise ValueError("Comparison requires unique, identically paired query IDs")
    return [(left_rows[key], right_rows[key]) for key in sorted(left_rows)]


def retrieval_runs(data):
    runs = {
        (run["configuration"]["method"], run["configuration"]["examples"]): run
        for run in data["runs"]
        if run["configuration"]["method"] in METHODS
    }
    if set(runs) != {(method, examples) for method in METHODS for examples in (False, True)}:
        raise ValueError("Missing BM25/vector/RRF configuration")
    return runs


def knee_runs(data):
    runs = {(run["tool_count"], run["arm"]): run for run in data["runs"]}
    if set(runs) != {(size, arm) for size in SIZES for arm in ("full_load", "search_first")}:
        raise ValueError("Missing knee size or arm")
    return runs


def validate(real_retrieval, mock_retrieval, real_knee, mock_knee):
    if (
        real_retrieval["metadata"]["dataset_sha256"] != mock_retrieval["metadata"]["dataset_sha256"]
        or real_knee["dataset_sha256"] != mock_knee["dataset_sha256"]
    ):
        raise ValueError("Each real/mock experiment must use the same query dataset")
    if real_retrieval["metadata"]["mode"] != "real" or real_knee["mode"] != "real":
        raise ValueError("Real report inputs must identify real mode")
    if real_retrieval["metadata"]["rerank_profile"] is not None:
        raise ValueError("Real retrieval report must have rerank disabled")
    if any(run["configuration"]["method"] not in METHODS for run in real_retrieval["runs"]):
        raise ValueError("Unexpected real retrieval configuration")
    real_runs, mock_runs = retrieval_runs(real_retrieval), retrieval_runs(mock_retrieval)
    for key in real_runs:
        for real, mock in paired(real_runs[key], mock_runs[key]):
            if any(
                real[field] != mock[field]
                for field in ("language", "permission_group", "relevant_ids", "allowed_ids")
            ):
                raise ValueError("Real/mock retrieval labels differ")
        real_overall = real_runs[key]["metrics"]["overall"]
        mock_overall = mock_runs[key]["metrics"]["overall"]
        cohorts = ("primary", "more_than_5", "empty")
        if (
            real_overall["n"] != real_retrieval["metadata"]["query_count"]
            or mock_overall["n"] != mock_retrieval["metadata"]["query_count"]
            or sum(real_overall[cohort]["n"] for cohort in cohorts) != real_overall["n"]
            or sum(mock_overall[cohort]["n"] for cohort in cohorts) != mock_overall["n"]
            or any(real_overall[cohort]["n"] != mock_overall[cohort]["n"] for cohort in cohorts)
        ):
            raise ValueError("Unexpected or unmatched retrieval cohort counts")
    real_arms, mock_arms = knee_runs(real_knee), knee_runs(mock_knee)
    query_sets = []
    for size in SIZES:
        for arms in (real_arms, mock_arms):
            pairs = paired(arms[size, "full_load"], arms[size, "search_first"])
            if len(pairs) != 30:
                raise ValueError("Knee comparison requires 30 paired queries per size")
            if any(full["expected_id"] != search["expected_id"] for full, search in pairs):
                raise ValueError("Knee arms disagree on truth")
            query_sets.append({full["query_id"] for full, _ in pairs})
        for real, mock in paired(real_arms[size, "full_load"], mock_arms[size, "full_load"]):
            if (real["expected_id"], real["candidate_ids"]) != (
                mock["expected_id"],
                mock["candidate_ids"],
            ):
                raise ValueError("Real/mock knee catalogs differ")
    if any(query_set != query_sets[0] for query_set in query_sets):
        raise ValueError("Knee sizes must reuse the same query sample")


def retrieval_score(row, metric):
    truth = set(row["relevant_ids"]) & set(row["allowed_ids"])
    if not 1 <= len(truth) <= 5:
        return None
    retrieved = row["retrieved_ids"][:5] if row["error"] is None else []
    if metric == "recall":
        return len(set(retrieved) & truth) / len(truth)
    if metric == "hit":
        return float(bool(set(retrieved) & truth))
    return next((1 / rank for rank, value in enumerate(retrieved, 1) if value in truth), 0.0)


def example_delta(runs, method, metric):
    values = []
    for without, with_examples in paired(runs[method, False], runs[method, True]):
        if any(without[field] != with_examples[field] for field in ("relevant_ids", "allowed_ids")):
            raise ValueError("Example comparison labels differ")
        before = retrieval_score(without, metric)
        after = retrieval_score(with_examples, metric)
        if before is not None:
            values.append(after - before)
    return estimate(values)


def modes(counts):
    # Only known mode labels are rendered; source error bodies/IDs stay private.
    allowed = {"bm25", "vector", "rrf", "error", "not_applicable"}
    if not set(counts) <= allowed:
        raise ValueError("Unexpected search mode")
    return ", ".join(f"{name}: {counts[name]}" for name in sorted(counts))


def retrieval_report(real_data, mock_data):
    real, mock = retrieval_runs(real_data), retrieval_runs(mock_data)
    configurations = [(method, examples) for examples in (False, True) for method in METHODS]
    counts = real[configurations[0]]["metrics"]["overall"]
    total, primary, more, empty = (
        counts["n"],
        counts["primary"]["n"],
        counts["more_than_5"]["n"],
        counts["empty"]["n"],
    )
    tools, gateways = real_data["metadata"]["tool_count"], real_data["metadata"]["gateway_count"]

    def label(key):
        return f"{key[0].upper()} / {'with' if key[1] else 'without'} examples"

    recalls = {
        key: real[key]["metrics"]["overall"]["primary"]["recall_at_5"]["value"]
        for key in configurations
    }
    best_recall = max(recalls.values())
    best_labels = ", ".join(
        label(key)
        for key, value in recalls.items()
        if math.isclose(value, best_recall, abs_tol=1e-12)
    )
    deltas = {method: recalls[method, True] - recalls[method, False] for method in METHODS}
    direction = (
        "decreased observed Recall@5 for every method"
        if all(value < 0 for value in deltas.values())
        else "increased observed Recall@5 for every method"
        if all(value > 0 for value in deltas.values())
        else "left observed Recall@5 unchanged for every method"
        if all(value == 0 for value in deltas.values())
        else "had mixed effects on observed Recall@5 across methods"
    )
    example_changes = "; ".join(
        f"{method.upper()} {100 * deltas[method]:+.2f} percentage points" for method in METHODS
    )
    lead = (
        f"Among the six configurations, **{best_labels}** had the highest observed "
        + f"primary-cohort Recall@5 ({best_recall:.3f}) on the {primary} primary queries. "
        + f"Adding the fixed examples {direction} ({example_changes}). These are "
        + "descriptive results for this sample; the paired intervals below show "
        + "sampling uncertainty."
    )
    parts = [
        "# Synthetic retrieval: real model and offline mock",
        f"> **{DISCLAIMER}**",
        lead,
        (
            f"Matched corpus: {tools} tools across {gateways} synthetic Gateways; "
            + f"{total} queries per configuration. Real vector retrieval uses "
            + "the embedding model used in this experiment; BM25 is local "
            + "and therefore remains a lexical control. The mock embedding is a deterministic "
            + "regression fixture, not a simulation of real model quality. Rerank is disabled "
            + "in every real run. The mock baseline contains separate rerank rows; those rows "
            + "are excluded from this comparison. `configured-eval-v1` is a local configuration "
            + "label, not a provider-pinned backend revision; the embedding and chat "
            + "model aliases may evolve."
        ),
        (
            f"The primary comparison uses the same {total}-query deterministic evaluation "
            + "round-robin strata in real and mock modes. Metrics are macro averages "
            + "within the selected query sample, without "
            + "reweighting to the full dataset's stratum proportions."
        ),
        (
            "Queries and descriptions share synthetic template vocabulary; "
            "there is no independent human holdout. The with-examples arm uses "
            "the dataset's fixed, versioned offline mock example "
            + "queries. No actual LLM generated those examples during this run. This isolates "
            + "the effect of indexing those particular examples, and does not evaluate "
            + "live example generation."
        ),
        (
            "Sources: [real observations](real-synthetic.json), "
            + "[matched offline mock](mock/offline-regression.json), "
            + "[run configuration](run-config.json), [cost and usage](usage-cost.md), "
            + "[tool-count comparison](knee-comparison.md)."
        ),
        "## Retrieval quality and denominators",
        (
            f"Primary metrics use only the {primary} queries with 1–5 authorized relevant tools. "
            + f"The other cohorts are {more} queries with more than 5 and {empty} with none. "
            + "Truth is the "
            + "intersection of relevant tools and allowed tools. Recall@5 is macro recall; "
            + "Hit@5 indicates any relevant top-5 result; MRR@5 is truncated at rank 5. "
            + "Errors remain in denominators and contribute zero retrieval quality. Values "
            + "are on a 0–1 scale with the query-bootstrap 95% CI in brackets."
        ),
    ]
    quality_headers = [
        "Configuration",
        "Real Recall@5",
        "Mock Recall@5",
        "Real Hit@5",
        "Mock Hit@5",
        "Real MRR@5",
        "Mock MRR@5",
    ]
    for cohort, title in (
        ("primary", f"Primary cohort: n = {primary}"),
        ("more_than_5", f">5 cohort: n = {more}"),
    ):
        parts.append(f"### {title}")
        rows = []
        for key in configurations:
            rows.append(
                [
                    label(key),
                    *[
                        ci(runs[key]["metrics"]["overall"][cohort][metric])
                        for metric in ("recall_at_5", "hit_at_5", "mrr_at_5")
                        for runs in (real, mock)
                    ],
                ]
            )
        parts.append(table(quality_headers, rows))
    ceiling = real[configurations[0]]["metrics"]["overall"]["more_than_5"][
        "theoretical_recall_ceiling"
    ]
    parts.append(
        f"For the >5 cohort, the corpus-specific macro Recall@5 ceiling is {ci(ceiling)} "
        + "because no more than five relevant tools can be returned. This cohort is kept "
        + "separate from the primary denominator."
    )
    parts += [
        "## Paired effect of fixed examples",
        (
            "Each delta is **with examples minus without examples**, paired by the same "
            + f"{primary} primary query IDs. All deltas are on the original 0–1 metric scale. "
            + "These additional 95% CIs use 1,000 query-pair bootstrap samples with seed "
            + "20261010 and linear percentile bounds. They describe only this synthetic "
            + "query sample and do not remove label or example-generation bias."
        ),
        table(
            [
                "Method",
                "Real ΔRecall@5",
                "Mock ΔRecall@5",
                "Real ΔHit@5",
                "Mock ΔHit@5",
                "Real ΔMRR@5",
                "Mock ΔMRR@5",
            ],
            [
                [
                    method.upper(),
                    *[
                        ci(example_delta(runs, method, metric), signed=True)
                        for metric in ("recall", "hit", "mrr")
                        for runs in (real, mock)
                    ],
                ]
                for method in METHODS
            ],
        ),
        "## Errors, authorization, and empty truth",
        (
            f"The following counts cover all {total} matched queries. "
            + "Unauthorized results count every "
            + "returned tool outside the allowed set, including ranks after five."
        ),
        table(
            [
                "Configuration",
                "Real n",
                "Mock n",
                "Real errors",
                "Mock errors",
                "Real unauthorized results",
                "Mock unauthorized results",
            ],
            [
                [
                    label(key),
                    *[
                        runs[key]["metrics"]["overall"][field]
                        for field in ("n", "n_errors", "n_unauthorized_results")
                        for runs in (real, mock)
                    ],
                ]
                for key in configurations
            ],
        ),
        (
            f"For the {empty} queries with zero authorized relevant tools, a false recommendation "
            + "means any top-5 result; correct empty means no result and no request error. "
            + "This measures retrieval behavior, not an agent's refusal quality."
        ),
        table(
            [
                "Configuration",
                "Real false recommendation",
                "Mock false recommendation",
                "Real correct empty",
                "Mock correct empty",
            ],
            [
                [
                    label(key),
                    *[
                        ci(runs[key]["metrics"]["overall"]["empty"][metric])
                        for metric in ("false_recommendation_rate", "correct_empty_response_rate")
                        for runs in (real, mock)
                    ],
                ]
                for key in configurations
            ],
        ),
        "## Latency and actual retrieval modes",
        (
            "Latency is warm local retrieval elapsed time, including query embedding provider "
            + "calls when used and retaining failed requests. Index build is excluded. "
            + "It is not Gateway end-to-end latency or a production network SLO; mock timing "
            + f"has no provider round trip. p50/p95 use all {total} matched queries; "
            + "units are milliseconds."
        ),
        table(
            [
                "Configuration",
                "Real p50 [95% CI]",
                "Mock p50 [95% CI]",
                "Real p95 [95% CI]",
                "Mock p95 [95% CI]",
            ],
            [
                [
                    label(key),
                    *[
                        ci(runs[key]["metrics"]["overall"]["latency_ms"][percentile], 2)
                        for percentile in ("p50", "p95")
                        for runs in (real, mock)
                    ],
                ]
                for key in configurations
            ],
        ),
        table(
            ["Configured retrieval", "Real observed modes", "Mock observed modes"],
            [
                [label(key), modes(real[key]["mode_counts"]), modes(mock[key]["mode_counts"])]
                for key in configurations
            ],
        ),
        (
            "Configured RRF can report BM25 for queries with an empty authorized catalog "
            + "or a degradation path. The observed mode table is authoritative for execution "
            + "counts; a configured method name alone does not establish that a vector "
            + "provider was used for every query. No business tool was executed."
        ),
        f"> **{DISCLAIMER}**",
    ]
    return "\n\n".join(parts) + "\n"


def candidate_coverage(run):
    rows = run["observations"]
    count = sum(row["expected_id"] in row["candidate_ids"] for row in rows)
    return f"{count}/{len(rows)}"


def outside_candidates(run):
    return sum(
        row["selected_id"] is not None and row["selected_id"] not in row["candidate_ids"]
        for row in run["observations"]
    )


def search_latency(run, probability):
    return quantile([row["search_ms"] for row in run["observations"]], probability)


def knee_report(real_data, mock_data):
    real, mock = knee_runs(real_data), knee_runs(mock_data)
    configurations = [(size, arm) for size in SIZES for arm in ("full_load", "search_first")]
    arm_label = {"full_load": "Full load", "search_first": "Search first"}
    latency_deltas = {
        size: real[size, "search_first"]["latency_ms"]["p50"]["value"]
        - real[size, "full_load"]["latency_ms"]["p50"]["value"]
        for size in SIZES
    }
    accuracy_deltas = {
        size: real[size, "search_first"]["accuracy"]["value"]
        - real[size, "full_load"]["accuracy"]["value"]
        for size in SIZES
    }
    crossings = [
        (left, right)
        for left, right in zip(SIZES, SIZES[1:], strict=False)
        if latency_deltas[left] >= 0 > latency_deltas[right]
    ]
    faster = [str(size) for size in SIZES if latency_deltas[size] < 0]
    preserves_accuracy = [
        size for size in SIZES if latency_deltas[size] < 0 and accuracy_deltas[size] >= 0
    ]
    if crossings:
        brackets = ", ".join(f"{left} and {right}" for left, right in crossings)
        latency_summary = (
            "The observed p50 elapsed-latency crossover in favor of search first "
            + f"is bracketed by {brackets} tools in the tested grid. "
        )
    else:
        latency_summary = "No sampled slower-to-faster p50 latency crossover was observed. "
    latency_summary += (
        f"Search first had lower observed p50 at {', '.join(faster)} tools. "
        if faster
        else "Search first had no lower observed p50 in the tested grid. "
    )
    accuracy_summary = (
        "Its observed accuracy was lower at every tested size. "
        if all(value < 0 for value in accuracy_deltas.values())
        else "Accuracy differences varied across the tested sizes. "
    )
    largest = max(SIZES)
    accuracy_summary += (
        f"At {largest} tools, full-load accuracy was "
        + f"{100 * real[largest, 'full_load']['accuracy']['value']:.1f}% versus "
        + f"{100 * real[largest, 'search_first']['accuracy']['value']:.1f}% for search first. "
    )
    interpretation = (
        "No tested size combined lower p50 latency with preserved observed accuracy. "
        if not preserves_accuracy
        else "The accuracy and latency tables identify the sampled trade-offs. "
    )
    lead = (
        latency_summary
        + accuracy_summary
        + interpretation
        + "These are descriptive results on this synthetic sample, not a production knee."
    )
    parts = [
        "# Synthetic tool-count experiment: real model and offline mock",
        f"> **{DISCLAIMER}**",
        lead,
        (
            "Each size (20, 50, 100, 200, 500 tools) uses the same 30 paired queries: "
            + "150 query-size pairs and 300 selection attempts per provider mode. Each "
            + "catalog contains one authorized truth plus nested synthetic distractors. "
            + "Catalogs and queries are matched between real and mock modes and between "
            + "arms. The namespace prefix makes repeated CRUD functions distinguishable. "
            + "Real selection uses the chat model used in this experiment; "
            + "search first uses the embedding model from the same experiment "
            + "with configured RRF, followed by one selection chat over retrieved candidates. "
            + "Rerank remains disabled. No business tool is executed. `configured-eval-v1` "
            + "is a local configuration label, not a provider-pinned backend revision; "
            + "the model aliases may evolve."
        ),
        (
            "Knee queries are selected from the full source dataset, independently of "
            + "the smaller matched retrieval subset. Real and mock knee dataset hashes "
            + "match each other; they need not equal the retrieval-subset hashes. "
            + "These rows describe only the supplied completed result files. "
            + "Provider attempts and reported usage are summarized separately in "
            + "[cost and usage](usage-cost.md)."
        ),
        (
            "The fixed experiment order is **full load first, then search first for each "
            + "query**. Repeated queries, shared prompts, provider-side prompt caching, "
            + "warm-up, and time-varying network/model conditions can confound latency and "
            + "cost differences. There is no randomized arm-order or cold-cache control. "
            + "Provider cache counters, when available, are in the usage evidence. "
            + "The mock selector is a deterministic regression fixture, not a model-quality "
            + "or latency surrogate."
        ),
        (
            "Sources: [real observations](real-knee.json), "
            + "[matched offline mock](mock/offline-knee.json), "
            + "[retrieval comparison](retrieval-comparison.md), "
            + "[run configuration](run-config.json), [cost and usage](usage-cost.md)."
        ),
        "## Accuracy, errors, and candidate truth",
        (
            "Accuracy measures selected tool identity only. Request/response errors count "
            + "as incorrect and stay in the 30-query denominator. Brackets contain reference "
            + "query-bootstrap 95% CIs. Candidate truth coverage below is recomputed from "
            + "recorded candidate IDs; it is distinct from the harness `truth_present_count`, "
            + "which describes truth inserted into the source catalog. If retrieval fails "
            + "before updating candidates, the harness retains the initial catalog and "
            + "selection is not called, so recorded coverage alone does not prove that "
            + "selection received the truth."
        ),
        table(
            [
                "Tools",
                "Arm",
                "Real accuracy [95% CI]",
                "Mock accuracy [95% CI]",
                "Real errors",
                "Mock errors",
                "Real candidate truth",
                "Mock candidate truth",
            ],
            [
                [
                    size,
                    arm_label[arm],
                    ci(real[size, arm]["accuracy"]),
                    ci(mock[size, arm]["accuracy"]),
                    real[size, arm]["n_errors"],
                    mock[size, arm]["n_errors"],
                    candidate_coverage(real[size, arm]),
                    candidate_coverage(mock[size, arm]),
                ]
                for size, arm in configurations
            ],
        ),
        table(
            [
                "Tools",
                "Arm",
                "Real candidate range",
                "Mock candidate range",
                "Real selections outside candidates",
                "Mock selections outside candidates",
            ],
            [
                [
                    size,
                    arm_label[arm],
                    *[
                        (
                            f"{runs[size, arm]['candidate_tools']['min']}–"
                            + f"{runs[size, arm]['candidate_tools']['max']}"
                        )
                        for runs in (real, mock)
                    ],
                    outside_candidates(real[size, arm]),
                    outside_candidates(mock[size, arm]),
                ]
                for size, arm in configurations
            ],
        ),
        "## Chat tokens and coverage",
        (
            "Real values below are **provider-reported chat tokens retained in selection "
            + "observations**, displayed as mean [95% CI]; `n/30` is field coverage. Missing "
            + "usage is n/a, never zero. Mock values are serialized prompt UTF-8 bytes/4 "
            + "estimates, not model tokens, and cannot be used to infer real token savings. "
            + "Search embedding usage is separate. The [usage ledger](usage-ledger.jsonl) "
            + "and [cost report](usage-cost.md) additionally retain numeric provider "
            + "usage from responses that failed later adapter validation; those requests "
            + "can be billable even when selection-level usage is absent."
        ),
        table(
            [
                "Tools",
                "Arm",
                "Real prompt tokens; coverage",
                "Real completion tokens; coverage",
                "Real total tokens; coverage",
                "Mock prompt bytes/4; coverage",
            ],
            [
                [
                    size,
                    arm_label[arm],
                    *[
                        (
                            f"{ci(real[size, arm]['mean_reported_tokens'][field], 1)}; "
                            + f"{real[size, arm]['mean_reported_tokens'][field]['n']}/30"
                        )
                        for field in ("prompt_tokens", "completion_tokens", "total_tokens")
                    ],
                    (
                        f"{ci(mock[size, arm]['mean_estimated_prompt_tokens'], 1)}; "
                        + f"{mock[size, arm]['mean_estimated_prompt_tokens']['n']}/30"
                    ),
                ]
                for size, arm in configurations
            ],
        ),
        "## Paired arm differences",
        (
            "Accuracy and elapsed-time deltas are **search first minus full load** on "
            + "the same queries. Negative latency favors search first. The median elapsed "
            + "delta is the median of query-pair differences, not the difference of the "
            + "two marginal medians. The real prompt-token reduction is "
            + "`100 × (1 − sum(search prompt tokens) / sum(full-load prompt tokens))`, "
            + "using only pairs with provider-reported prompt counts in both arms and "
            + "positive full-load counts. Positive reduction means fewer prompt tokens; "
            + "it excludes search embedding tokens, completion tokens, and cache pricing."
        ),
        (
            "Additional paired 95% CIs use 1,000 query-pair bootstrap samples with "
            + "seed 20261010 and linear percentile bounds. Sizes reuse the same queries, "
            + "so observations across sizes are not independent; intervals do not establish "
            + "a production threshold or correct synthetic sample bias."
        ),
    ]
    difference_rows = []
    token_rows = []
    for size in SIZES:
        by_mode = {
            mode: paired(runs[size, "full_load"], runs[size, "search_first"])
            for mode, runs in (("real", real), ("mock", mock))
        }
        difference_rows.append(
            [
                size,
                *[
                    ci(
                        estimate(
                            [
                                float(search["correct"]) - float(full["correct"])
                                for full, search in by_mode[mode]
                            ]
                        ),
                        signed=True,
                    )
                    for mode in ("real", "mock")
                ],
                *[
                    ci(
                        estimate(
                            [
                                search["latency_ms"] - full["latency_ms"]
                                for full, search in by_mode[mode]
                            ],
                            statistic,
                        ),
                        2,
                        True,
                    )
                    for statistic in ("mean", "p50")
                    for mode in ("real", "mock")
                ],
            ]
        )
        reduction = token_reduction(by_mode["real"])
        token_rows.append([size, ci(reduction, 2), f"{reduction['n']}/30"])
    parts += [
        table(
            [
                "Tools",
                "Real Δaccuracy",
                "Mock Δaccuracy",
                "Real mean Δms",
                "Mock mean Δms",
                "Real median Δms",
                "Mock median Δms",
            ],
            difference_rows,
        ),
        table(
            ["Tools", "Real paired prompt-token reduction % [95% CI]", "Complete pairs"],
            token_rows,
        ),
        "## Latency including search",
        (
            "Elapsed time includes retrieval (when present) and the selection call; "
            + "index build and business-tool execution are excluded. Failed attempts remain "
            + "in latency summaries. Units are milliseconds. These measurements include "
            + "provider/network conditions of this run and are not Gateway latency SLOs."
        ),
        table(
            [
                "Tools",
                "Arm",
                "Real p50 [95% CI]",
                "Mock p50 [95% CI]",
                "Real p95 [95% CI]",
                "Mock p95 [95% CI]",
            ],
            [
                [
                    size,
                    arm_label[arm],
                    *[
                        ci(runs[size, arm]["latency_ms"][percentile], 2)
                        for percentile in ("p50", "p95")
                        for runs in (real, mock)
                    ],
                ]
                for size, arm in configurations
            ],
        ),
        (
            "The search-only component is already included in elapsed time above and "
            + "must not be added again. A search that fails before completion can retain "
            + "a zero `search_ms` in the harness; the full elapsed attempt still appears "
            + "in the preceding table."
        ),
        table(
            [
                "Tools",
                "Real search p50 / p95 ms",
                "Mock search p50 / p95 ms",
                "Real observed search modes",
                "Mock observed search modes",
            ],
            [
                [
                    size,
                    *[
                        (
                            f"{fmt(search_latency(runs[size, 'search_first'], 0.5), 2)} / "
                            + f"{fmt(search_latency(runs[size, 'search_first'], 0.95), 2)}"
                        )
                        for runs in (real, mock)
                    ],
                    modes(real[size, "search_first"]["search_mode_counts"]),
                    modes(mock[size, "search_first"]["search_mode_counts"]),
                ]
                for size in SIZES
            ],
        ),
        "## Interpretation boundary",
        (
            "The paired rows describe the observed accuracy, prompt volume, and elapsed "
            + "time trade-off for these 30 synthetic queries and catalog sizes. A tool-count "
            + "point with lower prompt volume or latency is not by itself an accuracy knee "
            + "or a recommended production cutoff. Candidate truth loss, response errors, "
            + "the small shared sample, arm order, provider caching, and synthetic labels "
            + "must be considered together. No actual LLM-generated example queries are "
            + "evaluated here, and this experiment does not exercise a business tool."
        ),
        f"> **{DISCLAIMER}**",
    ]
    return "\n\n".join(parts) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    directory = args.report_dir
    real_retrieval = read(directory / "real-synthetic.json")
    mock_retrieval = read(directory / "mock" / "offline-regression.json")
    real_knee = read(directory / "real-knee.json")
    mock_knee = read(directory / "mock" / "offline-knee.json")
    validate(real_retrieval, mock_retrieval, real_knee, mock_knee)
    outputs = {
        "retrieval-comparison.md": retrieval_report(real_retrieval, mock_retrieval),
        "knee-comparison.md": knee_report(real_knee, mock_knee),
    }
    for name, content in outputs.items():
        (directory / name).write_text(content, encoding="utf-8")
    print("Wrote retrieval-comparison.md and knee-comparison.md.")


if __name__ == "__main__":
    main()

"""Summarize sanitized provider usage without credentials, network, or library edits."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

DISCLAIMER = "这是合成数据上的真实模型参考结果，不代表质量验收，也不构成开启 rerank 的依据。"
TOKEN_KEYS = ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens", "total_tokens")
MILLION = Decimal(1_000_000)
SOURCES = {
    "prices": "pricing-sources.md",
    "embedding_usage": "pricing-sources.md",
    "chat_usage": "pricing-sources.md",
    "cache": "pricing-sources.md",
}


def integer(value):
    return value if type(value) is int and value >= 0 else None


def alias(usage, *keys):
    values = [usage[key] for key in keys if integer(usage.get(key)) is not None]
    return (values[0] if values else None), len(set(values)) > 1


def complete_response(row):
    # The observer sets this marker only after receiving and decoding the full body.
    return (
        row.get("status") == 200
        and not row.get("transport_error")
        and "model_echo_matches_request" in row
    )


def validate_prices(prices):
    if not isinstance(prices, dict) or set(prices) - {"embedding", "chat", "rerank"}:
        raise ValueError("prices must map model roles to input/output rates")
    for rates in prices.values():
        if (
            not isinstance(rates, (list, tuple))
            or len(rates) != 2
            or any(
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
                for value in rates
            )
        ):
            raise ValueError("invalid configured price")


def analyze(row, *, prices=None):
    """Price each response independently; total is never added to input/output."""
    usage = {
        key: value
        for key, value in row.get("usage", {}).items()
        if key in TOKEN_KEYS and integer(value) is not None
    }
    details = row.get("usage_details", {})
    prompt_details = details.get("prompt_tokens_details", {})
    completion_details = details.get("completion_tokens_details", {})
    cached = integer(prompt_details.get("cached_tokens"))
    reasoning = integer(completion_details.get("reasoning_tokens"))
    incoming, input_conflict = alias(usage, "prompt_tokens", "input_tokens")
    outgoing, output_conflict = alias(usage, "completion_tokens", "output_tokens")
    source = next((key for key in ("prompt_tokens", "input_tokens") if key in usage), None)
    issues = []
    if row["kind"] == "embedding":
        if incoming is None and outgoing in (None, 0):
            incoming = usage.get("total_tokens")
            source = "total_tokens" if incoming is not None else None
        if outgoing not in (None, 0):
            issues.append("embedding_reported_output")
        outgoing = 0
    if input_conflict or output_conflict:
        issues.append("conflicting_usage_aliases")
    if incoming is None or outgoing is None:
        issues.append("missing_input_or_output_usage")
    elif "total_tokens" in usage and usage["total_tokens"] != incoming + outgoing:
        issues.append("inconsistent_total_tokens")
    if cached is not None and incoming is not None and cached > incoming:
        issues.append("cached_tokens_exceed_input")
    if reasoning is not None and outgoing is not None and reasoning > outgoing:
        issues.append("reasoning_tokens_exceed_output")

    reasons = list(issues)
    if row.get("status") != 200:
        reasons.append("non_200_or_no_response_cost_unknown")
    if row.get("transport_error"):
        reasons.append("transport_error_cost_unknown")
    if row.get("status") == 200 and not complete_response(row):
        reasons.append("complete_response_body_not_observed")
    rate = None
    rates = (prices or {}).get(row["kind"])
    if rates is None:
        reasons.append("missing_price")
    else:
        validate_prices({row["kind"]: rates})
        rate = tuple(Decimal(str(value)) for value in rates)
    cost = (
        None
        if reasons or rate is None
        else (Decimal(incoming) * rate[0] + Decimal(outgoing) * rate[1]) / MILLION
    )
    return {
        "row": row,
        "usage": usage,
        "input": incoming,
        "output": outgoing,
        "input_source": source,
        "issues": issues,
        "unknown_cost_reasons": reasons,
        "cached": cached,
        "reasoning": reasoning,
        "cost": cost,
    }


def amount(value):
    return float(value.quantize(Decimal("0.0000000001")))


def aggregate(items):
    statuses = Counter(str(item["row"].get("status")) for item in items)
    complete_count = sum(complete_response(item["row"]) for item in items)
    transport_errors = Counter(
        item["row"]["transport_error"] for item in items if item["row"].get("transport_error")
    )
    priced = [item for item in items if item["cost"] is not None]
    cost = sum((item["cost"] for item in priced), Decimal(0))
    return {
        "requests": len(items),
        "http_200_requests": statuses.get("200", 0),
        "http_200_complete_responses": complete_count,
        "failed_or_no_response_requests": len(items) - complete_count,
        "transport_error_requests": sum(transport_errors.values()),
        "transport_error_counts": dict(sorted(transport_errors.items())),
        "http_200_with_transport_error_requests": sum(
            item["row"].get("status") == 200 and bool(item["row"].get("transport_error"))
            for item in items
        ),
        "status_counts": dict(sorted(statuses.items())),
        "usage_present_requests": sum(bool(item["usage"]) for item in items),
        "missing_usage_requests": sum(not item["usage"] for item in items),
        "incomplete_or_inconsistent_usage_requests": sum(bool(item["issues"]) for item in items),
        "priced_requests": len(priced),
        "unknown_cost_requests": len(items) - len(priced),
        "unknown_cost_reason_counts": dict(
            sorted(
                Counter(reason for item in items for reason in item["unknown_cost_reasons"]).items()
            )
        ),
        "observed_input_tokens": sum(item["input"] or 0 for item in items),
        "observed_output_tokens": sum(item["output"] or 0 for item in items),
        "reported_usage_field_sums": {
            key: sum(item["usage"].get(key, 0) for item in items) for key in TOKEN_KEYS
        },
        "input_usage_source_counts": dict(
            sorted(Counter(item["input_source"] for item in items if item["input_source"]).items())
        ),
        "priced_input_tokens": sum(item["input"] for item in priced),
        "priced_output_tokens": sum(item["output"] for item in priced),
        "reasoning_tokens_reported": sum(item["reasoning"] or 0 for item in items),
        "cached_tokens_reported": sum(item["cached"] or 0 for item in items),
        "cache_detail_present_requests": sum(item["cached"] is not None for item in items),
        "standard_undiscounted_list_estimate_rmb": amount(cost)
        if len(priced) == len(items)
        else None,
        "known_cost_subtotal_rmb": amount(cost),
    }


def grouped(items, fields):
    groups = defaultdict(list)
    for item in items:
        groups[tuple(item["row"][field] for field in fields)].append(item)
    return [
        {**dict(zip(fields, key, strict=False)), **aggregate(group)}
        for key, group in sorted(groups.items())
    ]


def read_snapshot(directory):
    status_path = directory / "run-status.json"
    # Read status first so a concurrently completed run cannot label an old snapshot complete.
    status = json.loads(status_path.read_text()) if status_path.exists() else {"status": "running"}
    config = json.loads((directory / "run-config.json").read_text())
    raw = (directory / "usage-ledger.jsonl").read_bytes()
    pending_tail = bool(raw and not raw.endswith(b"\n"))
    lines = raw.splitlines()
    if pending_tail:
        lines = lines[:-1]
    rows = [json.loads(line) for line in lines if line.strip()]
    sequences = [row["sequence"] for row in rows]
    if len(sequences) != len(set(sequences)) or sequences != list(range(1, len(rows) + 1)):
        raise ValueError(
            "Ledger sequence is duplicated or noncontiguous; refusing double counting."
        )
    expected = status.get("http_attempts", {})
    counts = Counter(row["kind"] for row in rows)
    reconciled = (
        all(expected.get(f"http_{kind}_attempts") == counts[kind] for kind in ("embedding", "chat"))
        if expected
        else False
    )
    completed = (
        status.get("status") == "completed"
        and status.get("exit_code") == 0
        and reconciled
        and not pending_tail
    )
    return (
        rows,
        config,
        {
            "state": "completed" if completed else "partial",
            "declared_run_status": status.get("status", "unknown"),
            "declared_exit_code": status.get("exit_code"),
            "attempt_counts_reconciled": reconciled,
            "incomplete_final_line_excluded": pending_tail,
            "ledger_bytes": len(raw),
            "ledger_sha256": hashlib.sha256(raw).hexdigest(),
        },
    )


def formatted_amount(value):
    return "未知" if value is None else f"{value:.8f}"


def markdown(report):
    total = report["total"]
    state = (
        "完整运行"
        if report["snapshot"]["state"] == "completed"
        else "部分结果，运行尚未完成或快照未核对"
    )
    lines = [
        "# 调用量与费用",
        "",
        f"**{DISCLAIMER}**",
        "",
        f"**状态：{state}。** 统计时间（UTC）：{report['generated_at_utc']}。",
        "",
        (
            f"本快照包括 {total['requests']} 次 HTTP 尝试，其中配置探测 "
            + f"{report['configuration_probe_requests']} 次。公开价估算为 **人民币 "
            + f"{formatted_amount(total['standard_undiscounted_list_estimate_rmb'])} 元**，"
            + f"覆盖 {total['priced_requests']} 次有完整且一致 usage 的成功响应。"
            + f"另有 {total['unknown_cost_requests']} 次费用未知；可计算部分小计为 "
            + f"{total['known_cost_subtotal_rmb']:.8f} 元，"
            + "未知费用不按零处理。"
        ),
        "",
        "估算使用调用方提供的单价；未配置价格的请求费用未知。不扣免费额度或优惠，不是实际账单。",
        "",
        "| 阶段 | 类型 | 尝试 | 200 响应头 | 完整 200 响应 | 失败/未完整响应 | "
        "无 usage | usage 不完整/不一致 | 输入 token | 输出 token | 可估请求 | 元 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report["by_phase_and_kind"]:
        lines.append(
            f"| {row['phase']} | {row['kind']} | {row['requests']} | "
            + f"{row['http_200_requests']} | {row['http_200_complete_responses']} | "
            + f"{row['failed_or_no_response_requests']} | "
            + f"{row['missing_usage_requests']} | "
            + f"{row['incomplete_or_inconsistent_usage_requests']} | "
            + f"{row['observed_input_tokens']} | {row['observed_output_tokens']} | "
            + f"{row['priced_requests']} | "
            + f"{formatted_amount(row['standard_undiscounted_list_estimate_rmb'])} |"
        )
    lines += [
        "",
        "| 模型 | 尝试 | 输入 token | 输出 token | 可估请求 | 费用未知请求 | 元 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report["by_model"]:
        lines.append(
            f"| {row['requested_model']} | {row['requests']} | "
            + f"{row['observed_input_tokens']} | "
            + f"{row['observed_output_tokens']} | {row['priced_requests']} | "
            + f"{row['unknown_cost_requests']} | "
            + f"{formatted_amount(row['standard_undiscounted_list_estimate_rmb'])} |"
        )
    lines += [
        "",
        (
            "Embedding + rerank 尝试数：**"
            + f"{report['request_budget']['embedding_plus_rerank_attempts']} / 2,000**；"
            + f"rerank 请求数：**{report['request_budget']['rerank_attempts']}**。"
        ),
        "",
        "HTTP 状态计数："
        + json.dumps(total["status_counts"], ensure_ascii=False)
        + (
            "。"
            + "无 usage 与 usage 不完整/不一致两列可重叠；详细状态与原始数值字段分别汇总于 JSON，"
            "不能将各字段再次相加。"
        ),
        (
            f"HTTP 200 响应头不等于完整响应。传输错误共 {total['transport_error_requests']} 次，"
            + f"其中 {total['http_200_with_transport_error_requests']} 次在收到 200 响应头后失败；"
            + "这些尝试计入请求总量，费用仍为未知。完整响应以观察器解码完整响应体后设置的标记确认。"
        ),
        "",
        (
            "费用按每次请求计算。Embedding 优先使用 `prompt_tokens`，其次 `input_tokens`，"
            + "均缺失且无输出 token 时才使用 `total_tokens`。Chat 分别使用输入与输出字段；"
            + "`total_tokens` 不再相加，`reasoning_tokens` 也不额外计数。"
            + "非 200、传输错误、未观察到完整响应体、缺 usage、字段矛盾的请求均标为费用未知。"
        ),
    ]
    lines += ["", "| 类型 | 输入元/百万 token | 输出元/百万 token |", "| --- | ---: | ---: |"]
    for kind, rates in sorted(report["prices_cny_per_million_tokens"].items()):
        lines.append(f"| {kind} | {rates[0]} | {rates[1]} |")
    lines.append("缓存计数仅用于 usage 核查；未配置缓存价目，不推算缓存折扣。")
    if total["unknown_cost_reason_counts"]:
        lines += [
            "",
            "费用未知原因（同一请求可能有多个原因）：`"
            + json.dumps(total["unknown_cost_reason_counts"], ensure_ascii=False, sort_keys=True)
            + "`。",
        ]
    lines += [
        "",
        (
            f"[公开价格]({SOURCES['prices']}) · "
            + f"[Embedding usage]({SOURCES['embedding_usage']}) · "
            + f"[Chat usage]({SOURCES['chat_usage']}) · [缓存计费]({SOURCES['cache']})"
        ),
        "",
        "单价须按模型服务的公开价目与合同配置，阶梯或复杂合同需另行核算。",
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--prices-json", type=Path)
    args = parser.parse_args()
    rows, _config, snapshot = read_snapshot(args.report_dir)
    prices = json.loads(args.prices_json.read_text()) if args.prices_json else None
    if prices is not None:
        validate_prices(prices)
    items = [analyze(row, prices=prices) for row in rows]
    kinds = Counter(row["kind"] for row in rows)
    report = {
        "disclaimer": DISCLAIMER,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "snapshot": snapshot,
        "pricing_basis": "Caller-configured rates",
        "prices_cny_per_million_tokens": prices or {},
        "http_200_requests_are_header_status_only": True,
        "sources": SOURCES,
        "configuration_probe_requests": sum(
            row["phase"].endswith("configuration_probe") for row in rows
        ),
        "request_budget": {
            "limit": 2000,
            "embedding_plus_rerank_attempts": kinds["embedding"] + kinds["rerank"],
            "rerank_attempts": kinds["rerank"],
            "within_limit": kinds["embedding"] + kinds["rerank"] <= 2000,
        },
        "total": aggregate(items),
        "by_phase_and_kind": grouped(items, ("phase", "kind")),
        "by_kind": grouped(items, ("kind",)),
        "by_model": grouped(items, ("requested_model",)),
    }
    encoded_json = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    encoded_markdown = markdown(report)
    (args.report_dir / "usage-cost.json").write_text(encoded_json)
    (args.report_dir / "usage-cost.md").write_text(encoded_markdown)
    print(
        json.dumps(
            {
                "state": snapshot["state"],
                "requests": len(rows),
                "priced_requests": report["total"]["priced_requests"],
                "unknown_cost_requests": report["total"]["unknown_cost_requests"],
                "estimate_rmb": report["total"]["standard_undiscounted_list_estimate_rmb"],
            }
        )
    )


if __name__ == "__main__":
    main()

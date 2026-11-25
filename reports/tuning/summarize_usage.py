"""Independently audit completed sanitized ledgers; never infer a proxy invoice."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

CAPS = {"embedding": 1500, "rerank": 1000, "chat": 500}
PHASES = ("preflight", "dev", "test", "test-knee")
TOKEN_KEYS = ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens", "total_tokens")
ZERO = Decimal(0)
MILLION = Decimal(1_000_000)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("expected sanitized JSON object")
    return cast(dict[str, object], value)


def _amount(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.0000000001")))


def _usage(value: object) -> tuple[dict[str, int], bool]:
    if not isinstance(value, dict):
        return {}, False
    tokens = {
        key: value[key] for key in TOKEN_KEYS if type(value.get(key)) is int and value[key] >= 0
    }
    valid = all(
        key not in value or (type(value[key]) is int and value[key] >= 0) for key in TOKEN_KEYS
    )
    return tokens, valid


def _price(kind: str, incoming: int, outgoing: int, prices: Mapping[str, object]) -> Decimal:
    rates = prices.get(kind)
    if (
        not isinstance(rates, list | tuple)
        or len(rates) != 2
        or any(
            isinstance(rate, bool)
            or not isinstance(rate, int | float)
            or not math.isfinite(rate)
            or rate < 0
            for rate in rates
        )
    ):
        raise ValueError("invalid configured price")
    rate_in, rate_out = Decimal(str(rates[0])), Decimal(str(rates[1]))
    return (incoming * rate_in + outgoing * rate_out) / MILLION


def _independent_cost(
    row: Mapping[str, object],
    kind: str,
    tokens: Mapping[str, int],
    valid: bool,
    prices: Mapping[str, object],
) -> tuple[Decimal | None, str]:
    if row.get("complete_response") is not True:
        return None, "incomplete_response"
    if not valid or row.get("usage_status") == "invalid":
        return None, "invalid_usage"
    if "model" in row and (not isinstance(row["model"], str) or not row["model"].strip()):
        return None, "invalid_model"
    for left, right in (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
    ):
        if left in tokens and right in tokens and tokens[left] != tokens[right]:
            return None, "aliases_disagree"
    incoming = tokens.get("prompt_tokens", tokens.get("input_tokens"))
    outgoing = tokens.get("completion_tokens", tokens.get("output_tokens"))
    if kind != "chat":
        if outgoing not in (None, 0):
            return None, "unexpected_output_tokens"
        outgoing = 0
        incoming = incoming if incoming is not None else tokens.get("total_tokens")
    if incoming is None or outgoing is None:
        return None, "missing_usage"
    if "total_tokens" in tokens and tokens["total_tokens"] != incoming + outgoing:
        return None, "totals_disagree"
    cached = row.get("cached_tokens")
    if cached is not None and (type(cached) is not int or not 0 <= cached <= incoming):
        return None, "invalid_cached_tokens"
    if row.get("usage_status", "reported") != "reported":
        return None, "recorded_usage_uncertainty"
    if kind not in prices:
        return None, "missing_price"
    return _price(kind, incoming, outgoing, prices), ""


def _sensitivity_tokens(
    row: Mapping[str, object], kind: str, tokens: Mapping[str, int], valid: bool
) -> int | None:
    """Only the observed conflicting rerank pattern receives this hypothetical price."""
    prompt = tokens.get("prompt_tokens", 0)
    if (
        valid
        and kind == "rerank"
        and row.get("complete_response") is True
        and row.get("usage_status", "reported") in ("reported", "inconsistent")
        and prompt > 0
        and tokens.get("total_tokens") == prompt
        and all(
            tokens.get(key) == 0
            for key in TOKEN_KEYS
            if key not in ("prompt_tokens", "total_tokens")
        )
    ):
        return prompt
    return None


@dataclass
class Group:
    phase: str
    kind: str
    attempts: int = 0
    complete_responses: int = 0
    unknown_cost_requests: int = 0
    known_cost: Decimal = ZERO
    sensitivity: Decimal = ZERO
    sensitivity_requests: int = 0
    sensitivity_tokens: int = 0
    http_ms: Decimal = ZERO
    clock_observations: int = 0
    usage: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "kind": self.kind,
            "attempts": self.attempts,
            "complete_responses": self.complete_responses,
            "unknown_cost_requests": self.unknown_cost_requests,
            "known_public_list_price_cny": _amount(self.known_cost),
            "sensitivity_addition_cny": _amount(self.sensitivity),
            "sensitivity_covered_requests": self.sensitivity_requests,
            "sensitivity_prompt_total_tokens": self.sensitivity_tokens,
            "http_stage_ms_sum": float(self.http_ms),
            "clock_observations": self.clock_observations,
            "usage": dict(self.usage),
            "unknown_reasons": dict(self.reasons),
        }


def build_audit(
    raw_rows: Sequence[object],
    summary: Mapping[str, object],
) -> dict[str, object]:
    """Compute costs and reconcile source counters without importing Ledger."""
    prices = (
        _object(summary["prices_cny_per_million_tokens"])
        if "prices_cny_per_million_tokens" in summary
        else {}
    )
    if set(prices) - set(CAPS):
        raise ValueError("unknown price role")
    for kind in prices:
        _price(kind, 0, 0, prices)
    models = _object(summary["models"]) if "models" in summary else None
    groups: dict[tuple[str, str], Group] = {}
    sequence: list[object] = []
    counts = dict.fromkeys(CAPS, 0)
    reported_usage: dict[str, Counter[str]] = {}
    for raw in raw_rows:
        row = _object(raw)
        kind, phase = row.get("kind"), row.get("phase")
        if not isinstance(kind, str) or kind not in CAPS or phase not in PHASES:
            raise ValueError("unknown sanitized request kind or phase")
        assert isinstance(phase, str)
        sequence.append(row.get("sequence"))
        counts[kind] += 1
        group = groups.setdefault((phase, kind), Group(phase, kind))
        group.attempts += 1
        group.complete_responses += row.get("complete_response") is True
        tokens, valid = _usage(row.get("usage"))
        group.usage.update(tokens)
        reported_usage.setdefault(kind, Counter()).update(tokens)
        if models is not None and row.get("model") != models.get(kind):
            cost, reason = None, "model_mismatch"
        else:
            cost, reason = _independent_cost(row, kind, tokens, valid, prices)
        if cost is None:
            group.unknown_cost_requests += 1
            group.reasons[reason] += 1
            hypothetical = _sensitivity_tokens(row, kind, tokens, valid)
            if hypothetical is not None and reason != "model_mismatch" and "rerank" in prices:
                group.sensitivity += _price("rerank", hypothetical, 0, prices)
                group.sensitivity_requests += 1
                group.sensitivity_tokens += hypothetical
        else:
            group.known_cost += cost
        duration = row.get("http_ms")
        if (
            type(duration) in (int, float)
            and isinstance(duration, int | float)
            and math.isfinite(duration)
            and duration >= 0
        ):
            group.http_ms += Decimal(str(duration))
            group.clock_observations += 1

    known = sum((group.known_cost for group in groups.values()), ZERO)
    unknown = sum(group.unknown_cost_requests for group in groups.values())
    sensitivity = sum((group.sensitivity for group in groups.values()), ZERO)
    covered = sum(group.sensitivity_requests for group in groups.values())
    caps_raw = summary.get("caps")
    caps = _object(caps_raw) if isinstance(caps_raw, dict) else {}
    caps_valid = set(caps) == set(CAPS) and all(
        type(value) is int and isinstance(value, int) and 0 <= value <= CAPS[kind]
        for kind, value in caps.items()
    )
    within_configured = caps_valid and all(counts[kind] <= cast(int, caps[kind]) for kind in CAPS)
    total = None if unknown else _amount(known)
    checks = {
        "sequence_complete": all(type(number) is int for number in sequence)
        and sequence == list(range(1, len(sequence) + 1)),
        "within_authorized_caps": all(counts[kind] <= CAPS[kind] for kind in CAPS),
        "configured_caps_are_authorized": caps_valid,
        "within_configured_caps": within_configured,
        "summary_matches_attempts": summary.get("attempts") == counts,
        "summary_matches_reported_usage": summary.get("reported_usage")
        == {kind: dict(tokens) for kind, tokens in reported_usage.items()},
        "summary_matches_independent_cost": (
            summary.get("known_public_list_price_cny") == _amount(known)
            and summary.get("unknown_cost_requests") == unknown
            and "public_list_price_cny" in summary
            and summary["public_list_price_cny"] == total
        ),
    }
    return {
        "audit_passed": all(checks.values()),
        "checks": checks,
        "record_count": len(sequence),
        "attempts": counts,
        "authorized_caps": CAPS,
        "configured_caps": caps,
        "public_list_price_cny": total,
        "known_public_list_price_cny": _amount(known),
        "unknown_cost_requests": unknown,
        "non_billing_sensitivity": {
            "assumption": "Conflicting rerank prompt_tokens=total_tokens are the billable input.",
            "rerank_prompt_total_addition_cny": _amount(sensitivity),
            "known_plus_sensitivity_subtotal_cny": _amount(known + sensitivity),
            "covered_unknown_requests": covered,
            "uncovered_unknown_requests": unknown - covered,
            "is_invoice_or_resolved_total": False,
        },
        "phase_kind": [
            group.as_dict()
            for _, group in sorted(
                groups.items(), key=lambda item: (PHASES.index(item[0][0]), item[0][1])
            )
        ],
        "reported_usage": {kind: dict(tokens) for kind, tokens in reported_usage.items()},
        "models": models or {},
        "prices_cny_per_million_tokens": prices,
        "price_scope": "Caller-configured rates; missing prices are unknown.",
        "clock_scope": "Sum of observed HTTP stage durations, including client instrumentation.",
    }


def render_markdown(audit: Mapping[str, object], directory: Path) -> str:
    sensitivity = _object(audit["non_billing_sensitivity"])
    checks = _object(audit["checks"])
    counts, caps = _object(audit["attempts"]), _object(audit["authorized_caps"])
    groups = [_object(group) for group in cast(list[object], audit["phase_kind"])]
    exact = (
        "未知（null）"
        if audit["public_list_price_cny"] is None
        else str(audit["public_list_price_cny"])
    )
    lines = [
        "# 调用量与费用独立核对",
        "",
        "**合成数据，不代表质量验收。**",
        "",
        (
            f"生成时间（UTC）：{audit['generated_at_utc']}。"
            + f"记账一致性：{'通过' if audit['audit_passed'] else '失败'}。"
            + "费用结论："
            + ("未知。" if audit["public_list_price_cny"] is None else "公开价估算，非账单。")
        ),
        (
            f"按公开目录价折算的完整总额：**{exact}**；可计算部分：**¥{audit['known_public_list_price_cny']}**；"
            + f"未知费用请求：**{audit['unknown_cost_requests']}**。"
        ),
        "采用运行汇总中的显式单价；未配置单价的请求费用未知。不是代理账单。",
        "",
        "| 类型 | 实际 HTTP 尝试 | 授权上限 |",
        "|---|---:|---:|",
        *[f"| {kind} | {counts[kind]} | {caps[kind]} |" for kind in CAPS],
        "",
        "## 未解决的 rerank usage 矛盾",
        "",
        (
            "`prompt_tokens=total_tokens>0` 与 `input_tokens=output_tokens=completion_tokens=0`"
            + " 互相矛盾；保留未知状态，不把零值或非零值静默认定为真实账单输入。"
        ),
        "",
        (
            "非账单敏感性情景：**假设**该矛盾中的 prompt/total 是计费输入，"
            "按显式配置的单价折算，"
            + f"可另外计得 ¥{sensitivity['rerank_prompt_total_addition_cny']}；与已知部分相加的"
            + f"**情景小计为 ¥{sensitivity['known_plus_sensitivity_subtotal_cny']}**。"
            + f"该情景覆盖 {sensitivity['covered_unknown_requests']} 个未知请求，"
            + f"仍未覆盖 {sensitivity['uncovered_unknown_requests']} 个。"
            + "它不替代未知总额，也不构成账单。"
        ),
        "",
        "## 按阶段与类型拆分",
        "",
        (
            "| 阶段 | 类型 | 尝试 | 完整响应 | 已知费用 CNY | 未知请求 |"
            + " 情景追加 CNY | HTTP 阶段累计 ms |"
        ),
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for group in groups:
        lines.append(
            f"| {group['phase']} | {group['kind']} | {group['attempts']} |"
            + f" {group['complete_responses']} | {group['known_public_list_price_cny']} |"
            + f" {group['unknown_cost_requests']} | {group['sensitivity_addition_cny']} |"
            + f" {float(cast(float, group['http_stage_ms_sum'])):.2f} |"
        )
    lines.extend(
        [
            "",
            (
                "HTTP 时间是已记录请求阶段耗时之和，包含客户端记账开销；不是整体运行墙钟时间，"
                + "也不是纯服务端推理时延。未记录耗时的请求不会虚构零时延。"
            ),
            "",
            "以下分别累加 provider 白名单字段；别名列保留原值，**不可跨列相加**。",
            "",
            (
                "| 阶段 | 类型 | prompt_tokens | input_tokens | completion_tokens |"
                + " output_tokens | total_tokens |"
            ),
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for group in groups:
        tokens = _object(group["usage"])
        lines.append(
            f"| {group['phase']} | {group['kind']} | "
            + " | ".join(str(tokens.get(key, "未上报")) for key in TOKEN_KEYS)
            + " |"
        )
    lines.extend(
        [
            "",
            "## 核对与来源",
            "",
            *[f"- `{key}`：{'通过' if value else '失败'}。" for key, value in checks.items()],
            "",
            (
                "独立重算未导入 Ledger 实现；核对序号连续且唯一、请求计数、授权/配置上限、"
                + "各 token 字段及可计算费用。JSON 保存输入文件 SHA-256、固定状态与数值，"
                + "不复制 provider 内容。"
            ),
            "",
            (
                "[原始记账](usage-ledger.jsonl) · "
                + "[运行汇总](usage-cost.json) · "
                + "[独立核对 JSON](usage-audit.json)"
            ),
            "",
        ]
    )
    models = _object(audit["models"])
    prices = _object(audit["prices_cny_per_million_tokens"])
    lines += [
        "单价由调用方按所选模型服务的公开价目提供，不扣优惠或免费额度。",
        "",
        "| 类型 | 模型 | 输入元/百万 token | 输出元/百万 token |",
        "|---|---|---:|---:|",
    ]
    for kind in CAPS:
        rates = prices.get(kind)
        cells = f"{rates[0]} | {rates[1]}" if isinstance(rates, list | tuple) else "未知 | 未知"
        lines.append(f"| {kind} | {models.get(kind, '未记录')} | {cells} |")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parent)
    arguments = parser.parse_args(argv)
    directory = cast(Path, arguments.directory)
    ledger_path, summary_path = directory / "usage-ledger.jsonl", directory / "usage-cost.json"
    ledger_bytes, summary_bytes = ledger_path.read_bytes(), summary_path.read_bytes()
    rows: list[object] = [json.loads(line) for line in ledger_bytes.splitlines() if line.strip()]
    decoded: object = json.loads(summary_bytes)
    audit = build_audit(rows, _object(decoded))
    audit["generated_at_utc"] = datetime.now(UTC).isoformat()
    audit["source_sha256"] = {
        "usage-ledger.jsonl": hashlib.sha256(ledger_bytes).hexdigest(),
        "usage-cost.json": hashlib.sha256(summary_bytes).hexdigest(),
    }
    encoded_json = json.dumps(audit, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    encoded_markdown = render_markdown(audit, directory)
    (directory / "usage-audit.json").write_text(encoded_json, encoding="utf-8")
    (directory / "usage-cost.md").write_text(encoded_markdown, encoding="utf-8")
    print(json.dumps({"audit_passed": audit["audit_passed"], "records": audit["record_count"]}))
    return 0 if audit["audit_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

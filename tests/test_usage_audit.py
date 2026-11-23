"""The final cost audit must preserve contradictions rather than invent a bill."""

import copy
import runpy
from pathlib import Path

AUDIT = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "reports/tuning/summarize_usage.py")
)


def fixture():
    rows = [
        {
            "sequence": 1,
            "phase": "dev",
            "kind": "embedding",
            "complete_response": True,
            "usage_status": "reported",
            "usage": {"input_tokens": 100, "total_tokens": 100},
            "http_ms": 12.5,
        },
        {
            "sequence": 2,
            "phase": "dev",
            "kind": "rerank",
            "complete_response": True,
            "usage_status": "inconsistent",
            "usage": {
                "prompt_tokens": 1000,
                "input_tokens": 0,
                "completion_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 1000,
            },
            "http_ms": 200.0,
        },
        {
            "sequence": 3,
            "phase": "test-knee",
            "kind": "chat",
            "complete_response": True,
            "usage_status": "reported",
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            "http_ms": 1000.0,
        },
    ]
    summary = {
        "caps": {"embedding": 1500, "rerank": 1000, "chat": 500},
        "attempts": {"embedding": 1, "rerank": 1, "chat": 1},
        "reported_usage": {row["kind"]: row["usage"] for row in rows},
        "public_list_price_cny": None,
        "known_public_list_price_cny": 0.000062,
        "unknown_cost_requests": 1,
    }
    return rows, summary


def test_alias_conflict_stays_unknown_and_sensitivity_is_explicitly_separate():
    rows, summary = fixture()
    # Detect the contradiction independently, even if the row's status is wrong.
    rows[1]["usage_status"] = "reported"
    rows[1]["private"] = "never copy provider bodies"
    audit = AUDIT["build_audit"](rows, summary)
    assert audit["audit_passed"] is True
    assert audit["public_list_price_cny"] is None
    assert audit["known_public_list_price_cny"] == 0.000062
    assert audit["unknown_cost_requests"] == 1
    sensitivity = audit["non_billing_sensitivity"]
    assert sensitivity["rerank_prompt_total_addition_cny"] == 0.0005
    assert sensitivity["known_plus_sensitivity_subtotal_cny"] == 0.000562
    assert sensitivity["covered_unknown_requests"] == 1
    assert sensitivity["uncovered_unknown_requests"] == 0
    group = next(group for group in audit["phase_kind"] if group["kind"] == "rerank")
    assert group["usage"]["prompt_tokens"] == 1000
    assert group["usage"]["input_tokens"] == 0
    assert group["unknown_reasons"] == {"aliases_disagree": 1}
    assert "never copy provider bodies" not in str(audit)


def test_sequence_gap_and_cap_breach_are_audit_failures():
    rows, summary = fixture()
    rows[2]["sequence"] = 4
    summary["caps"]["embedding"] = 0
    audit = AUDIT["build_audit"](rows, summary)
    assert audit["audit_passed"] is False
    assert audit["checks"]["sequence_complete"] is False
    assert audit["checks"]["within_configured_caps"] is False


def test_reported_numeric_total_cannot_replace_unknown_total():
    rows, summary = fixture()
    summary["public_list_price_cny"] = summary["known_public_list_price_cny"]
    audit = AUDIT["build_audit"](rows, summary)
    assert audit["audit_passed"] is False
    assert audit["checks"]["summary_matches_independent_cost"] is False
    assert audit["public_list_price_cny"] is None


def test_uncovered_incomplete_request_is_excluded_from_sensitivity_subtotal():
    rows, summary = fixture()
    rows[2]["complete_response"] = False
    summary = copy.deepcopy(summary)
    summary["known_public_list_price_cny"] = 0.00005
    summary["unknown_cost_requests"] = 2
    audit = AUDIT["build_audit"](rows, summary)
    assert audit["audit_passed"] is True
    assert audit["non_billing_sensitivity"]["uncovered_unknown_requests"] == 1
    assert audit["non_billing_sensitivity"]["known_plus_sensitivity_subtotal_cny"] == 0.00055

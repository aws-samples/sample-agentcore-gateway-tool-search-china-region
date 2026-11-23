"""Report denominators and failure attribution must not overstate evidence."""

from __future__ import annotations

import runpy
from dataclasses import asdict
from pathlib import Path
from typing import Any

from semantic_eval.dataset import EvalQuery
from semantic_eval.metrics import Observation

ROOT = Path(__file__).parents[1]
REPORT = runpy.run_path(str(ROOT / "reports/tuning/summarize_tuning.py"))
AUDIT = runpy.run_path(str(ROOT / "reports/tuning/summarize_usage.py"))


def _query(key: str, allowed: tuple[str, ...], relevant: tuple[str, ...]) -> EvalQuery:
    return EvalQuery(key, " Same TEXT ", "en", "gateway", allowed, relevant, "restricted")


def _run(queries: tuple[EvalQuery, ...]) -> dict[str, Any]:
    return {
        "name": "fixture",
        "observations": [
            asdict(
                Observation(
                    query.id,
                    query.language,
                    query.permission_group,
                    query.relevant_ids,
                    query.allowed_ids,
                    ("allowed",) if query.allowed_ids else (),
                    1.0,
                    "invalid_response" if query.id == "error" else None,
                )
            )
            for query in queries
        ],
    }


def test_empty_denominators_separate_acl_outside_catalog_and_permission_negatives() -> None:
    queries = (
        _query("acl", (), ("hidden",)),
        _query("outside", ("allowed",), ()),
        _query("permission", ("allowed",), ("hidden",)),
        _query("error", ("allowed",), ("hidden",)),
        _query("positive", ("allowed",), ("allowed",)),
    )
    result = REPORT["empty_breakdown"](_run(queries), queries)
    assert result["empty_acl"]["n"] == 1
    assert result["all_empty"]["false_recommendations"] == 2
    assert result["all_empty"]["n"] == 4
    assert result["nonempty_acl_empty"]["false_rate"] == 2 / 3
    assert result["outside_catalog"]["n"] == 1
    assert result["permission_negative"]["n"] == 2
    assert result["permission_negative"]["errors"] == 1
    assert result["permission_negative"]["correct_empty"] == 0
    assert result["permission_negative"]["unique_texts"] == 1


def test_knee_failure_diagnosis_does_not_relabel_retrieval_misses_as_model_failures() -> None:
    rows = [
        {
            "correct": False,
            "expected_id": "truth",
            "candidate_ids": ["other"],
            "error": None,
            "language": "zh",
            "usage": {},
        },
        {
            "correct": False,
            "expected_id": "truth",
            "candidate_ids": ["truth", "other"],
            "error": None,
            "language": "en",
            "usage": {},
        },
        {
            "correct": False,
            "expected_id": "truth",
            "candidate_ids": ["truth"],
            "error": "invalid_response",
            "language": "zh",
            "usage": {"completion_tokens": 256},
        },
        {
            "correct": False,
            "expected_id": "truth",
            "candidate_ids": ["truth"],
            "error": "timeout",
            "language": "en",
            "usage": {},
        },
    ]
    result = REPORT["knee_failure_breakdown"](rows)
    assert result["retrieval_misses"] == 1
    assert result["retrieval_misses_by_language"] == {"zh": 1}
    assert result["selection_misses"] == 1
    assert result["errors"] == {"invalid_response": 1, "timeout": 1}
    # Retain observed counts, never infer truncation without finish_reason.
    assert result["invalid_response_completion_tokens"] == [256]
    assert "truncation" not in result


def test_future_ungated_arm_is_compared_without_manufacturing_historical_measurements() -> None:
    queries = (_query("positive", ("allowed",), ("allowed",)),)
    indexed = {
        name: {**_run(queries), "name": name}
        for name in ("current_rrf", "recommended", "recommended_rerank", "vector_ungated")
    }
    comparisons = REPORT["test_comparisons"](indexed, queries)
    assert any(
        value["baseline"] == "vector_ungated" and value["candidate"] == "recommended"
        for value in comparisons
    )
    del indexed["vector_ungated"]
    assert len(REPORT["test_comparisons"](indexed, queries)) == 2


def test_cost_success_headline_distinguishes_accounting_from_unknown_invoice() -> None:
    audit = AUDIT["build_audit"](
        [],
        {
            "caps": {"embedding": 1500, "rerank": 1000, "chat": 500},
            "attempts": {"embedding": 0, "rerank": 0, "chat": 0},
            "reported_usage": {},
            "public_list_price_cny": 0,
            "known_public_list_price_cny": 0,
            "unknown_cost_requests": 0,
        },
    )
    audit["generated_at_utc"] = "offline fixture"
    audit["public_list_price_cny"] = None
    audit["audit_passed"] = True
    audit["source_sha256"] = {}
    rendered = AUDIT["render_markdown"](audit, ROOT / "reports/tuning")
    assert "记账一致性：通过" in rendered
    assert "费用结论：未知" in rendered

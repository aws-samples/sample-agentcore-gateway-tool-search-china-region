"""Local collection and joining never invent a backend start or embedding budget."""

import importlib.util
import json
import subprocess  # nosec B404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/analyze_search_routes.py"


def module():
    spec = importlib.util.spec_from_file_location("search_route_analysis", SCRIPT)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def fixtures():
    first, second = "a" * 32, "b" * 32
    route_id, worker_id = "facade-id", "actual-platform-worker-id"
    records = [
        {
            "operation": "search_route",
            "phase": "end",
            "aws_request_id": route_id,
            "correlation_id": "c" * 64,
            "ecs_attempt_id": first,
            "attempt_id": second,
            "backend": "fallback",
            "fallback_reason": "capacity",
            "fallback_started": True,
        },
        {
            "operation": "search_attempt",
            "attempt_id": first,
            "backend": "ecs",
            "phase": "start",
            "timestamp_ms": 10,
            "remaining_ms": 4000,
            "aws_request_id": route_id,
        },
        {
            "operation": "search_attempt",
            "attempt_id": first,
            "backend": "ecs",
            "phase": "end",
            "timestamp_ms": 11,
            "status": "capacity",
            "aws_request_id": route_id,
        },
        {
            "operation": "search_ingress",
            "attempt_id": first,
            "phase": "end",
            "timestamp_ms": 10,
            "status": "capacity",
        },
        {
            "operation": "search_attempt",
            "attempt_id": second,
            "backend": "fallback",
            "phase": "start",
            "timestamp_ms": 12,
            "remaining_ms": 8998,
            "aws_request_id": route_id,
        },
        {
            "operation": "search_backend",
            "attempt_id": second,
            "backend": "fallback",
            "phase": "start",
            "timestamp_ms": 13,
            "aws_request_id": worker_id,
        },
        {
            "operation": "search",
            "aws_request_id": worker_id,
            "vector_availability": "available",
            "degraded": False,
            "embedding_timing": {
                "search_remaining_ms": 3000,
                "stage_budget_ms": 1000,
                "configured_timeout_ms": 1000,
                "attempts": [{"effective_timeout_ms": 780, "total_ms": 120}],
            },
        },
        {
            "operation": "search_backend",
            "attempt_id": second,
            "backend": "fallback",
            "phase": "end",
            "timestamp_ms": 14,
            "status": "ok",
            "aws_request_id": worker_id,
        },
        {
            "operation": "search_fallback",
            "attempt_id": second,
            "backend": "fallback",
            "phase": "end",
            "timestamp_ms": 15,
            "status": "ok",
            "aws_request_id": worker_id,
            "cold_start": True,
        },
        {
            "operation": "search_attempt",
            "attempt_id": second,
            "backend": "fallback",
            "phase": "end",
            "timestamp_ms": 16,
            "status": "ok",
            "aws_request_id": route_id,
        },
    ]
    return records


def test_analysis_joins_real_lambda_id_and_preserves_individual_embedding_budget():
    result = module().analyze(fixtures())
    route = result["routes"][0]
    primary, secondary = route["attempts"]
    assert primary["backend_start"] == "not_started"
    assert primary["no_start_reason"] == "capacity"
    assert secondary["backend_start"] == "observed"
    assert secondary["platform_request_id"] == "actual-platform-worker-id"
    assert secondary["embedding_timing"]["attempts"][0]["effective_timeout_ms"] == 780
    assert secondary["cold_start"] is True
    assert not result["anomalies"]


def test_quality_flags_survive_route_export_without_inferred_defaults():
    rows = fixtures()
    result = module().analyze(rows)
    secondary = result["routes"][0]["attempts"][1]
    assert secondary["vector_availability"] == "available"
    assert secondary["degraded"] is False
    search = next(row for row in rows if row["operation"] == "search")
    search.pop("degraded")
    assert module().analyze(rows)["routes"][0]["attempts"][1]["degraded"] is None


def test_missing_start_is_unknown_for_response_success_and_is_not_erased():
    rows = fixtures()
    rows = [
        row
        for row in rows
        if not (row["operation"] == "search_backend" and row.get("phase") == "start")
    ]
    result = module().analyze(rows)
    assert result["routes"][0]["attempts"][1]["backend_start"] == "missing"
    assert any(item["kind"] == "missing_backend_start" for item in result["anomalies"])


def test_capacity_without_ingress_id_still_records_unstarted_work():
    rows = [row for row in fixtures() if row["operation"] != "search_ingress"]
    result = module().analyze(rows)
    primary = result["routes"][0]["attempts"][0]
    assert primary["backend_start"] == "not_started"
    assert primary["no_start_reason"] == "capacity"


def test_rejections_before_decode_remain_visible_without_an_attempt_id():
    rejected = [
        {
            "operation": "search_ingress",
            "attempt_id": None,
            "status": status,
            "execution_assigned": False,
            "http_status": code,
            "_source_file": "ecs.jsonl",
            "_source_line": index,
        }
        for index, (status, code) in enumerate(
            [
                ("invalid input", 400),
                ("payload too large", 413),
                ("not found", 404),
                ("deadline exceeded", 408),
            ],
            1,
        )
    ]
    result = module().analyze(fixtures() + rejected)
    assert result["unmatched_ingress_records"] == rejected
    assert len(result["routes"]) == 1
    assert len(result["routes"][0]["attempts"]) == 2
    assert not result["unmatched_search_records"]
    assert sum(item["kind"] == "unmatched_ingress_record" for item in result["anomalies"]) == 4


def test_orphan_ingress_keeps_original_record_without_inventing_a_route():
    rejected = {
        "operation": "search_ingress",
        "attempt_id": "unobserved-attempt",
        "status": "capacity",
        "execution_assigned": False,
    }
    result = module().analyze([rejected])
    assert result["unmatched_ingress_records"] == [rejected]
    assert result["routes"] == []
    assert result["anomalies"] == [
        {
            "kind": "orphan_attempt",
            "attempt_id": "unobserved-attempt",
            "aws_request_id": None,
        },
        {
            "kind": "unmatched_ingress_record",
            "attempt_id": "unobserved-attempt",
            "aws_request_id": None,
        },
    ]


def test_transport_timeout_does_not_claim_the_remote_work_never_started():
    rows = fixtures()
    rows = [row for row in rows if row["operation"] != "search_ingress"]
    for row in rows:
        if row.get("status") == "capacity":
            row["status"] = "read_timeout"
    result = module().analyze(rows)
    primary = result["routes"][0]["attempts"][0]
    assert primary["backend_start"] == "unknown"
    assert primary["no_start_reason"] == "read_timeout"


def test_oauth_deadline_has_no_remote_start_and_is_not_a_credential_failure():
    rows = [row for row in fixtures() if row["operation"] != "search_ingress"]
    for row in rows:
        if row.get("status") == "capacity":
            row["status"] = "deadline_exceeded"
    rows.append(
        {
            "operation": "search_oauth",
            "phase": "end",
            "attempt_id": "a" * 32,
            "status": "deadline_exceeded",
        }
    )
    result = module().analyze(rows)
    primary = result["routes"][0]["attempts"][0]
    assert primary["backend_start"] == "not_started"
    assert primary["no_start_reason"] == "deadline_exceeded"
    assert not result["anomalies"]


def test_oauth_outcomes_prove_no_ecs_execution_without_hiding_the_failure_kind():
    for status in (
        "oauth_transport_failed",
        "oauth_refresh_unavailable",
        "oauth_secret_unavailable",
        "oauth_rejected",
        "oauth_tls_failed",
        "oauth_server_failed",
        "oauth_failed",
    ):
        rows = [row for row in fixtures() if row["operation"] != "search_ingress"]
        for row in rows:
            if row.get("status") == "capacity":
                row["status"] = status
        rows.append(
            {
                "operation": "search_oauth",
                "phase": "end",
                "attempt_id": "a" * 32,
                "status": status,
            }
        )
        result = module().analyze(rows)
        primary = result["routes"][0]["attempts"][0]
        assert primary["backend_start"] == "not_started", status
        assert primary["no_start_reason"] == status
        assert not result["anomalies"]


def test_embedding_budget_violation_is_flagged_without_dropping_the_attempt():
    rows = fixtures()
    search = next(row for row in rows if row["operation"] == "search")
    search["embedding_timing"]["attempts"][0]["effective_timeout_ms"] = 1002
    result = module().analyze(rows)
    assert any(item["kind"] == "embedding_budget_exceeded" for item in result["anomalies"])
    assert (
        result["routes"][0]["attempts"][1]["embedding_timing"]["attempts"][0][
            "effective_timeout_ms"
        ]
        == 1002
    )


def test_duplicate_summary_and_missing_attempt_end_are_reported():
    rows = fixtures()
    rows.append(rows[0].copy())
    rows.pop(-2)
    kinds = {item["kind"] for item in module().analyze(rows)["anomalies"]}
    assert {"duplicate_route_finish", "missing_attempt_end"} <= kinds


def test_cli_collects_export_wrappers_retains_invalid_line_count_and_writes_report(tmp_path):
    source = tmp_path / "input.jsonl"
    source.write_text(
        "\n".join(json.dumps({"message": json.dumps(row)}) for row in fixtures())
        + "\nnot valid JSON\n"
    )
    output = tmp_path / "report.json"
    collected = tmp_path / "collected.jsonl"
    # Fixed local test program and fixture paths; argv is not request-controlled.
    # nosemgrep: dangerous-subprocess-use-audit
    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            str(SCRIPT),
            "--input",
            str(source),
            "--output",
            str(output),
            "--collected",
            str(collected),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["collection"]["invalid_lines"] == 1
    assert len(collected.read_text().splitlines()) == len(fixtures())
    assert len(report["routes"]) == 1


def test_started_work_without_search_or_backend_end_is_an_integrity_failure():
    rows = [
        row
        for row in fixtures()
        if row["operation"] != "search"
        and not (row["operation"] == "search_backend" and row.get("phase") == "end")
    ]
    result = module().analyze(rows)
    kinds = {item["kind"] for item in result["anomalies"]}
    assert {"missing_search_record", "missing_backend_end"} <= kinds
    secondary = result["routes"][0]["attempts"][1]
    assert secondary["search_record"] == "missing"
    assert secondary["embedding_observation"] == "missing"


def test_available_embedding_without_timing_is_an_integrity_failure():
    rows = fixtures()
    search = next(row for row in rows if row["operation"] == "search")
    search["embedding_timing"] = None
    result = module().analyze(rows)
    assert any(item["kind"] == "missing_embedding_timing" for item in result["anomalies"])


def test_timed_out_started_work_has_explicit_missing_observations():
    rows = [
        row
        for row in fixtures()
        if row["operation"] not in ("search", "search_fallback")
        and not (row["operation"] == "search_backend" and row.get("phase") == "end")
    ]
    for row in rows:
        if (
            row["operation"] == "search_attempt"
            and row.get("backend") == "fallback"
            and row.get("phase") == "end"
        ):
            row["status"] = "deadline_exceeded"
    result = module().analyze(rows)
    secondary = result["routes"][0]["attempts"][1]
    assert secondary["search_record"] == "missing"
    assert secondary["completion"] == "unknown"
    assert any(item["kind"] == "missing_search_record" for item in result["anomalies"])


def test_direct_lambda_and_unmatched_clients_are_preserved_for_explicit_control_join():
    rows = [
        {"operation": "search", "aws_request_id": "old-lambda-id", "embedding_timing": None},
        {"operation": "search_client", "correlation_id": "d" * 64, "case_id": "sample"},
    ]
    result = module().analyze(rows)
    assert result["unmatched_search_records"] == rows[:1]
    assert result["unmatched_clients"] == rows[1:]
    kinds = {item["kind"] for item in result["anomalies"]}
    assert {"unmatched_search_record", "unmatched_client"} <= kinds


def test_unassociated_backend_rejections_survive_without_the_optional_collected_file():
    rows = [
        {
            "operation": "search_fallback",
            "phase": "end",
            "status": "unauthenticated",
            "attempt_id": None,
            "aws_request_id": "real-platform-id",
            "failure_stage": "admission",
        },
        {
            "operation": "search_backend",
            "phase": "end",
            "status": "invalid input",
            "attempt_id": "orphan-attempt",
            "search_started": False,
        },
    ]
    result = module().analyze(rows)
    assert result["routes"] == []
    assert result["unmatched_backend_records"] == rows
    assert sum(row["kind"] == "unmatched_backend_record" for row in result["anomalies"]) == 2
    assert result["routes"] == []


def test_collection_rejects_duplicate_keys_and_nonfinite_numbers(tmp_path):
    source = tmp_path / "input.jsonl"
    source.write_text('{"operation":"search","operation":"search_route"}\n{"duration_ms":NaN}\n')
    rows, counts = module().collect([source])
    assert rows == []
    assert counts["invalid_lines"] == 2


def test_stage_budget_and_second_http_attempt_cannot_reset_deadline():
    rows = fixtures()
    timing = next(row for row in rows if row["operation"] == "search")["embedding_timing"]
    timing["stage_budget_ms"] = 1500
    report = module().analyze(rows)
    assert any(row["kind"] == "embedding_stage_budget_exceeded" for row in report["anomalies"])
    timing["stage_budget_ms"] = 1000
    timing["attempts"] = [
        {"effective_timeout_ms": 1000, "total_ms": 700},
        {"effective_timeout_ms": 800, "total_ms": 100},
    ]
    report = module().analyze(rows)
    assert any(row["kind"] == "embedding_attempt_budget_reset" for row in report["anomalies"])


def test_incomplete_budget_fields_and_diagnostic_flags_are_explicit():
    for broken in [
        {},
        {"search_remaining_ms": 3000, "stage_budget_ms": 1000},
        {
            "search_remaining_ms": 3000,
            "stage_budget_ms": 1000,
            "configured_timeout_ms": 1000,
            "attempts": [],
            "diagnostics_failed": True,
        },
        {
            "search_remaining_ms": 3000,
            "stage_budget_ms": 1000,
            "configured_timeout_ms": 1000,
            "attempts": [],
            "attempts_truncated": True,
        },
    ]:
        rows = fixtures()
        next(row for row in rows if row["operation"] == "search")["embedding_timing"] = broken
        result = module().analyze(rows)
        assert result["routes"][0]["attempts"][1]["embedding_observation"] == "incomplete"
        assert any(row["kind"] == "incomplete_embedding_timing" for row in result["anomalies"])


def test_huge_finite_json_integer_does_not_crash_budget_analysis():
    rows = fixtures()
    timing = next(row for row in rows if row["operation"] == "search")["embedding_timing"]
    timing["stage_budget_ms"] = 10**1000
    result = module().analyze(rows)
    assert any(row["kind"] == "incomplete_embedding_timing" for row in result["anomalies"])


def test_explicit_pre_execution_and_pre_search_rejections_are_not_missing_logs():
    for status, marker in [("unauthenticated", "ingress"), ("replay unavailable", "backend")]:
        rows = fixtures()
        for row in rows:
            if row.get("status") == "capacity":
                row["status"] = status
        if marker == "ingress":
            next(row for row in rows if row["operation"] == "search_ingress")[
                "execution_assigned"
            ] = False
        else:
            rows = [row for row in rows if row["operation"] != "search_ingress"]
            rows.append(
                {
                    "operation": "search_backend",
                    "phase": "end",
                    "status": status,
                    "attempt_id": "a" * 32,
                    "search_started": False,
                }
            )
        result = module().analyze(rows)
        primary = result["routes"][0]["attempts"][0]
        assert primary["backend_start"] == "not_started"
        assert primary["no_start_reason"] == status
        assert not result["anomalies"]


def test_fallback_pre_admission_log_joins_by_actual_platform_id():
    rows = fixtures()
    rows = [
        row
        for row in rows
        if row["operation"] != "search"
        and not (row["operation"] == "search_backend" and row.get("phase") == "start")
    ]
    for row in rows:
        if row["operation"] == "search_backend":
            row["attempt_id"] = None
            row["search_started"] = False
            row["status"] = "unauthenticated"
        if (
            row["operation"] in ("search_fallback", "search_attempt")
            and row.get("backend") == "fallback"
        ):
            row["status"] = "unauthenticated"
    result = module().analyze(rows)
    secondary = result["routes"][0]["attempts"][1]
    assert secondary["backend_start"] == "not_started"
    assert secondary["no_start_reason"] == "unauthenticated"
    assert not result["anomalies"]

#!/usr/bin/env python3
"""Collect already-exported JSONL and join route attempts without AWS access.

Pass one --input per client/facade/ECS/fallback export for a single measurement
run and region. CloudWatch {"message": "<JSON>"} wrappers are accepted. The
optional --collected file preserves normalized records, including unmatched
records. Invalid lines are counted rather than silently disappearing.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import cast

Record = dict[str, object]


def _unique(pairs: list[tuple[str, object]]) -> Record:
    result: Record = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value: str) -> object:
    raise ValueError("nonfinite JSON number")


def _decode(value: str) -> object:
    return json.loads(value, object_pairs_hook=_unique, parse_constant=_invalid_constant)


def collect(paths: Iterable[Path]) -> tuple[list[Record], Record]:
    records: list[Record] = []
    invalid = 0
    for path in paths:
        with path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    value = _decode(line)
                    if isinstance(value, dict) and isinstance(value.get("message"), str):
                        value = _decode(value["message"])
                    if not isinstance(value, dict):
                        raise ValueError("object required")
                    record = cast(Record, value)
                    record["_source_line"] = line_number
                    record["_source_file"] = path.name
                    records.append(record)
                except (ValueError, TypeError):
                    invalid += 1
    return records, {"records": len(records), "invalid_lines": invalid}


def _number(value: object) -> float | None:
    try:
        if type(value) in (int, float) and math.isfinite(cast(float, value)):
            return float(cast(float, value))
    except OverflowError:
        pass
    return None


def _events(rows: list[Record], operation: str, phase: str | None = None) -> list[Record]:
    return [
        row
        for row in rows
        if row.get("operation") == operation and (phase is None or row.get("phase") == phase)
    ]


def analyze(records: list[Record]) -> Record:
    by_attempt: dict[str, list[Record]] = defaultdict(list)
    searches: dict[str, list[Record]] = defaultdict(list)
    clients: dict[str, list[Record]] = defaultdict(list)
    routes: list[Record] = []
    anomalies: list[Record] = []
    seen_routes: set[str] = set()
    linked_attempts: set[str] = set()
    linked_searches: set[int] = set()
    linked_clients: set[int] = set()
    linked_ingress: set[int] = set()
    linked_backends: set[int] = set()
    for row in records:
        attempt = row.get("attempt_id")
        if isinstance(attempt, str):
            by_attempt[attempt].append(row)
        request_id = row.get("aws_request_id")
        if row.get("operation") == "search" and isinstance(request_id, str):
            searches[request_id].append(row)
        correlation = row.get("correlation_id")
        if row.get("operation") == "search_client" and isinstance(correlation, str):
            clients[correlation].append(row)

    fallback_links = {
        row["aws_request_id"]: row["attempt_id"]
        for row in _events(records, "search_fallback", "end")
        if isinstance(row.get("aws_request_id"), str) and isinstance(row.get("attempt_id"), str)
    }
    for row in _events(records, "search_backend"):
        request_id = row.get("aws_request_id")
        if row.get("attempt_id") is None and isinstance(request_id, str):
            linked = fallback_links.get(request_id)
            if isinstance(linked, str):
                by_attempt[linked].append(row)

    def anomaly(kind: str, attempt_id: object = None, request_id: object = None) -> None:
        anomalies.append({"kind": kind, "attempt_id": attempt_id, "aws_request_id": request_id})

    for summary in _events(records, "search_route", "end"):
        route_id = summary.get("aws_request_id")
        if isinstance(route_id, str):
            if route_id in seen_routes:
                anomaly("duplicate_route_finish", request_id=route_id)
                continue
            seen_routes.add(route_id)
        attempts: list[Record] = []
        attempt_ids = dict.fromkeys(
            item
            for item in (summary.get("ecs_attempt_id"), summary.get("attempt_id"))
            if isinstance(item, str)
        )
        for attempt_id in attempt_ids:
            linked_attempts.add(attempt_id)
            rows = by_attempt[attempt_id]
            starts = _events(rows, "search_attempt", "start")
            ends = _events(rows, "search_attempt", "end")
            backend_starts = _events(rows, "search_backend", "start")
            backend_ends = _events(rows, "search_backend", "end")
            ingress = _events(rows, "search_ingress")
            linked_ingress.update(id(row) for row in ingress)
            oauth = _events(rows, "search_oauth", "end")
            fallback = _events(rows, "search_fallback", "end")
            linked_backends.update(id(row) for row in backend_starts + backend_ends + fallback)
            for label, boundary in (("start", starts), ("end", ends)):
                if not boundary:
                    anomaly(f"missing_attempt_{label}", attempt_id, route_id)
                elif len(boundary) != 1:
                    anomaly(f"duplicate_attempt_{label}", attempt_id, route_id)
            reason = ends[-1].get("status") if ends else "unknown"
            state = "observed"
            no_start_reason: object = None
            if not backend_starts:
                rejected_ingress = [
                    row for row in ingress if row.get("execution_assigned") is False
                ]
                rejected_backend = [
                    row for row in backend_ends if row.get("search_started") is False
                ]
                rejected_fallback = [
                    row for row in fallback if row.get("failure_stage") in {"admission", "factory"}
                ]
                if rejected_ingress or rejected_backend or rejected_fallback:
                    rejection = (rejected_ingress or rejected_backend or rejected_fallback)[-1]
                    state, no_start_reason = "not_started", rejection.get("status")
                elif reason == "capacity" or any(
                    row.get("status") == "capacity" for row in ingress
                ):
                    state, no_start_reason = "not_started", "capacity"
                elif oauth and oauth[-1].get("status") in {
                    "oauth_failed",
                    "oauth_transport_failed",
                    "oauth_refresh_unavailable",
                    "oauth_secret_unavailable",
                    "oauth_rejected",
                    "oauth_tls_failed",
                    "oauth_server_failed",
                    "deadline_exceeded",
                }:
                    # Token acquisition precedes the ECS HTTP request. A local
                    # budget expiry here is not evidence of bad credentials.
                    state, no_start_reason = "not_started", oauth[-1].get("status")
                elif reason in ("connect_error", "connect_timeout", "pool_timeout"):
                    state, no_start_reason = "not_started", reason
                elif reason == "ok" or any(row.get("status") == "ok" for row in backend_ends):
                    state = "missing"
                    anomaly("missing_backend_start", attempt_id, route_id)
                else:
                    # A timeout or connection interruption cannot establish that
                    # the remote Search never began. Do not synthesize a zero.
                    state, no_start_reason = "unknown", reason
            elif len(backend_starts) != 1:
                anomaly("duplicate_backend_start", attempt_id, route_id)
            platform_id: object = None
            worker_rows = backend_starts + backend_ends + fallback
            if worker_rows:
                platform_id = worker_rows[0].get("aws_request_id")
            search = searches.get(platform_id, []) if isinstance(platform_id, str) else []
            if len(search) > 1:
                anomaly("ambiguous_search_record", attempt_id, platform_id)
            search_state = "observed" if len(search) == 1 else "not_started"
            if state != "not_started" and len(search) != 1:
                search_state = "missing" if not search else "ambiguous"
                if not search:
                    anomaly("missing_search_record", attempt_id, platform_id)
            completion = "not_started" if state == "not_started" else "unknown"
            if backend_ends:
                completion = "observed"
                if len(backend_ends) != 1:
                    anomaly("duplicate_backend_end", attempt_id, platform_id)
            elif state != "not_started":
                anomaly("missing_backend_end", attempt_id, platform_id)
            observation = search[0] if len(search) == 1 else {}
            if len(search) == 1:
                linked_searches.add(id(search[0]))
            timing = observation.get("embedding_timing")
            embedding_state = "observed" if isinstance(timing, dict) else "not_attempted"
            if search_state in {"missing", "ambiguous"}:
                embedding_state = "missing"
            elif observation.get("vector_availability") in {
                "available",
                "failed",
            } and not isinstance(timing, dict):
                embedding_state = "missing"
                anomaly("missing_embedding_timing", attempt_id, platform_id)
            if isinstance(timing, dict):
                keys = ("search_remaining_ms", "stage_budget_ms", "configured_timeout_ms")
                budgets = {key: _number(timing.get(key)) for key in keys}
                caps = [value for value in budgets.values() if value is not None and value >= 0]
                samples = timing.get("attempts")
                incomplete = (
                    len(caps) != 3
                    or not isinstance(samples, list)
                    or timing.get("diagnostics_failed") is True
                    or timing.get("attempts_truncated") is True
                )
                stage_budget = budgets["stage_budget_ms"]
                search_remaining = budgets["search_remaining_ms"]
                configured = budgets["configured_timeout_ms"]
                if (
                    stage_budget is not None
                    and search_remaining is not None
                    and configured is not None
                    and stage_budget > min(search_remaining, configured) + 1
                ):
                    anomaly("embedding_stage_budget_exceeded", attempt_id, platform_id)
                spent_http = 0.0
                spent_known = True
                if isinstance(samples, list):
                    if not samples and observation.get("vector_availability") == "available":
                        incomplete = True
                    for sample in samples:
                        if not isinstance(sample, dict):
                            incomplete = True
                            anomaly("invalid_embedding_attempt", attempt_id, platform_id)
                            continue
                        effective = _number(sample.get("effective_timeout_ms"))
                        elapsed = _number(sample.get("total_ms"))
                        if (
                            effective is None
                            or effective < 0
                            or elapsed is None
                            or elapsed < 0
                            or sample.get("diagnostics_failed") is True
                        ):
                            incomplete = True
                        if effective is not None and caps and effective > min(caps) + 1:
                            anomaly("embedding_budget_exceeded", attempt_id, platform_id)
                        # Non-HTTP credential work can reduce the real remainder
                        # further. This is a conservative no-reset bound, not an
                        # assertion that total_http explains every elapsed ms.
                        if (
                            spent_known
                            and effective is not None
                            and stage_budget is not None
                            and effective > max(0.0, stage_budget - spent_http) + 1
                        ):
                            anomaly("embedding_attempt_budget_reset", attempt_id, platform_id)
                        if elapsed is None or elapsed < 0:
                            spent_known = False
                        else:
                            spent_http += elapsed
                if incomplete:
                    embedding_state = "incomplete"
                    anomaly("incomplete_embedding_timing", attempt_id, platform_id)
            timeline = [
                row for row in rows if row.get("operation") not in ("search_route", "search")
            ]
            timeline.sort(key=lambda row: _number(row.get("timestamp_ms")) or 0.0)
            attempts.append(
                {
                    "attempt_id": attempt_id,
                    "backend": starts[0].get("backend") if starts else None,
                    "backend_start": state,
                    "no_start_reason": no_start_reason,
                    "status": reason,
                    "platform_request_id": platform_id,
                    "cold_start": fallback[-1].get("cold_start") if fallback else None,
                    "embedding_timing": timing,
                    "embedding_observation": embedding_state,
                    "search_record": search_state,
                    "completion": completion,
                    "vector_availability": observation.get("vector_availability"),
                    "degraded": observation.get("degraded"),
                    "timeline": timeline,
                }
            )
        correlation = summary.get("correlation_id")
        matched_clients = clients.get(correlation, []) if isinstance(correlation, str) else []
        if len(matched_clients) > 1:
            anomaly("ambiguous_client_correlation", request_id=route_id)
        if len(matched_clients) == 1:
            linked_clients.add(id(matched_clients[0]))
        routes.append({"summary": summary, "attempts": attempts, "client_records": matched_clients})
    for attempt_id in by_attempt.keys() - linked_attempts:
        anomaly("orphan_attempt", attempt_id)
    unmatched_searches = [
        row for row in _events(records, "search") if id(row) not in linked_searches
    ]
    unmatched_clients = [
        row for row in _events(records, "search_client") if id(row) not in linked_clients
    ]
    # Rejections before decode legitimately lack an attempt ID. Retain their
    # source and status without assigning them to a nearby, unrelated request.
    unmatched_ingress = [
        row for row in _events(records, "search_ingress") if id(row) not in linked_ingress
    ]
    unmatched_backends = [
        row
        for row in records
        if row.get("operation") in {"search_backend", "search_fallback"}
        and id(row) not in linked_backends
    ]
    for row in unmatched_searches:
        anomaly("unmatched_search_record", request_id=row.get("aws_request_id"))
    for row in unmatched_clients:
        anomaly("unmatched_client", request_id=row.get("aws_request_id"))
    for row in unmatched_ingress:
        anomaly("unmatched_ingress_record", row.get("attempt_id"), row.get("aws_request_id"))
    for row in unmatched_backends:
        anomaly("unmatched_backend_record", row.get("attempt_id"), row.get("aws_request_id"))
    return {
        "version": 1,
        "routes": routes,
        "unmatched_search_records": unmatched_searches,
        "unmatched_clients": unmatched_clients,
        "unmatched_ingress_records": unmatched_ingress,
        "unmatched_backend_records": unmatched_backends,
        "anomalies": anomalies,
        "join_rules": {
            "route": "facade aws_request_id",
            "attempt": "exact attempt_id",
            "search": "backend aws_request_id to runtime search aws_request_id",
            "client": "unique correlation_id within one run, route arm and region",
            "timeline": "wall timestamps are descriptive; cross-host clock skew is not latency",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--collected", type=Path)
    options = parser.parse_args()
    records, collection = collect(options.input)
    report = analyze(records)
    report["collection"] = collection
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if options.collected:
        options.collected.parent.mkdir(parents=True, exist_ok=True)
        with options.collected.open("w", encoding="utf-8") as target:
            for record in records:
                target.write(json.dumps(record, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Join captured Search quality and Gateway errors without cloud access.

Read an existing per-request ledger and its original sanitized client/CloudWatch
exports. No secret retrieval, new measurements, inferred quality, or wire-byte
equivalence is involved. The output must be a new file.
"""

from __future__ import annotations

import argparse
import copy
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

Record = dict[str, Any]
BACKEND_GROUP = {"direct_lambda": "search", "ecs": "ecs", "fallback": "fallback"}
ARMS = frozenset({"before", "after", "settled"})
IDENTITY_FIELDS = ("trace_root", "request_sequence", "query_sha256", "phase")


def enrich(
    records: list[Record],
    searches: dict[str, list[Record]],
    clients: dict[str, list[Record]],
) -> list[Record]:
    output = copy.deepcopy(records)
    seen_requests: set[tuple[str, str]] = set()
    seen_backends: set[tuple[str, str, str]] = set()
    for row in output:
        fields = ("arm", "correlation_id", "trace_root", "query_sha256", "phase", "status")
        if (
            any(not isinstance(row.get(field), str) or not row[field] for field in fields)
            or row["arm"] not in ARMS
            or type(row.get("request_sequence")) is not int
            or row["request_sequence"] < 0
        ):
            raise ValueError("request identity is missing or invalid")
        arm, correlation = row["arm"], row["correlation_id"]
        request_key = (arm, correlation)
        if request_key in seen_requests:
            raise ValueError("duplicate ledger request")
        seen_requests.add(request_key)
        matches = [
            client
            for client in clients.get(arm, [])
            if client.get("record_state") == "finished"
            and client.get("correlation_id") == correlation
        ]
        if len(matches) != 1 or any(
            matches[0].get(field) != row.get(field) for field in IDENTITY_FIELDS
        ):
            raise ValueError("client evidence is missing, ambiguous, or inconsistent")
        client = matches[0]
        if client.get("status") != row.get("status"):
            raise ValueError("client outcome disagrees with the ledger")
        row["gateway_error"] = (
            {
                "response": client.get("response"),
                "response_sha256": client.get("response_sha256"),
                "http_status": client.get("http_status"),
                "source": f"measurement-{arm}-finished.jsonl",
                "scope": "captured sanitized decoded response; digest refers to received bytes",
            }
            if row.get("status") != "ok"
            else None
        )
        backends = row.get("backends")
        if not isinstance(backends, list):
            raise ValueError("backend collection must be a list")
        for backend in backends:
            if (
                not isinstance(backend, dict)
                or not isinstance(backend.get("backend"), str)
                or backend["backend"] not in BACKEND_GROUP
            ):
                raise ValueError("backend record must identify a supported backend")
            group = BACKEND_GROUP[cast(str, backend["backend"])]
            request_id = backend.get("platform_request_id")
            if group is not None and isinstance(request_id, str) and request_id:
                backend_key = (arm, group, request_id)
                if backend_key in seen_backends:
                    raise ValueError("backend Search evidence reused across ledger entries")
                seen_backends.add(backend_key)
            observations = [
                search
                for search in searches.get(arm, [])
                if isinstance(request_id, str)
                and request_id
                and search.get("operation") == "search"
                and search.get("aws_request_id") == request_id
                and search.get("_source_function") == group
            ]
            backend["quality_state"] = "MISSING" if not observations else "AMBIGUOUS"
            backend["quality_source"] = None
            if len(observations) != 1:
                backend["vector_availability"] = backend["degraded"] = None
                continue
            observation = observations[0]
            vector = observation.get("vector_availability")
            degraded = observation.get("degraded")
            for field in ("vector_availability", "degraded"):
                if field in backend and backend[field] != observation.get(field):
                    raise ValueError("quality evidence disagrees with the ledger")
            backend["vector_availability"] = vector
            backend["degraded"] = degraded
            backend["quality_state"] = (
                "OBSERVED" if isinstance(vector, str) and type(degraded) is bool else "INCOMPLETE"
            )
            backend["quality_source"] = {
                "function": group,
                "aws_request_id": request_id,
                "event_id": observation.get("_event_id"),
                "log_stream": observation.get("_log_stream"),
            }
    return output


def _invalid_constant(value: str) -> object:
    raise ValueError("nonfinite JSON number")


def _object(raw: str) -> Record:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> Record:
        result: Record = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(raw, parse_constant=_invalid_constant, object_pairs_hook=unique_pairs)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return cast(Record, value)


def _lines(path: Path) -> list[Record]:
    return [_object(line) for line in path.read_text().splitlines() if line.strip()]


def load_searches(directory: Path, arms: set[str]) -> dict[str, list[Record]]:
    result: dict[str, list[Record]] = defaultdict(list)
    for arm in sorted(arms):
        groups = ("search",) if arm == "before" else ("ecs", "fallback")
        for group in groups:
            path = directory / f"{arm}-log-events-{group}.json"
            value = _object(path.read_text())
            events = value.get("events")
            if not isinstance(events, list):
                raise ValueError("complete events export required")
            seen: dict[str, Record] = {}
            for event in events:
                if not isinstance(event, dict) or not isinstance(event.get("message"), str):
                    raise ValueError("invalid CloudWatch event")
                event_id = event.get("eventId")
                if not isinstance(event_id, str):
                    raise ValueError("event identity required")
                if event_id in seen:
                    if event != seen[event_id]:
                        raise ValueError("duplicate event identity disagrees")
                    continue
                seen[event_id] = event
                message = event["message"]
                if not message.lstrip().startswith("{"):
                    continue
                record = _object(message)
                if record.get("operation") == "search":
                    result[arm].append(
                        {
                            **record,
                            "_event_id": event_id,
                            "_log_stream": event.get("logStreamName"),
                            "_source_function": group,
                        }
                    )
    return dict(result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--exports", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = _lines(args.records)
    arms = {row.get("arm") for row in records}
    if not arms or not arms <= ARMS:
        raise ValueError("unexpected measurement arm")
    names = cast(set[str], arms)
    clients = {arm: _lines(args.exports / f"measurement-{arm}-finished.jsonl") for arm in names}
    output = enrich(records, load_searches(args.exports, names), clients)
    with args.output.open("x", encoding="utf-8") as target:
        for row in output:
            target.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()

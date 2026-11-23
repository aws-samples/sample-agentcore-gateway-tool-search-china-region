"""IAM-only Lambda worker; never attempts routing or accepts a Gateway target directly."""

from __future__ import annotations

import hashlib
import math
import time
from typing import cast

from semantic_service.facade import (
    DeadlineExpired,
    LambdaContext,
    emit_record,
    enforce_deadline,
    platform_request_id,
)
from semantic_service.route_budget import (
    FALLBACK_SECONDS,
    MIN_ATTEMPT_SECONDS,
    PLATFORM_RESERVE_SECONDS,
)
from semantic_service.transport import decode_request, encode_json

_COLD_START = True


def handler(event: object, context: object) -> dict[str, object]:
    global _COLD_START
    started = time.monotonic()
    cold_start, _COLD_START = _COLD_START, False
    status, stage = "internal failure", "admission"
    attempt: object = None
    correlation: str | None = None
    deadline: int | None = None
    request_id = platform_request_id(context)
    factory_ms = 0.0
    try:
        from semantic_service.ecs_backend import backend_factory

        request = decode_request(encode_json(event))
        attempt = request["attempt_id"]
        custom = cast(dict[str, object], request["custom"])
        mcp_id = custom.get("bedrockAgentCoreMcpMessageId")
        if isinstance(mcp_id, str) and len(mcp_id) <= 256:
            correlation = hashlib.sha256(mcp_id.encode()).hexdigest()
        remaining = cast(LambdaContext, context).get_remaining_time_in_millis()
        if (
            type(remaining) not in (int, float)
            or not math.isfinite(remaining)
            or remaining < MIN_ATTEMPT_SECONDS * 1000
        ):
            raise ValueError()
        deadline = min(
            cast(int, request["deadline_ms"]),
            int(time.time() * 1000 + remaining - PLATFORM_RESERVE_SECONDS * 1000),
            int(time.time() * 1000 + FALLBACK_SECONDS * 1000),
        )
        request["deadline_ms"] = deadline
        end = min(
            started + FALLBACK_SECONDS,
            time.monotonic() + deadline / 1000 - time.time(),
        )
        stage = "factory"
        result: dict[str, object] | None = None
        try:
            with enforce_deadline(end):
                factory_started = time.monotonic()
                try:
                    worker = backend_factory("fallback")
                finally:
                    factory_ms = (time.monotonic() - factory_started) * 1000
                stage = "backend"
                result = worker.handle(request, backend="fallback", platform_request_id=request_id)
        except DeadlineExpired:
            if result is None:
                raise
        error = result.get("error")
        if result.get("ok") is True:
            status = "ok"
        elif error in {
            "unauthenticated",
            "unauthorized",
            "invalid input",
            "rate limited",
            "index not ready",
            "replay rejected",
            "replay unavailable",
            "deadline exceeded",
            "clock skew",
            "internal failure",
        }:
            status = cast(str, error)
        return result
    except DeadlineExpired:
        status = "deadline exceeded"
        return {"version": 1, "ok": False, "error": status, "retryable": True}
    except Exception:
        return {"version": 1, "ok": False, "error": "internal failure", "retryable": False}
    finally:
        emit_record(
            {
                "operation": "search_fallback",
                "phase": "end",
                "backend": "fallback",
                "status": status,
                "failure_stage": stage if status != "ok" else "none",
                "attempt_id": attempt,
                "aws_request_id": request_id,
                "correlation_id": correlation,
                "timestamp_ms": int(time.time() * 1000),
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "remaining_ms": max(0, deadline - int(time.time() * 1000)) if deadline else 0,
                "factory_ms": round(factory_ms, 3),
                "cold_start": cold_start,
            }
        )

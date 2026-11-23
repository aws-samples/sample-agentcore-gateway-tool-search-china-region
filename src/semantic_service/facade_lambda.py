"""Facade-only Lambda initialization; importing fallback never enters this module."""

# ruff: noqa: E402
from __future__ import annotations

import time

_STARTED = time.perf_counter()

# isort: split
import json

_IMPORT_STARTED = time.perf_counter()
try:
    from semantic_service import facade
except BaseException as error:
    elapsed = round((time.perf_counter() - _IMPORT_STARTED) * 1000, 3)
    _NAME = type(error).__name__
    print(
        json.dumps(
            {
                "operation": "search_facade_initialization",
                "phase": "end",
                "status": "failed",
                "failure_stage": "module_import",
                "exception_type": (
                    _NAME
                    if len(_NAME) <= 128 and _NAME.isascii() and _NAME.isidentifier()
                    else "unknown"
                ),
                "module_import_ms": elapsed,
                "duration_ms": round((time.perf_counter() - _STARTED) * 1000, 3),
                "timestamp_ms": int(time.time() * 1000),
            }
        ),
        flush=True,
    )
    raise RuntimeError("facade initialization failed") from None

_MODULE_IMPORT_MS = round((time.perf_counter() - _IMPORT_STARTED) * 1000, 3)
try:
    _ROUTER = facade._router()
except BaseException as error:
    facade.emit_record(
        {
            "operation": "search_facade_initialization",
            "phase": "end",
            "status": "failed",
            "failure_stage": "factory",
            "exception_type": (
                error.exception_type
                if isinstance(error, facade._InitializationError)
                else facade.initialization_error_type(error)
            ),
            "module_import_ms": _MODULE_IMPORT_MS,
            "duration_ms": round((time.perf_counter() - _STARTED) * 1000, 3),
            "stage_timings": getattr(error, "initialization_timings", {}),
            "timestamp_ms": int(time.time() * 1000),
        }
    )
    raise facade.ServiceError() from None
else:
    facade.emit_record(
        {
            "operation": "search_facade_initialization",
            "phase": "end",
            "status": "ok",
            "failure_stage": "none",
            "module_import_ms": _MODULE_IMPORT_MS,
            "duration_ms": round((time.perf_counter() - _STARTED) * 1000, 3),
            "stage_timings": _ROUTER.initialization_timings,
            "timestamp_ms": int(time.time() * 1000),
        }
    )


def handler(event: object, context: object) -> dict[str, object]:
    return facade.initialized_handler(_ROUTER, event, context)

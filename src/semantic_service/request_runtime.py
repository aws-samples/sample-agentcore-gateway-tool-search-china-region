"""REQUEST entry point; provisioned Lambda Init succeeds only after preparation."""

from __future__ import annotations

import time

_ENTRY_IMPORT_STARTED = time.perf_counter()

# Load runtime first to preserve its historical import timing boundary.
from semantic_service import runtime  # noqa: E402

# isort: split
import json  # noqa: E402
import os  # noqa: E402

from semantic_core._timing import collect, stage  # noqa: E402
from semantic_service import _request_prefetch  # noqa: E402
from semantic_service.auth import OIDCVerifier  # noqa: E402
from semantic_service.interceptor import RequestInterceptor  # noqa: E402


def request_handler(event: object, context: object) -> dict[str, object]:
    """Forward to the current runtime handler, including any installed wrapper."""
    return runtime.request_handler(event, context)


def _prefetch_oidc(
    handler: RequestInterceptor,
) -> tuple[dict[str, object], _request_prefetch.StageTimings]:
    try:
        verifier = handler.verifier
        if isinstance(verifier, OIDCVerifier):
            return _request_prefetch.prefetch(verifier)
    except BaseException:
        # A factory shape/programming error is distinct from an IdP outage.
        pass
    return {"status": "failed", "failure_kind": "structural"}, {}


def _prepare(entry_import_ms: float) -> None:
    if not (
        os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip()
        and os.environ.get("_HANDLER") == f"{__name__}.request_handler"
        and os.environ.get("AWS_LAMBDA_INITIALIZATION_TYPE") == "provisioned-concurrency"
    ):
        # No network, collector or Lambda telemetry on build hosts or on demand.
        return
    status = "failed"
    oidc_prefetch: dict[str, object] = {"status": "not_started"}
    prefetch_stages: _request_prefetch.StageTimings = {}
    with collect() as timings:
        factory_started = time.perf_counter()
        factory_elapsed_ms: float | None = None
        try:
            with stage("handler.factory"):
                handler = runtime._request()
            factory_elapsed_ms = max(0.0, time.perf_counter() - factory_started) * 1000
            oidc_prefetch = {"status": "interrupted"}
            oidc_prefetch, prefetch_stages = _prefetch_oidc(handler)
            status = "ready"
        except Exception:
            # Fail Init so Lambda cannot advertise a broken PC environment as
            # ready. Hide original exception text from the platform traceback.
            # Successful child factories and pinned configuration remain cached
            # in this failed process; recovery requires a new environment (and
            # a new configuration reference for invalid pinned configuration).
            raise RuntimeError("REQUEST initialization failed") from None
        finally:
            # Factory process-control failures still propagate; optional OIDC
            # failures are contained by _prefetch_oidc.
            stages = timings.snapshot() | prefetch_stages
            factory_ms = stages.get("handler.factory", {}).get("duration_ms")
            if factory_ms is None:
                # Optional diagnostics must not replace the factory's outcome.
                factory_ms = round(
                    factory_elapsed_ms
                    if factory_elapsed_ms is not None
                    else max(0.0, time.perf_counter() - factory_started) * 1000,
                    3,
                )
            print(
                json.dumps(
                    {
                        "operation": "request_init",
                        "status": status,
                        "duration_ms": round(
                            max(0.0, time.perf_counter() - factory_started) * 1000, 3
                        ),
                        "request_entry_import_ms": entry_import_ms,
                        "runtime_module_init_ms": runtime._RUNTIME_MODULE_INIT_MS,
                        "factory_ms": factory_ms,
                        "oidc_prefetch": oidc_prefetch,
                        "stage_timings": stages,
                    }
                ),
                flush=True,
            )


_ENTRY_IMPORT_MS = round((time.perf_counter() - _ENTRY_IMPORT_STARTED) * 1000, 3)
_prepare(_ENTRY_IMPORT_MS)

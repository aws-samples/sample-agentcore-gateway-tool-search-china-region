"""Bounded, request-local internal timing; never collect arguments or exceptions."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from threading import Lock

# Names are code-owned. Unknown names are ignored, so request text cannot create
# high-cardinality fields or leak into logs through this internal helper.
_STAGES = frozenset(
    {
        "handler.factory",
        "handler.work",
        "runtime.config",
        "runtime.aws_client",
        "runtime.aws_session",
        "runtime.aws_client.s3",
        "runtime.aws_client.dynamodb",
        "runtime.aws_client.secretsmanager",
        "runtime.aws_client.bedrock_agentcore_control",
        "runtime.http_client",
        "runtime.models",
        "auth.oidc",
        "auth.discovery",
        "auth.jwks",
        "auth.envelope",
        "auth.authorize",
        "auth.rate_limit",
        "aws.secrets",
        "aws.control",
        "aws.policy",
        "aws.safety",
        "snapshot.refresh",
        "snapshot.manifest",
        "snapshot.objects",
        "snapshot.load",
        "tokenizer.import",
        "tokenizer.base_dictionary",
        "tokenizer.dictionary",
        "tokenizer.dictionary.read",
        "tokenizer.dictionary.decode",
        "model.setup",
        "model.embedding",
        "model.rerank",
        "search.authorize",
        "search.core",
        "search.bm25",
        "search.vector",
        "search.ranking",
        "search.response",
    }
)


@dataclass
class _Aggregate:
    count: int = 0
    duration_ms: float = 0
    max_ms: float = 0
    errors: int = 0


class Timings:
    """Inclusive wall times: nested stages must not be summed as a total."""

    def __init__(self) -> None:
        self._values: dict[str, _Aggregate] = {}
        self._lock = Lock()

    def record(self, name: str, elapsed: float, failed: bool) -> None:
        if name not in _STAGES or not math.isfinite(elapsed):
            return
        duration = max(0.0, elapsed * 1000)
        with self._lock:
            value = self._values.setdefault(name, _Aggregate())
            value.count += 1
            value.duration_ms += duration
            value.max_ms = max(value.max_ms, duration)
            value.errors += int(failed)

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        with self._lock:
            return {
                name: {
                    "count": value.count,
                    "duration_ms": round(value.duration_ms, 3),
                    "max_ms": round(value.max_ms, 3),
                    "errors": value.errors,
                }
                for name, value in sorted(self._values.items())
            }


_CURRENT: ContextVar[Timings | None] = ContextVar("semantic_stage_timings", default=None)


@contextmanager
def collect() -> Iterator[Timings]:
    captured = Timings()
    token = _CURRENT.set(captured)
    try:
        yield captured
    finally:
        _CURRENT.reset(token)


@contextmanager
def stage(name: str) -> Iterator[None]:
    captured = _CURRENT.get()
    if captured is None or name not in _STAGES:
        yield
        return
    started = time.perf_counter()
    failed = True
    try:
        yield
        failed = False
    finally:
        try:
            captured.record(name, time.perf_counter() - started, failed)
        # Timing is best effort and cannot replace the application exception.
        except Exception:  # nosec B110
            # Diagnostics cannot replace an application result or exception.
            pass


def timed[**P, R](name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(action: Callable[P, R]) -> Callable[P, R]:
        @wraps(action)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with stage(name):
                return action(*args, **kwargs)

        return wrapped

    return decorate

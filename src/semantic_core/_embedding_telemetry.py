"""Bounded embedding HTTP observations; never inspect trace metadata or payloads."""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import Literal

ConnectionState = Literal["new_connect_observed", "existing_connection_inferred", "unknown"]
_MAX_ATTEMPTS = 2
_MAX_MILLISECONDS = 2_147_483_647.0
_CONNECT_EVENTS = {
    "connection.connect_tcp.started": ("tcp", "started"),
    "connection.connect_tcp.complete": ("tcp", "ended"),
    "connection.connect_tcp.failed": ("tcp", "ended"),
    "connection.start_tls.started": ("tls", "started"),
    "connection.start_tls.complete": ("tls", "ended"),
    "connection.start_tls.failed": ("tls", "ended"),
}
_SEND_STARTED = frozenset(
    {"http11.send_request_headers.started", "http2.send_request_headers.started"}
)
_HEADERS_COMPLETE = frozenset(
    {"http11.receive_response_headers.complete", "http2.receive_response_headers.complete"}
)


def _now() -> float:
    value = time.monotonic()
    if not math.isfinite(value):
        raise ValueError("invalid diagnostic clock")
    return value


def _milliseconds(seconds: float) -> float:
    if not math.isfinite(seconds):
        raise ValueError("invalid diagnostic duration")
    return round(min(_MAX_MILLISECONDS, max(0.0, seconds * 1000)), 3)


def _elapsed(start: float | None, end: float | None) -> float | None:
    return None if start is None or end is None else _milliseconds(end - start)


@dataclass(frozen=True, slots=True)
class HTTPAttemptTiming:
    attempt: int
    effective_timeout_ms: float | None
    http_status: int | None
    tcp_ms: float | None
    tls_ms: float | None
    response_headers_ms: float | None
    first_body_chunk_ms: float | None
    body_ms: float | None
    total_ms: float | None
    connection_state: ConnectionState
    body_complete: bool
    diagnostics_failed: bool


@dataclass(frozen=True, slots=True)
class EmbeddingTiming:
    search_remaining_ms: float | None
    stage_budget_ms: float | None
    configured_timeout_ms: float | None
    attempts: tuple[HTTPAttemptTiming, ...] = ()
    attempts_truncated: bool = False
    diagnostics_failed: bool = False


class AttemptTrace:
    """One HTTP attempt; timings include local scheduling and consumer work.

    TCP includes DNS resolution. Response headers means the complete final
    headers, and first_body_chunk means the first nonempty decoded body chunk.
    Neither field measures the first byte on the wire. A body timer covers the
    caller's iteration, including decoding and local checks.
    """

    def __init__(self, attempt: int, effective_timeout: float) -> None:
        self._lock = Lock()
        self._attempt = attempt
        self._started: float | None = None
        self._effective_timeout_ms: float | None = None
        self._http_status: int | None = None
        self._phase_starts: dict[str, float] = {}
        self._phase_ms: dict[str, float] = {}
        self._headers_at: float | None = None
        self._first_chunk_at: float | None = None
        self._body_started: float | None = None
        self._body_ms: float | None = None
        self._total_ms: float | None = None
        self._connect_observed = False
        self._send_observed = False
        self._body_complete = False
        self._diagnostics_failed = False
        try:
            self._started = _now()
            self._effective_timeout_ms = _milliseconds(effective_timeout)
        except Exception:
            self._diagnostics_failed = True

    def mark_failed(self) -> None:
        with self._lock:
            self._diagnostics_failed = True

    def trace(self, name: str, info: object) -> None:
        # httpcore does not catch callback errors. Never inspect `info`: it can
        # contain request headers, hostnames, streams or raw exceptions.
        try:
            self._on_event(name)
        except Exception:
            self.mark_failed()

    def _on_event(self, name: str) -> None:
        with self._lock:
            if name in _SEND_STARTED:
                self._send_observed = True
            elif name in _HEADERS_COMPLETE:
                self._headers_at = _now()
            elif event := _CONNECT_EVENTS.get(name):
                phase, action = event
                now = _now()
                if action == "started":
                    if phase == "tcp":
                        self._connect_observed = True
                    self._phase_starts[phase] = now
                else:
                    started = self._phase_starts.pop(phase, None)
                    if started is None:
                        self._diagnostics_failed = True
                    else:
                        self._phase_ms[phase] = min(
                            _MAX_MILLISECONDS,
                            self._phase_ms.get(phase, 0.0) + _milliseconds(now - started),
                        )

    def headers_received(self, status: int | None) -> None:
        with self._lock:
            if type(status) is int and 100 <= status <= 599:
                self._http_status = status
            if self._headers_at is None:
                # Custom transports need not emit httpcore trace events. The
                # returned response still establishes that headers were received.
                self._headers_at = _now()

    def body_started(self) -> None:
        with self._lock:
            self._body_started = _now()

    def body_chunk(self) -> None:
        with self._lock:
            if self._first_chunk_at is None:
                self._first_chunk_at = _now()

    def body_finished(self, complete: bool) -> None:
        with self._lock:
            self._body_ms = _elapsed(self._body_started, _now())
            self._body_complete = complete

    def finish(self) -> None:
        with self._lock:
            self._total_ms = _elapsed(self._started, _now())

    def snapshot(self) -> HTTPAttemptTiming:
        with self._lock:
            state: ConnectionState = "unknown"
            if not self._diagnostics_failed:
                if self._connect_observed:
                    state = "new_connect_observed"
                elif self._send_observed:
                    # No connection identity is retained. This is inference from
                    # a supported send event without a connection setup event.
                    state = "existing_connection_inferred"
            return HTTPAttemptTiming(
                attempt=self._attempt,
                effective_timeout_ms=self._effective_timeout_ms,
                http_status=self._http_status,
                tcp_ms=self._phase_ms.get("tcp"),
                tls_ms=self._phase_ms.get("tls"),
                response_headers_ms=_elapsed(self._started, self._headers_at),
                first_body_chunk_ms=_elapsed(self._started, self._first_chunk_at),
                body_ms=self._body_ms,
                total_ms=self._total_ms,
                connection_state=state,
                body_complete=self._body_complete,
                diagnostics_failed=self._diagnostics_failed,
            )


class EmbeddingTrace:
    def __init__(
        self, search_deadline: float, stage_deadline: float, configured_timeout: float
    ) -> None:
        self._lock = Lock()
        self._attempts: list[AttemptTrace] = []
        self._truncated = False
        self._diagnostics_failed = False
        self._search_remaining_ms: float | None = None
        self._stage_budget_ms: float | None = None
        self._configured_timeout_ms: float | None = None
        try:
            now = _now()
            self._search_remaining_ms = _milliseconds(search_deadline - now)
            self._stage_budget_ms = _milliseconds(stage_deadline - now)
            self._configured_timeout_ms = _milliseconds(configured_timeout)
        except Exception:
            self._diagnostics_failed = True

    def mark_failed(self) -> None:
        with self._lock:
            self._diagnostics_failed = True

    def start_attempt(self, effective_timeout: float) -> AttemptTrace | None:
        with self._lock:
            if len(self._attempts) >= _MAX_ATTEMPTS:
                self._truncated = True
                return None
            attempt = AttemptTrace(len(self._attempts) + 1, effective_timeout)
            self._attempts.append(attempt)
            return attempt

    def snapshot(self) -> EmbeddingTiming:
        with self._lock:
            attempts = tuple(self._attempts)
            failed = self._diagnostics_failed
            truncated = self._truncated
        snapshots: list[HTTPAttemptTiming] = []
        for attempt in attempts:
            try:
                snapshot = attempt.snapshot()
                snapshots.append(snapshot)
                failed = failed or snapshot.diagnostics_failed
            except Exception:
                failed = True
        return EmbeddingTiming(
            search_remaining_ms=self._search_remaining_ms,
            stage_budget_ms=self._stage_budget_ms,
            configured_timeout_ms=self._configured_timeout_ms,
            attempts=tuple(snapshots),
            attempts_truncated=truncated,
            diagnostics_failed=failed,
        )


_CURRENT: ContextVar[EmbeddingTrace | None] = ContextVar("embedding_http_trace", default=None)


@contextmanager
def collect_embedding(
    search_deadline: float, stage_deadline: float, configured_timeout: float
) -> Iterator[EmbeddingTrace | None]:
    try:
        captured: EmbeddingTrace | None = EmbeddingTrace(
            search_deadline, stage_deadline, configured_timeout
        )
    except Exception:
        captured = None
    token = _CURRENT.set(captured)
    try:
        yield captured
    finally:
        _CURRENT.reset(token)


def snapshot_embedding(captured: EmbeddingTrace | None) -> EmbeddingTiming:
    """Diagnostics must not replace a successful result or the provider's error."""
    if captured is not None:
        try:
            return captured.snapshot()
        # Return diagnostics_failed below; telemetry must not change provider results.
        except Exception:  # nosec B110
            pass
    return EmbeddingTiming(None, None, None, diagnostics_failed=True)


def start_http_attempt(effective_timeout: float) -> AttemptTrace | None:
    captured = _CURRENT.get()
    if captured is None:
        return None
    try:
        return captured.start_attempt(effective_timeout)
    except Exception:
        captured.mark_failed()
        return None


def http_extensions(attempt: AttemptTrace | None) -> dict[str, object] | None:
    return None if attempt is None else {"trace": attempt.trace}


def record_http(
    attempt: AttemptTrace | None,
    event: Literal["headers", "body_started", "body_chunk", "body_finished", "finish"],
    *,
    status: int | None = None,
    complete: bool = False,
) -> None:
    if attempt is None:
        return
    try:
        if event == "headers":
            attempt.headers_received(status)
        elif event == "body_started":
            attempt.body_started()
        elif event == "body_chunk":
            attempt.body_chunk()
        elif event == "body_finished":
            attempt.body_finished(complete)
        else:
            attempt.finish()
    except Exception:
        attempt.mark_failed()

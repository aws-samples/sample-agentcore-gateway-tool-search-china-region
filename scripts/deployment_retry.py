"""Persist bounded request attempts and check ambiguous writes before retrying.

This module creates no SDK clients and binds no account, region, or deployment.
Callers supply each request and its optional check for a write that already landed.
Reuse the same journal for a request after interruption; never reset its budget.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TRANSIENT = {
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "InternalFailure",
    "InternalError",
    "RequestTimeout",
    "RequestTimeoutException",
    "RequestLimitExceeded",
    "SlowDown",
}
NETWORK = {
    "EndpointConnectionError",
    "ConnectionClosedError",
    "ReadTimeoutError",
    "ConnectTimeoutError",
    "HTTPClientError",
}
REJECTED = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedException",
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
    "SignatureDoesNotMatch",
    "InvalidSignatureException",
    "ValidationError",
    "ValidationException",
}


def utc() -> datetime:
    return datetime.now(UTC)


def serial(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(type(value).__name__)


def canonical(value: object) -> bytes:
    """Preserve request-key serialization used by existing deployment journals."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=serial
    ).encode()


def error_section(error: BaseException, key: str) -> dict[str, Any]:
    response = getattr(error, "response", None)
    value = response.get(key) if isinstance(response, dict) else None
    return value if isinstance(value, dict) else {}


def error_code(error: BaseException) -> str:
    code = error_section(error, "Error").get("Code")
    return code if isinstance(code, str) else type(error).__name__


def transient(error: BaseException) -> bool:
    status = error_section(error, "ResponseMetadata").get("HTTPStatusCode", 0)
    code = error_code(error)
    if (isinstance(status, int) and status in {401, 403}) or code in REJECTED:
        return False
    return (
        isinstance(error, TimeoutError | ConnectionError)
        or type(error).__name__ in NETWORK
        or code in TRANSIENT
        or isinstance(status, int)
        and (status in {408, 429} or 500 <= status < 600)
    )


class Journal:
    """Append request attempts durably before invoking the supplied request."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line]

    def emit(self, row: dict[str, Any]) -> None:
        row = {"utc": utc().isoformat(), **row}
        with self.path.open("a") as stream:
            stream.write(json.dumps(row, allow_nan=False, default=serial) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def attempts(self, key: str, mutating: bool) -> list[dict[str, Any]]:
        rows = [row for row in self.rows() if row.get("key") == key]
        if not mutating:
            last = max(
                (
                    index
                    for index, row in enumerate(rows)
                    if row["event"] in {"success", "missing", "recovered"}
                ),
                default=-1,
            )
            rows = rows[last + 1 :]
        return [row for row in rows if row["event"] == "attempt"]


class Caller:
    """Allow at most five attempts within 600 seconds of the original attempt."""

    def __init__(
        self,
        journal: Journal,
        *,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.journal = journal
        self.sleep = sleep
        self.now = now

    def call[T](
        self,
        service: str,
        operation: str,
        parameters: dict[str, Any],
        fn: Callable[[], T],
        *,
        recover: Callable[[], T | None] | None = None,
        mutating: bool = False,
        missing: tuple[str, ...] = (),
        before_attempt: Callable[[], object] | None = None,
    ) -> T | None:
        key = hashlib.sha256(canonical([service, operation, parameters])).hexdigest()
        while True:
            previous = self.journal.attempts(key, mutating)
            clock = self.now()
            if len(previous) >= 5 or previous and not 0 <= clock - previous[0]["clock"] <= 600:
                raise RuntimeError("Bounded retries exhausted for " + service + "." + operation)
            if before_attempt:
                before_attempt()
                # Validation consumes the original budget, including the first attempt.
                started = previous[0]["clock"] if previous else clock
                if not 0 <= self.now() - started <= 600:
                    raise RuntimeError("Bounded retries exhausted for " + service + "." + operation)
            attempt = len(previous) + 1
            self.journal.emit(
                {
                    "event": "attempt",
                    "key": key,
                    "service": service,
                    "operation": operation,
                    "attempt": attempt,
                    "clock": clock,
                    "mutating": mutating,
                    "parameters": parameters,
                }
            )
            # These returns are inside Caller.call; the try/except is not a module body.
            try:
                result = fn()
            except Exception as error:
                code = error_code(error)
                retry = transient(error)
                if code in missing:
                    self.journal.emit({"event": "missing", "key": key, "error_code": code})
                    # nosemgrep: return-not-in-function
                    return None
                self.journal.emit(
                    {
                        "event": "failure",
                        "key": key,
                        "attempt": attempt,
                        "error_code": code,
                        "transient": retry,
                    }
                )
                # Check the same immutable target even after a non-transient conflict.
                if recover is not None:
                    recovered = recover()
                    if recovered is not None:
                        self.journal.emit({"event": "recovered", "key": key, "attempt": attempt})
                        # nosemgrep: return-not-in-function
                        return recovered
                if not retry or attempt >= 5:
                    raise RuntimeError(service + "." + operation + " failed: " + code) from None
                delay = min(16, 2 ** (attempt - 1)) + random.random()  # nosec B311
                self.journal.emit(
                    {"event": "wait", "key": key, "seconds": delay, "attempt": attempt}
                )
                self.sleep(delay)
                continue
            self.journal.emit({"event": "success", "key": key, "attempt": attempt})
            # nosemgrep: return-not-in-function
            return result

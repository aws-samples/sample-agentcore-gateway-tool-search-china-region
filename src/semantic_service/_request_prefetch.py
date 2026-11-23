"""Optional parent-process OIDC prefetch with a separate client and one deadline."""

from __future__ import annotations

import json
import math
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict

import httpx

from semantic_core._timing import collect, stage
from semantic_service.auth import (
    OIDCVerifier,
    ServiceError,
    collect_oidc_attempts,
    collect_oidc_diagnostics,
)

type StageTimings = dict[str, dict[str, float | int]]
_BUDGET_SECONDS = 3.0


class _Deadline(BaseException):
    """Cross dependency Exception handlers, then stop at the optional boundary."""


def _expired(unused_signal: int, unused_frame: object) -> None:
    raise _Deadline()


@contextmanager
def _alarm() -> Iterator[Callable[[float], None]]:
    # Borrow the process timer only when it is unused and unblocked. No fork,
    # executor, inherited client, background watchdog or replacement user timer.
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    previous = signal.getsignal(signal.SIGALRM)
    owned = False
    active = False

    def expire(signum: int, frame: object) -> None:
        nonlocal active
        if active:
            active = False
            _expired(signum, frame)

    try:
        if (
            signal.SIGALRM in old_mask
            or signal.SIGALRM in signal.sigpending()
            or signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0)
            or previous not in {signal.SIG_DFL, signal.SIG_IGN}
        ):
            raise RuntimeError("prefetch timer unavailable")
        signal.signal(signal.SIGALRM, expire)
        owned = True
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)

        def arm(deadline: float) -> None:
            nonlocal active
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Deadline()
            active = True
            signal.setitimer(signal.ITIMER_REAL, remaining)

        yield arm
    finally:
        try:
            # Other threads can receive the process-directed timer even while
            # the main thread masks it. Stop raising before restoring state.
            active = False
        finally:
            try:
                signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
                if owned:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    if signal.SIGALRM in signal.sigpending():
                        signal.sigwait({signal.SIGALRM})
            finally:
                try:
                    if owned:
                        signal.signal(signal.SIGALRM, previous)
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


def _failed(kind: str, prior: dict[str, object] | None = None) -> dict[str, object]:
    result: dict[str, object] = {"status": "failed", "failure_kind": kind}
    if prior:
        for name in ("jwks_validation", "auth_failure"):
            if name in prior:
                result[name] = prior[name]
        if prior.get("auth_recovered") is True or prior.get("metadata_recovered") is True:
            result["metadata_recovered"] = True
    return result


def _collect_prefetch(
    verifier: OIDCVerifier,
    emit: Callable[[dict[str, object]], None],
    *,
    deadline: float,
) -> tuple[dict[str, object], StageTimings]:
    fields: dict[str, object] = {"status": "failed"}
    with (
        collect() as timings,
        collect_oidc_diagnostics() as diagnostics,
        collect_oidc_attempts(emit),
    ):
        try:
            with stage("auth.oidc"):
                verifier._prefetch(first_timeout=0.8, retry_timeout=0.35, deadline=deadline)
            fields["status"] = "ready"
        except _Deadline:
            fields = _failed("deadline")
        except ServiceError as error:
            failure = error.oidc_failure
            fields["failure_kind"] = (
                "dependency"
                if failure is not None and failure.category != "unexpected"
                else "structural"
            )
            if failure is not None:
                fields["auth_failure"] = asdict(failure)
        except BaseException:
            # Optional prefetch must not turn dependency sys.exit/interrupt into
            # a failed Init. Factory failures remain outside this boundary.
            fields = _failed("structural")
        validation = diagnostics.get("jwks_validation")
        if isinstance(validation, dict):
            fields["jwks_validation"] = validation
            if validation.get("rejected", 0):
                fields = _failed("dependency", fields)
        if diagnostics.get("auth_recovered") is True:
            fields["auth_recovered" if fields["status"] == "ready" else "metadata_recovered"] = True
    return fields, timings.snapshot()


def prefetch(
    verifier: OIDCVerifier, *, budget_seconds: float = _BUDGET_SECONDS, log_fd: int = 1
) -> tuple[dict[str, object], StageTimings]:
    """Bound interruptible work and never adopt late/partial/failed metadata.

    The POSIX timer interrupts socket I/O, slow streams and raw log writes, not
    an uninterruptible native resolver/OS call. This is a software deadline,
    not a guarantee of hard real-time DNS or complete Lambda Init latency.
    """
    started = time.monotonic()
    if (
        type(budget_seconds) not in {int, float}
        or not math.isfinite(budget_seconds)
        or budget_seconds <= 0
        or type(log_fd) is not int
        or log_fd < 0
        or threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "setitimer")
    ):
        return _failed("structural"), {}
    seconds = min(_BUDGET_SECONDS, budget_seconds)
    deadline = started + seconds
    work_deadline = deadline - min(0.25, seconds / 10)
    fields: dict[str, object] = _failed("structural")
    timings: StageTimings = {}
    original_cache = verifier._keys, verifier._expires
    try:
        if verifier._keys and verifier.clock() < verifier._expires:
            return {"status": "ready"}, {}
        os.fstat(log_fd)
        with _alarm() as arm:
            arm(work_deadline)
            client: httpx.Client | None = None
            isolated: OIDCVerifier | None = None
            try:
                client = httpx.Client(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=0.8,
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
                )
                isolated = OIDCVerifier(verifier.config, client, clock=verifier.clock)

                def emit(attempt: dict[str, object]) -> None:
                    # The work alarm may already have fired inside a request;
                    # do not start a second blocking operation in its finally.
                    if time.monotonic() >= work_deadline:
                        return
                    data = memoryview(
                        (
                            json.dumps({**attempt, "phase": "init", "aws_request_id": None}) + "\n"
                        ).encode()
                    )
                    while data:
                        if time.monotonic() >= work_deadline:
                            return
                        data = data[os.write(log_fd, data) :]

                fields, timings = _collect_prefetch(isolated, emit, deadline=work_deadline)
            except _Deadline:
                fields = _failed("deadline", fields)
            except BaseException:
                fields = _failed("structural", fields)
            finally:
                if client is not None:
                    try:
                        try:
                            arm(deadline)
                        except _Deadline:
                            # Native code may have delayed delivery beyond the
                            # software deadline. Still close the owned client;
                            # do not grant it a fresh cleanup budget.
                            signal.setitimer(signal.ITIMER_REAL, 0)
                            fields = _failed("deadline", fields)
                        client.close()
                    except _Deadline:
                        fields = _failed("deadline", fields)
                    except BaseException:
                        fields = _failed("structural", fields)
            if fields["status"] == "ready" and isolated is not None:
                now = verifier.clock()
                if type(now) not in {int, float} or not math.isfinite(now):
                    return _failed("structural", fields), timings
                expires = min(isolated._expires, now + verifier.config.cache_seconds)
                if expires <= now or not isolated._keys:
                    return _failed("structural", fields), timings
                if time.monotonic() >= deadline:
                    return _failed("deadline", fields), timings
                verifier._keys, verifier._expires = isolated._keys, expires
    except _Deadline:
        fields = _failed("deadline", fields)
    except BaseException:
        fields = _failed("structural", fields)
    if fields["status"] == "ready" and time.monotonic() >= deadline:
        fields = _failed("deadline", fields)
    if fields["status"] != "ready":
        # Adoption is timed; teardown restores signal state without raising.
        # Any failed/late exit after the stores must not retain the new cache.
        verifier._keys, verifier._expires = original_cache
    return fields, timings

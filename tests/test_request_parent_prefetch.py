"""Exercise the production parent-process prefetch, including wall-clock limits."""

# Imported pytest fixtures are intentionally reused as parameter names.
# ruff: noqa: F811

import dataclasses
import importlib
import json
import math
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from test_oidc_reliability import fixture, signing_key  # noqa: F401

from semantic_service import _request_prefetch as prefetch

REAL_SLEEP = time.sleep


@pytest.fixture
def client_boundary(monkeypatch, fixture):
    original_client = httpx.Client
    clients = []
    pids = []

    def transport(unused, request):
        pids.append(os.getpid())
        return fixture.transport(request)

    def client(*args, **kwargs):
        created = original_client(*args, **kwargs)
        clients.append(created)
        return created

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", transport)
    monkeypatch.setattr(httpx, "Client", client)
    if hasattr(os, "posix_spawn"):
        monkeypatch.setattr(
            os, "posix_spawn", lambda *a, **kw: pytest.fail("prefetch started a process")
        )
    return clients, pids


def run(fixture, **kwargs):
    started = time.monotonic()
    fields, timings = prefetch.prefetch(fixture.verifier, **kwargs)
    return fields, timings, time.monotonic() - started


def test_real_parent_client_is_separate_closed_and_cache_used(
    fixture,
    client_boundary,
    capfd,
):
    fields, timings, elapsed = run(fixture)
    assert fields == {"status": "ready"}
    assert client_boundary[1] == [os.getpid(), os.getpid()]
    assert len(client_boundary[0]) == 1
    assert client_boundary[0][0] is not fixture.client
    assert client_boundary[0][0].is_closed and not fixture.client.is_closed
    assert fixture.verifier._expires == fixture.now + 300
    assert elapsed < 3
    assert timings["auth.discovery"]["count"] == timings["auth.jwks"]["count"] == 1
    assert fixture.verifier.verify(fixture.token()).subject == "synthetic-principal"
    assert fixture.endpoints == ["discovery", "jwks"]
    rows = [json.loads(line) for line in capfd.readouterr().out.splitlines()]
    assert len(rows) == 1 and rows[0]["phase"] == "init"
    assert rows[0]["aws_request_id"] is None
    run(fixture)
    assert len(client_boundary[0]) == 1


def test_discovery_retry_reserves_time_for_jwks(fixture, client_boundary):
    def slow_timeout(request):
        timeout = request.extensions["timeout"]["read"]
        REAL_SLEEP(timeout)
        raise httpx.ReadTimeout("private-marker")

    fixture.steps["discovery"] = [slow_timeout]
    fixture.steps["jwks"] = [slow_timeout]
    fields, _, elapsed = run(fixture)
    assert fields["failure_kind"] == "dependency"
    assert fields["auth_failure"]["endpoint"] == "jwks"
    assert fields["metadata_recovered"] is True
    assert fixture.endpoints == ["discovery", "discovery", "jwks"]
    assert [row["read"] for _, row in fixture.calls] == [0.8, 0.35, 0.8]
    assert 1.5 < elapsed < 2.8
    assert not fixture.verifier._keys
    before = len(fixture.calls)
    fixture.verifier.verify(fixture.token())
    assert fixture.endpoints[before:] == ["discovery", "jwks"]
    assert all(values["read"] == 2 for _, values in fixture.calls[before:])


@pytest.mark.parametrize("point", ["discovery", "jwks", "stream"])
def test_absolute_deadline_interrupts_blocking_io(
    fixture,
    client_boundary,
    point,
):
    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            while True:
                REAL_SLEEP(0.01)
                yield b" "

    def blocked(request):
        left, right = socket.socketpair()
        try:
            left.recv(1)
        finally:
            left.close()
            right.close()

    if point == "stream":
        fixture.steps["jwks"] = [httpx.Response(200, stream=SlowStream())]
    else:
        fixture.steps[point] = [blocked]
    fields, _, elapsed = run(fixture, budget_seconds=0.15)
    assert fields == {"status": "failed", "failure_kind": "deadline"}
    assert 0.1 < elapsed < 0.35
    assert not fixture.verifier._keys
    assert all(client.is_closed for client in client_boundary[0])


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit, RuntimeError])
@pytest.mark.parametrize("point", ["client", "discovery", "jwks", "close"])
def test_optional_prefetch_exceptions_never_escape(
    monkeypatch,
    fixture,
    client_boundary,
    error,
    point,
):
    def fail(*a, **kw):
        raise error("private exception text")

    if point == "client":
        monkeypatch.setattr(httpx, "Client", fail)
    elif point == "close":
        original = httpx.Client

        def client(*args, **kwargs):
            result = original(*args, **kwargs)
            close = result.close

            def broken_close():
                close()
                fail()

            result.close = broken_close
            return result

        monkeypatch.setattr(httpx, "Client", client)
    else:
        fixture.steps[point] = [fail]
    fields, _, _ = run(fixture)
    expected = {"status": "failed", "failure_kind": "structural"}
    if error is RuntimeError and point in {"discovery", "jwks"}:
        expected["auth_failure"] = {
            "endpoint": point,
            "category": "unexpected",
            "reason": "unknown",
            "http_status": None,
            "cause": None,
        }
    assert fields == expected
    assert fixture.verifier._keys == {}
    assert fixture.verifier._expires == -math.inf
    assert all(client.is_closed for client in client_boundary[0])


def test_partial_jwks_is_detected_and_not_adopted(fixture, client_boundary):
    fixture.keys.append({"kid": "active-private-marker", "use": "enc"})
    fields, _, _ = run(fixture)
    assert fields["status"] == "failed"
    assert fields["failure_kind"] == "dependency"
    assert fields["jwks_validation"] == {
        "accepted": 1,
        "rejected": 1,
        "rejection_reasons": {"usage": 1},
    }
    assert "private-marker" not in json.dumps(fields)
    assert fixture.verifier._keys == {}
    fixture.keys.pop()
    fixture.verifier.verify(fixture.token())
    assert fixture.endpoints == ["discovery", "jwks", "discovery", "jwks"]


def test_larger_requested_budget_cannot_extend_three_seconds(
    monkeypatch,
    fixture,
    client_boundary,
):
    fixture.steps["discovery"] = [lambda request: REAL_SLEEP(3.3)]
    fields, _, elapsed = run(fixture, budget_seconds=30)
    assert fields["failure_kind"] == "deadline"
    assert 2.5 < elapsed < 3.2
    assert not fixture.verifier._keys


def test_close_time_is_charged_and_late_success_is_not_adopted(
    monkeypatch,
    fixture,
    client_boundary,
):
    original = httpx.Client

    def client(*args, **kwargs):
        result = original(*args, **kwargs)
        close = result.close

        def slow_close():
            close()
            REAL_SLEEP(0.4)

        result.close = slow_close
        return result

    monkeypatch.setattr(httpx, "Client", client)
    fields, _, elapsed = run(fixture, budget_seconds=0.15)
    assert fields["failure_kind"] == "deadline"
    assert elapsed < 0.35
    assert not fixture.verifier._keys


def test_logs_after_deadline_do_not_block_cleanup(
    fixture,
    client_boundary,
):
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    try:
        while True:
            try:
                os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                break
        os.set_blocking(write_fd, True)
        fixture.steps["jwks"] = [lambda request: REAL_SLEEP(0.3)]
        fields, _, elapsed = run(fixture, budget_seconds=0.15, log_fd=write_fd)
        assert fields["failure_kind"] == "deadline"
        assert elapsed < 0.35
        assert all(client.is_closed for client in client_boundary[0])
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_existing_alarm_is_preserved(fixture, client_boundary):
    previous = signal.getsignal(signal.SIGALRM)
    signal.setitimer(signal.ITIMER_REAL, 30)
    try:
        fields, _, _ = run(fixture)
        assert fields == {"status": "failed", "failure_kind": "structural"}
        assert 29 < signal.getitimer(signal.ITIMER_REAL)[0] <= 30
        assert signal.getsignal(signal.SIGALRM) is previous
        assert client_boundary[0] == []
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def test_main_thread_timer_is_restored(fixture, client_boundary):
    previous = signal.getsignal(signal.SIGALRM)
    run(fixture)
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is previous


def test_non_main_thread_skips_without_creating_client(fixture, client_boundary):
    results = []
    thread = threading.Thread(target=lambda: results.append(run(fixture)[0]))
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert results == [{"status": "failed", "failure_kind": "structural"}]
    assert client_boundary[0] == []


def test_fd_count_is_stable_after_real_socket_interruptions(fixture, client_boundary):
    fd_dir = Path("/proc/self/fd") if Path("/proc/self/fd").exists() else Path("/dev/fd")
    baseline = len(list(fd_dir.iterdir()))
    for _ in range(5):

        def blocked(request):
            left, right = socket.socketpair()
            try:
                left.recv(1)
            finally:
                left.close()
                right.close()

        fixture.steps["jwks"] = [blocked]
        fields, _, _ = run(fixture, budget_seconds=0.1)
        assert fields["failure_kind"] == "deadline"
        assert len(list(fd_dir.iterdir())) == baseline


def test_short_cache_ttl_is_valid_without_bootstrap(fixture, client_boundary):
    fixture.verifier.config = dataclasses.replace(fixture.config, cache_seconds=1)
    fields, _, _ = run(fixture)
    assert fields == {"status": "ready"}
    assert fixture.verifier._expires == fixture.now + 1


def test_native_overrun_still_closes_independent_client(
    monkeypatch,
    fixture,
    client_boundary,
):
    original = time.monotonic
    offset = [0.0]

    def late_response(request):
        offset[0] = 5
        return httpx.Response(200, json=fixture.document("jwks"))

    monkeypatch.setattr(time, "monotonic", lambda: original() + offset[0])
    fixture.steps["jwks"] = [late_response]
    fields, _, _ = run(fixture)
    assert fields["failure_kind"] == "deadline"
    assert client_boundary[0][0].is_closed
    assert fixture.verifier._keys == {}


def test_interrupted_cache_adoption_is_rolled_back(
    monkeypatch,
    fixture,
    client_boundary,
):
    from semantic_service.auth import OIDCVerifier

    original = OIDCVerifier.__setattr__

    def delayed_store(self, name, value):
        original(self, name, value)
        if self is fixture.verifier and name == "_expires" and math.isfinite(value):
            REAL_SLEEP(0.4)

    monkeypatch.setattr(OIDCVerifier, "__setattr__", delayed_store)
    fields, _, elapsed = run(fixture, budget_seconds=0.15)
    assert fields["failure_kind"] == "deadline"
    assert elapsed < 0.35
    assert fixture.verifier._keys == {} and fixture.verifier._expires == -math.inf


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_optional_boundary_keeps_real_init_ready(
    monkeypatch,
    fixture,
    client_boundary,
    capfd,
    error,
):
    from semantic_service import runtime

    def fail(*args, **kwargs):
        raise error("private-control")

    def handler(event, context):
        return {"accepted": True}

    handler.verifier = fixture.verifier
    monkeypatch.setattr(runtime, "_request", lambda: handler)
    monkeypatch.setattr(prefetch, "prefetch", fail)
    name = "semantic_service.request_runtime"
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "request-test")
    monkeypatch.setenv("_HANDLER", name + ".request_handler")
    monkeypatch.setenv("AWS_LAMBDA_INITIALIZATION_TYPE", "provisioned-concurrency")
    sys.modules.pop(name, None)
    try:
        entry = importlib.import_module(name)
        rows = [json.loads(line) for line in capfd.readouterr().out.splitlines()]
        assert rows[-1]["status"] == "ready"
        assert rows[-1]["oidc_prefetch"] == {"status": "failed", "failure_kind": "structural"}
        assert "private-control" not in json.dumps(rows)
        assert entry.request_handler({}, object()) == {"accepted": True}
    finally:
        sys.modules.pop(name, None)


def test_final_adoption_rejects_metadata_after_monotonic_deadline(
    monkeypatch,
    fixture,
    client_boundary,
):
    original_monotonic = time.monotonic
    verifier_type = type(fixture.verifier)
    original_store = verifier_type.__setattr__
    offset = [0.0]
    clock_calls = []
    nonempty_stores = []

    def clock():
        clock_calls.append(fixture.now)
        if len(clock_calls) == 3:
            # The first two calls check/fill the isolated cache; only final
            # parent adoption crosses the software deadline. The real timer
            # has not fired, so this specifically exercises its final check.
            offset[0] = 5
        return fixture.now

    def track_store(self, name, value):
        if self is fixture.verifier and name == "_keys" and value:
            nonempty_stores.append(time.monotonic())
        original_store(self, name, value)

    fixture.verifier.clock = clock
    monkeypatch.setattr(time, "monotonic", lambda: original_monotonic() + offset[0])
    monkeypatch.setattr(verifier_type, "__setattr__", track_store)
    fields, _, _ = run(fixture)
    assert len(clock_calls) == 3
    assert fields == {"status": "failed", "failure_kind": "deadline"}
    assert nonempty_stores == [], "expired metadata was briefly adopted before rollback"
    assert fixture.verifier._keys == {} and fixture.verifier._expires == -math.inf
    assert fixture.endpoints == ["discovery", "jwks"]
    assert client_boundary[0][0].is_closed


def test_final_adoption_clamps_ttl_after_parent_clock_moves_back(
    fixture,
    client_boundary,
):
    original_now = fixture.now
    clock_calls = []

    def clock():
        clock_calls.append(fixture.now)
        if len(clock_calls) == 3:
            fixture.now -= 60
        return fixture.now

    fixture.verifier.clock = clock
    fields, _, _ = run(fixture)
    assert len(clock_calls) == 3
    assert fields == {"status": "ready"}
    assert fixture.verifier._keys
    assert fixture.verifier._expires == original_now + 240
    assert fixture.verifier._expires == fixture.now + fixture.config.cache_seconds
    assert client_boundary[0][0].is_closed


def test_final_adoption_rejects_expiry_after_parent_clock_moves_forward(
    fixture,
    client_boundary,
):
    clock_calls = []

    def clock():
        clock_calls.append(fixture.now)
        if len(clock_calls) == 3:
            fixture.now += fixture.config.cache_seconds + 1
        return fixture.now

    fixture.verifier.clock = clock
    fields, _, _ = run(fixture)
    assert len(clock_calls) == 3
    assert fields == {"status": "failed", "failure_kind": "structural"}
    assert fixture.verifier._keys == {} and fixture.verifier._expires == -math.inf
    assert client_boundary[0][0].is_closed


@pytest.mark.parametrize("pending", [False, True])
def test_existing_blocked_or_pending_alarm_is_preserved(
    fixture,
    client_boundary,
    pending,
):
    previous_handler = signal.getsignal(signal.SIGALRM)
    original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        if pending:
            # The mask belongs to this thread; CDK may have other unmasked
            # threads, so a process-directed signal could terminate pytest.
            signal.pthread_kill(threading.get_ident(), signal.SIGALRM)
        assert (signal.SIGALRM in signal.sigpending()) is pending
        fields, _, _ = run(fixture)
        assert fields == {"status": "failed", "failure_kind": "structural"}
        assert signal.SIGALRM in signal.pthread_sigmask(signal.SIG_BLOCK, set())
        assert (signal.SIGALRM in signal.sigpending()) is pending
        assert signal.getsignal(signal.SIGALRM) is previous_handler
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
        assert client_boundary[0] == []
    finally:
        if pending and signal.SIGALRM in signal.sigpending():
            signal.sigwait({signal.SIGALRM})
        signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)


def test_existing_custom_alarm_handler_is_preserved(fixture, client_boundary):
    deliveries = []

    def handler(signum, frame):
        deliveries.append(signum)

    previous = signal.signal(signal.SIGALRM, handler)
    try:
        fields, _, _ = run(fixture)
        assert fields == {"status": "failed", "failure_kind": "structural"}
        assert signal.getsignal(signal.SIGALRM) is handler
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
        assert client_boundary[0] == []
        signal.pthread_kill(threading.get_ident(), signal.SIGALRM)
        assert deliveries == [signal.SIGALRM]
    finally:
        signal.signal(signal.SIGALRM, previous)


def test_alarm_teardown_restores_state_with_an_unmasked_background_thread(monkeypatch):
    original_timer = signal.setitimer
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    ready, stop = threading.Event(), threading.Event()

    def background():
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
        ready.set()
        stop.wait(1)

    def delayed_cancel(which, seconds, interval=0):
        if seconds == 0:
            # Only the main thread is masked by _alarm. Its process timer can
            # expire through the unmasked background thread during teardown.
            REAL_SLEEP(0.06)
        return original_timer(which, seconds, interval)

    thread = threading.Thread(target=background)
    thread.start()
    interrupted = False
    try:
        assert ready.wait(1)
        monkeypatch.setattr(signal, "setitimer", delayed_cancel)
        try:
            with prefetch._alarm() as arm:
                arm(time.monotonic() + 0.02)
        except prefetch._Deadline:
            interrupted = True
        handler_restored = signal.getsignal(signal.SIGALRM) is previous_handler
        mask_restored = signal.pthread_sigmask(signal.SIG_BLOCK, set()) == previous_mask
        timer_disabled = signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    finally:
        # A regression must fail the assertion without poisoning later tests.
        original_timer(signal.ITIMER_REAL, 0)
        if signal.SIGALRM in signal.sigpending():
            signal.sigwait({signal.SIGALRM})
        signal.signal(signal.SIGALRM, previous_handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        stop.set()
        thread.join(1)
    assert not interrupted
    assert handler_restored and mask_restored and timer_disabled
    assert not thread.is_alive()


def test_deadline_crossed_during_timer_teardown_rolls_back_cache(
    monkeypatch, fixture, client_boundary
):
    original_monotonic = time.monotonic
    original_timer = signal.setitimer
    offset = [0.0]
    cancellations = []

    def late_cancel(which, seconds, interval=0):
        if seconds == 0:
            cancellations.append(True)
            offset[0] = 5
        return original_timer(which, seconds, interval)

    monkeypatch.setattr(signal, "setitimer", late_cancel)
    monkeypatch.setattr(time, "monotonic", lambda: original_monotonic() + offset[0])
    fields, _, _ = run(fixture)
    assert cancellations == [True]
    assert fields == {"status": "failed", "failure_kind": "deadline"}
    assert fixture.verifier._keys == {} and fixture.verifier._expires == -math.inf
    assert fixture.endpoints == ["discovery", "jwks"]
    assert client_boundary[0][0].is_closed

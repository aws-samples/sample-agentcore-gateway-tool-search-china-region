"""Route telemetry covers rejected admission and backend attempts without request data."""

import inspect
import json
import signal
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

from semantic_service import facade, fallback
from semantic_service.facade import (
    HTTPBackend,
    Router,
    ServiceError,
    TransportUnavailable,
    enforce_deadline,
)
from semantic_service.transport import RouteIdentity

IDENTITY = RouteIdentity("gateway", "target", "Search___search_tools")
ARGS = {"query": "private query", "_auth_context": "private envelope"}
SUCCESS = {"version": 1, "ok": True, "result": {"tools": []}}


def context():
    result = IDENTITY.context("private mcp id")
    result.get_remaining_time_in_millis = lambda: 10000
    result.aws_request_id = "12345678-1234-1234-1234-123456789abc"
    return result


@pytest.mark.parametrize(
    ("event", "ctx", "status"),
    [
        ({"query": "x"}, context(), "unauthenticated"),
        ({"query": "", "_auth_context": "x"}, context(), "invalid input"),
        (ARGS, SimpleNamespace(), "unauthenticated"),
        (ARGS, IDENTITY.context("id"), "internal failure"),
    ],
)
def test_every_facade_admission_failure_emits_one_finish(event, ctx, status):
    records = []
    calls = []

    def backend(body, end):
        calls.append(body)
        return SUCCESS

    with pytest.raises(ServiceError):
        Router(IDENTITY, backend, backend, emit=records.append)(event, ctx)
    summaries = [row for row in records if row["operation"] == "search_route"]
    assert len(summaries) == 1
    assert summaries[0]["phase"] == "end"
    assert summaries[0]["status"] == status
    assert summaries[0]["backend"] == "none"
    assert not calls
    assert "private" not in json.dumps(records)


@pytest.mark.parametrize(
    "kind", ["deadline exceeded", "replay unavailable", "clock skew", "worker unavailable"]
)
def test_controlled_infrastructure_error_retries_once_with_reason(kind):
    records = []
    calls = []

    def primary(body, end):
        return {"version": 1, "ok": False, "error": kind, "retryable": True}

    def secondary(body, end):
        calls.append(body)
        return SUCCESS

    assert Router(IDENTITY, primary, secondary, emit=records.append)(ARGS, context()) == {
        "tools": []
    }
    assert len(calls) == 1
    assert records[-1]["fallback_reason"] == kind.replace(" ", "_")
    attempts = [row for row in records if row["operation"] == "search_attempt"]
    assert [(row["backend"], row["phase"]) for row in attempts] == [
        ("ecs", "start"),
        ("ecs", "end"),
        ("fallback", "start"),
        ("fallback", "end"),
    ]
    assert all("remaining_ms" in row and "timestamp_ms" in row for row in attempts)
    assert attempts[1]["duration_ms"] >= 0
    assert attempts[2]["remaining_ms"] > 0
    assert records[-1]["fallback_started"] is True


def test_completed_backend_result_does_not_trigger_fallback_after_wall_clock_jump(
    monkeypatch,
):
    from semantic_service import ecs_backend
    from semantic_service.ecs_backend import Admission, Backend

    wall = [time.time()]
    calls, secondary_calls = [], []
    monkeypatch.setattr(ecs_backend.time, "time", lambda: wall[0])

    def search(arguments, ctx):
        calls.append(arguments)
        wall[0] += 5
        return {"tools": []}

    backend = Backend(
        identity=IDENTITY,
        envelope=SimpleNamespace(),
        replay=SimpleNamespace(consume=lambda *_: None),
        search=search,
    )
    # Authentication has separate signed-envelope integration coverage. This
    # fixture isolates the Search completion/route retry boundary.
    backend.admit = lambda request, **kwargs: Admission(
        request, "private mcp id", "ecs", request["deadline_ms"], time.monotonic() + 1
    )

    def primary(body, end):
        return backend.handle(body)

    def secondary(body, end):
        secondary_calls.append(body)
        return SUCCESS

    assert Router(IDENTITY, primary, secondary)(ARGS, context()) == {"tools": []}
    assert len(calls) == 1 and not secondary_calls


def test_exhausted_budget_does_not_claim_fallback_started(monkeypatch):
    records = []
    calls = []
    clock = [100.0]
    monkeypatch.setattr(facade.time, "monotonic", lambda: clock[0])

    def primary(body, end):
        clock[0] += context().get_remaining_time_in_millis() / 1000
        raise TransportUnavailable()

    def secondary(body, end):
        calls.append(body)
        return SUCCESS

    with pytest.raises(ServiceError):
        Router(IDENTITY, primary, secondary, emit=records.append)(ARGS, context())
    assert not calls
    assert records[-1]["backend"] == "ecs"
    assert records[-1]["fallback_started"] is False
    assert records[-1]["fallback_not_started"] == "insufficient_budget"
    assert not [row for row in records if row.get("backend") == "fallback"]


def test_handler_factory_failure_has_exactly_one_finish(monkeypatch, capsys):
    def failed():
        raise ValueError("private configuration")

    monkeypatch.setattr(facade, "_router", failed)
    with pytest.raises(ServiceError):
        facade.handler(ARGS, context())
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(records) == 1
    assert records[0]["operation"] == "search_route"
    assert records[0]["status"] == "internal failure"
    assert records[0]["failure_stage"] == "factory"
    assert records[0]["cold_start"] in (True, False)
    assert "private" not in json.dumps(records)


@pytest.mark.parametrize("event", [{}, {"query": "private"}])
def test_fallback_parse_failure_has_one_finish(event, capsys):
    assert fallback.handler(event, context())["ok"] is False
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(records) == 1
    assert records[0]["operation"] == "search_fallback"
    assert records[0]["status"] == "internal failure"
    assert records[0]["aws_request_id"] == context().aws_request_id
    assert "private" not in json.dumps(records)


def test_fallback_factory_failure_has_one_finish(monkeypatch, capsys):
    from semantic_service import ecs_backend

    def failed(backend):
        raise ValueError("private configuration")

    monkeypatch.setattr(ecs_backend, "backend_factory", failed)
    body = {
        "version": 1,
        "arguments": ARGS,
        "custom": context().client_context.custom,
        "deadline_ms": int(time.time() * 1000 + 5000),
        "attempt_id": "a" * 32,
    }
    assert fallback.handler(body, context())["ok"] is False
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(records) == 1
    assert records[0]["operation"] == "search_fallback"
    assert records[0]["failure_stage"] == "factory"
    assert records[0]["attempt_id"] == "a" * 32


def test_oauth_failure_is_measured_and_never_retried():
    records = []

    class Tokens:
        def token(self, request):
            raise ValueError("private secret")

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as http:
        backend = HTTPBackend("https://search.example/search", http, Tokens(), emit=records.append)
        with pytest.raises(ServiceError):
            backend({"attempt_id": "a" * 32}, time.monotonic() + 1)
    assert [(r["operation"], r["phase"]) for r in records] == [
        ("search_oauth", "start"),
        ("search_oauth", "end"),
    ]
    assert records[-1]["status"] == "oauth_failed"
    assert records[-1]["duration_ms"] >= 0
    assert "private" not in json.dumps(records)


def test_real_timer_expiry_and_cleanup_restore_original_handler():
    original = signal.getsignal(signal.SIGALRM)
    with pytest.raises(facade.DeadlineExpired), enforce_deadline(time.monotonic() + 0.02):
        time.sleep(0.1)
    assert signal.getsignal(signal.SIGALRM) is original
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_timer_arriving_during_cleanup_does_not_discard_success(monkeypatch):
    original_handler = signal.getsignal(signal.SIGALRM)
    real_setitimer = signal.setitimer

    def setitimer(which, seconds, interval=0.0):
        if seconds == 0:
            # Model a signal delivery immediately before the timer is disarmed.
            signal.getsignal(signal.SIGALRM)(signal.SIGALRM, None)
        return real_setitimer(which, seconds, interval)

    monkeypatch.setattr(signal, "setitimer", setitimer)
    try:
        with enforce_deadline(time.monotonic() + 1):
            completed = True
        assert completed
        assert signal.getsignal(signal.SIGALRM) is original_handler
    finally:
        real_setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, original_handler)


def test_http_stage_records_connection_timing_without_trace_metadata():
    records = []

    class Tokens:
        def token(self, request):
            return "private bearer"

    def response(request):
        trace = request.extensions["trace"]
        for name in (
            "connection.connect_tcp.started",
            "connection.connect_tcp.complete",
            "connection.start_tls.started",
            "connection.start_tls.complete",
            "http11.send_request_headers.started",
        ):
            trace(name, {"private": "credentials"})
        return httpx.Response(200, json=SUCCESS)

    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        backend = HTTPBackend("https://search.example/search", http, Tokens(), emit=records.append)
        assert backend({"attempt_id": "a" * 32}, time.monotonic() + 1) == SUCCESS
    network = [r for r in records if r["operation"] == "search_transport"]
    assert len(network) == 1
    assert network[0]["phase"] == "end"
    assert network[0]["http_timing"]["connection_state"] == "new_connect_observed"
    assert network[0]["http_timing"]["body_complete"] is True
    assert network[0]["http_timing"]["tls_ms"] >= 0
    assert "private" not in json.dumps(records)


def test_factory_time_is_in_route_budget_and_cold_marker_is_per_handler(monkeypatch):
    now = [100.0]
    records = []
    calls = []
    monkeypatch.setattr(facade.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(facade, "_COLD_START", True)

    def backend(body, deadline):
        calls.append(body)
        return SUCCESS

    route = Router(IDENTITY, backend, backend, emit=records.append)

    def factory():
        now[0] += facade.ROUTE_SECONDS
        return route

    monkeypatch.setattr(facade, "_router", factory)
    for cold in (True, False):
        with pytest.raises(ServiceError):
            facade.handler(ARGS, context())
        assert records[-1]["cold_start"] is cold
        assert records[-1]["factory_ms"] == facade.ROUTE_SECONDS * 1000
    assert not calls


def test_unknown_backend_failure_finishes_attempt_as_internal_failure():
    records = []

    def failed(body, deadline):
        raise RuntimeError("private details")

    with pytest.raises(ServiceError):
        Router(IDENTITY, failed, failed, emit=records.append)(ARGS, context())
    attempts = [r for r in records if r["operation"] == "search_attempt"]
    assert attempts[-1]["status"] == "internal failure"
    assert "private" not in json.dumps(records)


def test_factory_deadline_interrupts_work_and_still_emits_finish(monkeypatch, capsys):
    completed = []
    monkeypatch.setattr(facade, "ROUTE_SECONDS", 0.03)

    def factory():
        time.sleep(0.2)
        completed.append(True)
        return Router(IDENTITY, lambda *_: SUCCESS, lambda *_: SUCCESS)

    monkeypatch.setattr(facade, "_router", factory)
    started = time.monotonic()
    with pytest.raises(ServiceError):
        facade.handler(ARGS, context())
    assert time.monotonic() - started < 0.15
    assert not completed
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(records) == 1
    assert records[0]["failure_stage"] == "factory"


def test_alarm_at_first_cleanup_line_restores_state_and_keeps_accepted_result():
    source, first = inspect.getsourcelines(enforce_deadline.__wrapped__)
    target = first + next(i for i, line in enumerate(source) if "active = False" in line)
    original_handler = signal.getsignal(signal.SIGALRM)
    previous_trace = sys.gettrace()
    fired = []
    fallback_calls = []

    def trace(frame, event, arg):
        if (
            event == "line"
            and frame.f_code.co_name == "enforce_deadline"
            and frame.f_lineno == target
            and not fired
        ):
            fired.append(True)
            signal.raise_signal(signal.SIGALRM)
        return trace

    def secondary(body, deadline):
        fallback_calls.append(body)
        return SUCCESS

    try:
        sys.settrace(trace)
        assert Router(IDENTITY, lambda *_: SUCCESS, secondary)(ARGS, context()) == {"tools": []}
        assert fired and not fallback_calls
        assert signal.getsignal(signal.SIGALRM) is original_handler
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    finally:
        sys.settrace(previous_trace)
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, original_handler)

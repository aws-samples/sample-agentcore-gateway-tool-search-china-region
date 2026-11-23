"""JWKS 事件先于补偿输出；既有认证行为与 REQUEST 汇总保持不变。"""

import builtins
import json
import math
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from test_oidc_reliability import (
    NOW,
    OIDCFixture,
    assert_attempt_timeouts,
    signing_key,  # noqa: F401
)
from test_oidc_reliability import fixture as oidc_fixture  # noqa: F401

from semantic_service import auth, runtime
from semantic_service.auth import ServiceError

PRIVATE = "private-attempt-event-fixture"
KID = f"{PRIVATE}-kid"
REQUEST_ID = "01234567-89ab-cdef-0123-456789abcdef"
OTHER_REQUEST_ID = "11234567-89ab-cdef-0123-456789abcdef"
_COMMON_EVENT_FIELDS = {
    "operation",
    "event",
    "endpoint",
    "aws_request_id",
    "attempt",
    "compensation",
    "duration_ms",
    "outcome",
    "failure",
}
EVENT_FIELDS = {
    "request": _COMMON_EVENT_FIELDS,
    "init": _COMMON_EVENT_FIELDS | {"phase"},
}


@pytest.fixture
def attempt_fixture(oidc_fixture):  # noqa: F811
    oidc_fixture.keys[0]["kid"] = KID
    return oidc_fixture


def failure(category, reason, status=None, cause=None):
    return {
        "endpoint": "jwks",
        "category": category,
        "reason": reason,
        "http_status": status,
        "cause": cause,
    }


def invoke(monkeypatch, fixture, *, token=None, expected_error=None, request_id=REQUEST_ID):
    token = token or fixture.token(KID)
    result = {"tools": []}

    def handler(event, context):
        fixture.verifier.verify(token)
        return result

    monkeypatch.setattr(runtime, "_request", lambda: handler)
    context = SimpleNamespace(aws_request_id=request_id)
    if expected_error is None:
        assert runtime.request_handler({}, context) is result
        error = None
    else:
        with pytest.raises(expected_error) as caught:
            runtime.request_handler({}, context)
        error = caught.value
        assert type(error) is expected_error
    return token, error


def read_logs(capsys, fixture, token, *, request_id=REQUEST_ID):
    captured = capsys.readouterr()
    for value in (
        PRIVATE,
        fixture.config.issuer,
        KID,
        token,
        "synthetic-principal",
        fixture.keys[0]["n"],
    ):
        assert value not in captured.out + captured.err
    rows = [json.loads(line) for line in captured.out.splitlines()]
    return split_request_rows(rows, request_id)


def split_request_rows(rows, request_id):
    """Keep one summary and reject unknown rows instead of silently filtering them."""
    assert all(row.get("operation") in {"request", "oidc_fetch"} for row in rows)
    summaries = [row for row in rows if row["operation"] == "request"]
    events = [row for row in rows if row["operation"] == "oidc_fetch"]
    assert len(summaries) == 1
    assert len(rows) == len(events) + 1
    assert rows[-1] is summaries[0]
    assert summaries[0]["aws_request_id"] == request_id
    assert_attempt_events(events, phase="request", request_id=request_id)
    assert len(events) <= 3
    return events, summaries[0]


def split_init_rows(rows):
    """Apply the same strict oracle to real Init events without adding request fields."""
    assert all(row.get("operation") in {"request_init", "oidc_fetch"} for row in rows)
    summaries = [row for row in rows if row["operation"] == "request_init"]
    events = [row for row in rows if row["operation"] == "oidc_fetch"]
    assert len(summaries) == 1
    assert len(rows) == len(events) + 1
    assert rows[-1] is summaries[0]
    assert "aws_request_id" not in summaries[0]
    assert_attempt_events(events, phase="init", request_id=None)
    assert len(events) <= 2
    return events, summaries[0]


def assert_attempt_events(events, *, phase, request_id):
    for index, event in enumerate(events, 1):
        assert set(event) == EVENT_FIELDS[phase]
        assert event["operation"] == "oidc_fetch"
        if phase == "init":
            assert event["phase"] == "init"
        assert event["event"] == "attempt_finished"
        assert event["endpoint"] == "jwks"
        assert event["aws_request_id"] == request_id
        assert event["attempt"] == index
        assert type(event["compensation"]) is bool
        assert event["outcome"] in {"ok", "failed", "interrupted"}
        assert type(event["duration_ms"]) in (float, int)
        assert math.isfinite(event["duration_ms"]) and event["duration_ms"] >= 0
        if event["outcome"] in {"ok", "interrupted"}:
            assert event["failure"] is None
        else:
            assert set(event["failure"]) == set(failure("http", "status"))
            assert event["failure"]["endpoint"] == "jwks"


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("read", failure("timeout", "read")),
        ("connect", failure("timeout", "connect")),
        ("http", failure("http", "status", 503)),
    ],
)
def test_first_failure_is_written_before_backoff_and_compensation(
    attempt_fixture, monkeypatch, capsys, kind, expected
):
    fixture = attempt_fixture
    clock = FakeClock()
    monkeypatch.setattr(auth.time, "perf_counter", clock)
    monkeypatch.setattr(auth.time, "monotonic", clock)
    emitted = []
    checkpoints = []
    original_print = builtins.print

    def capture_print(*args, **kwargs):
        emitted.append(json.loads(args[0]))
        original_print(*args, **kwargs)

    def first(request):
        clock.advance(2.0)
        if kind == "http":
            return httpx.Response(503, text=PRIVATE)
        error_type = httpx.ReadTimeout if kind == "read" else httpx.ConnectTimeout
        raise error_type(f"{PRIVATE} {fixture.config.issuer}")

    def sleep(delay):
        checkpoints.append(("backoff", list(emitted)))
        fixture.delays.append(delay)
        clock.advance(delay)

    def second(request):
        checkpoints.append(("compensation", list(emitted)))
        clock.advance(0.027)
        return httpx.Response(200, json=fixture.document("jwks"))

    monkeypatch.setattr(builtins, "print", capture_print)
    monkeypatch.setattr(auth.time, "sleep", sleep)
    fixture.steps["jwks"] = [first, second]
    token, _ = invoke(monkeypatch, fixture)
    events, summary = read_logs(capsys, fixture, token)
    assert len(events) == 2, "each completed JWKS GET must emit its own event"
    assert [row["failure"] for row in events] == [expected, None]
    assert [row["compensation"] for row in events] == [False, True]
    assert [row["outcome"] for row in events] == ["failed", "ok"]
    assert [row["duration_ms"] for row in events] == pytest.approx([2000.0, 27.0])
    assert checkpoints == [("backoff", [events[0]]), ("compensation", [events[0]])]
    assert fixture.endpoints == ["discovery", "jwks", "jwks"]
    assert fixture.delays == [0.05]
    assert_attempt_timeouts(fixture, [2.0, 2.0, 0.5])
    assert summary["status"] == "ok" and summary["auth_recovered"] is True
    assert "auth_failure" not in summary
    assert summary["stage_timings"]["auth.jwks"] == {
        "count": 2,
        "duration_ms": 2027.0,
        "max_ms": 2000.0,
        "errors": 1,
    }


@pytest.mark.parametrize("kind", ["dns", "tls_certificate", "tls", "http429", "json"])
def test_nonretryable_failure_keeps_typed_cause_and_one_attempt(
    attempt_fixture, monkeypatch, capsys, kind
):
    fixture = attempt_fixture
    if kind in {"dns", "tls_certificate"}:
        lower = (
            socket.gaierror(1, PRIVATE)
            if kind == "dns"
            else ssl.SSLCertVerificationError(1, PRIVATE)
        )
        step = httpx.ConnectError(PRIVATE)
        step.__cause__ = lower
        expected = failure("network", "connect", cause=kind)
    elif kind == "tls":
        step = httpx.ConnectTimeout(PRIVATE)
        step.__cause__ = ssl.SSLError(1, PRIVATE)
        expected = failure("timeout", "connect", cause="tls")
    elif kind == "http429":
        step = httpx.Response(429, text=PRIVATE)
        expected = failure("http", "status", 429)
    else:
        step = httpx.Response(200, content=PRIVATE.encode())
        expected = failure("document", "json_invalid", 200)
    fixture.steps["jwks"] = [step]
    token, error = invoke(monkeypatch, fixture, expected_error=ServiceError)
    events, summary = read_logs(capsys, fixture, token)
    assert len(events) == 1, "terminal JWKS failures still require an attempt event"
    assert events[0]["outcome"] == "failed"
    assert events[0]["failure"] == expected == summary["auth_failure"]
    assert events[0]["compensation"] is False
    assert error.oidc_failure is not None
    assert str(error) == "internal failure"
    assert fixture.endpoints == ["discovery", "jwks"]
    assert fixture.delays == []
    assert_attempt_timeouts(fixture, [2.0, 2.0])
    assert "auth_recovered" not in summary


def test_retry_failure_preserves_first_event_and_last_failure_summary(
    attempt_fixture, monkeypatch, capsys
):
    fixture = attempt_fixture
    fixture.steps["jwks"] = [httpx.ReadTimeout(PRIVATE), httpx.Response(503, text=PRIVATE)]
    token, _ = invoke(monkeypatch, fixture, expected_error=ServiceError)
    events, summary = read_logs(capsys, fixture, token)
    assert len(events) == 2
    assert [row["failure"] for row in events] == [
        failure("timeout", "read"),
        failure("http", "status", 503),
    ]
    assert [row["compensation"] for row in events] == [False, True]
    assert summary["auth_failure"] == events[-1]["failure"]
    assert "auth_recovered" not in summary
    assert_attempt_timeouts(fixture, [2.0, 2.0, 0.5])


def test_body_read_timeout_keeps_received_http_status(attempt_fixture, monkeypatch, capsys):
    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b"{"
            raise httpx.ReadTimeout(PRIVATE)

    fixture = attempt_fixture
    fixture.steps["jwks"] = [httpx.Response(200, stream=BrokenBody())]
    token, _ = invoke(monkeypatch, fixture)
    events, summary = read_logs(capsys, fixture, token)
    assert len(events) == 2
    assert events[0]["failure"] == failure("timeout", "read", 200)
    assert summary["auth_recovered"] is True


def test_three_jwks_attempts_distinguish_unknown_kid_refresh_from_compensation(
    attempt_fixture, monkeypatch, capsys
):
    fixture = attempt_fixture
    rotated = fixture.keys[0] | {"kid": f"{PRIVATE}-rotated"}
    fixture.steps["jwks"] = [
        httpx.Response(200, json=fixture.document("jwks")),
        httpx.ReadTimeout(PRIVATE),
        httpx.Response(200, json={"keys": [rotated]}),
    ]
    token, _ = invoke(monkeypatch, fixture, token=fixture.token(rotated["kid"]))
    events, summary = read_logs(capsys, fixture, token)
    assert len(events) == 3
    assert [row["attempt"] for row in events] == [1, 2, 3]
    assert [row["compensation"] for row in events] == [False, False, True]
    assert [row["outcome"] for row in events] == ["ok", "failed", "ok"]
    assert fixture.endpoints == ["discovery", "jwks", "jwks", "jwks"]
    assert_attempt_timeouts(fixture, [2.0, 2.0, 2.0, 0.5])
    assert summary["auth_recovered"] is True
    assert summary["stage_timings"]["auth.jwks"]["count"] == 3


def test_discovery_compensation_does_not_reset_jwks_retry_budget(
    attempt_fixture, monkeypatch, capsys
):
    fixture = attempt_fixture
    fixture.steps["discovery"] = [httpx.Response(503)]
    fixture.steps["jwks"] = [httpx.ReadTimeout(PRIVATE), httpx.Response(503)]
    token, _ = invoke(monkeypatch, fixture, expected_error=ServiceError)
    events, summary = read_logs(capsys, fixture, token)
    assert len(events) == 1
    assert events[0]["attempt"] == 1 and events[0]["compensation"] is False
    assert events[0]["failure"] == failure("timeout", "read")
    assert fixture.endpoints == ["discovery", "discovery", "jwks"]
    assert len(fixture.steps["jwks"]) == 1
    assert_attempt_timeouts(fixture, [2.0, 0.5, 2.0])
    assert "auth_recovered" not in summary


def test_successful_fetch_is_distinct_from_later_jwks_document_failure(
    attempt_fixture, monkeypatch, capsys
):
    fixture = attempt_fixture
    fixture.steps["jwks"] = [httpx.Response(200, json={"keys": []})]
    token, _ = invoke(monkeypatch, fixture, expected_error=ServiceError)
    events, summary = read_logs(capsys, fixture, token)
    assert len(events) == 1
    assert events[0]["outcome"] == "ok" and events[0]["failure"] is None
    assert summary["auth_failure"] == failure("document", "jwks_keys", 200)
    assert summary["stage_timings"]["auth.jwks"]["errors"] == 0


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_interrupted_attempt_is_not_falsely_classified_as_success_or_dependency_error(
    attempt_fixture, monkeypatch, capsys, error_type
):
    fixture = attempt_fixture
    original = error_type(PRIVATE)

    def interrupt(request):
        raise original

    fixture.steps["jwks"] = [interrupt]
    token, error = invoke(monkeypatch, fixture, expected_error=error_type)
    events, summary = read_logs(capsys, fixture, token)
    assert len(events) == 1
    assert events[0]["outcome"] == "interrupted" and events[0]["failure"] is None
    assert error is original
    assert fixture.endpoints == ["discovery", "jwks"] and fixture.delays == []
    assert "auth_failure" not in summary and "auth_recovered" not in summary


def test_first_failure_survives_interruption_during_backoff(attempt_fixture, monkeypatch, capsys):
    fixture = attempt_fixture
    fixture.steps["jwks"] = [httpx.ReadTimeout(PRIVATE)]
    original = SystemExit(PRIVATE)

    def interrupt(delay):
        raise original

    monkeypatch.setattr(auth.time, "sleep", interrupt)
    token, error = invoke(monkeypatch, fixture, expected_error=SystemExit)
    events, summary = read_logs(capsys, fixture, token)
    assert len(events) == 1
    assert events[0]["outcome"] == "failed"
    assert events[0]["failure"] == failure("timeout", "read")
    assert error is original
    assert fixture.endpoints == ["discovery", "jwks"]
    assert "auth_recovered" not in summary


@pytest.mark.parametrize("scenario", ["clean", "recovered", "terminal"])
def test_event_output_failure_cannot_replace_authentication_result(
    attempt_fixture, monkeypatch, capsys, scenario
):
    fixture = attempt_fixture
    if scenario == "recovered":
        fixture.steps["jwks"] = [httpx.ReadTimeout(PRIVATE)]
    elif scenario == "terminal":
        fixture.steps["jwks"] = [httpx.Response(429, text=PRIVATE)]
    failed_writes = []
    original_print = builtins.print

    def broken_event_output(*args, **kwargs):
        row = json.loads(args[0])
        if row.get("operation") == "oidc_fetch":
            failed_writes.append(row)
            raise OSError(PRIVATE)
        original_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", broken_event_output)
    token, _ = invoke(
        monkeypatch, fixture, expected_error=ServiceError if scenario == "terminal" else None
    )
    events, summary = read_logs(capsys, fixture, token)
    assert events == []
    assert len(failed_writes) == (2 if scenario == "recovered" else 1)
    if scenario == "terminal":
        assert summary["auth_failure"] == failure("http", "status", 429)
    else:
        assert summary["status"] == "ok" and "auth_failure" not in summary
    assert summary.get("auth_recovered", False) is (scenario == "recovered")
    assert fixture.endpoints == (
        ["discovery", "jwks", "jwks"] if scenario == "recovered" else ["discovery", "jwks"]
    )


def test_warm_hit_has_no_events_and_expired_cache_starts_a_new_attempt_sequence(
    attempt_fixture, monkeypatch, capsys
):
    fixture = attempt_fixture
    token, _ = invoke(monkeypatch, fixture)
    first, _ = read_logs(capsys, fixture, token)
    assert len(first) == 1 and first[0]["attempt"] == 1
    token, _ = invoke(monkeypatch, fixture, request_id=OTHER_REQUEST_ID)
    warm, _ = read_logs(capsys, fixture, token, request_id=OTHER_REQUEST_ID)
    assert warm == []
    assert fixture.endpoints == ["discovery", "jwks"]
    fixture.now += 301
    token, _ = invoke(
        monkeypatch,
        fixture,
        token=fixture.token(KID, iat=NOW + 301, exp=NOW + 601),
        request_id=OTHER_REQUEST_ID,
    )
    refreshed, _ = read_logs(capsys, fixture, token, request_id=OTHER_REQUEST_ID)
    assert len(refreshed) == 1 and refreshed[0]["attempt"] == 1
    assert refreshed[0]["compensation"] is False
    assert fixture.endpoints == ["discovery", "jwks", "discovery", "jwks"]


@pytest.mark.parametrize("request_id", [None, PRIVATE])
def test_request_id_is_sanitized_identically_in_event_and_summary(
    attempt_fixture, monkeypatch, capsys, request_id
):
    fixture = attempt_fixture
    token, _ = invoke(monkeypatch, fixture, request_id=request_id)
    events, _ = read_logs(capsys, fixture, token, request_id=None)
    assert len(events) == 1


@pytest.mark.parametrize("operation", [None, "search", "indexer", "enhancer"])
def test_verifier_outside_runtime_request_context_does_not_emit_attempt_events(
    attempt_fixture, capsys, operation
):
    fixture = attempt_fixture

    def action():
        return fixture.verifier.verify(fixture.token(KID))

    if operation is None:
        action()
    else:
        runtime._observed(operation, action)
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert all(row["operation"] != "oidc_fetch" for row in rows)
    assert len(rows) == (0 if operation is None else 1)
    assert fixture.endpoints == ["discovery", "jwks"]


def test_discovery_only_failure_does_not_emit_a_jwks_attempt(attempt_fixture, monkeypatch, capsys):
    fixture = attempt_fixture
    fixture.steps["discovery"] = [httpx.Response(503), httpx.Response(503)]
    token, _ = invoke(monkeypatch, fixture, expected_error=ServiceError)
    events, summary = read_logs(capsys, fixture, token)
    assert events == []
    assert summary["auth_failure"]["endpoint"] == "discovery"
    assert fixture.endpoints == ["discovery", "discovery"]


def test_event_output_time_is_not_charged_to_fetch_or_endpoint_stage(
    attempt_fixture, monkeypatch, capsys
):
    fixture = attempt_fixture
    clock = FakeClock()
    monkeypatch.setattr(auth.time, "perf_counter", clock)
    monkeypatch.setattr(auth.time, "monotonic", clock)
    original_print = builtins.print

    def slow_output(*args, **kwargs):
        row = json.loads(args[0])
        if row["operation"] == "oidc_fetch":
            clock.advance(0.750)
        original_print(*args, **kwargs)

    def fetch(request):
        clock.advance(0.100)
        return httpx.Response(200, json=fixture.document("jwks"))

    monkeypatch.setattr(builtins, "print", slow_output)
    fixture.steps["jwks"] = [fetch]
    token, _ = invoke(monkeypatch, fixture)
    events, summary = read_logs(capsys, fixture, token)
    assert len(events) == 1
    assert events[0]["duration_ms"] == 100.0
    assert summary["stage_timings"]["auth.jwks"]["duration_ms"] == 100.0
    assert summary["stage_timings"]["auth.jwks"]["max_ms"] == 100.0
    assert summary["duration_ms"] == 850.0


@pytest.mark.parametrize("phase", ["start", "finish"])
@pytest.mark.parametrize("terminal", [False, True])
def test_optional_event_clock_failure_preserves_get_and_original_result(
    attempt_fixture, monkeypatch, capsys, phase, terminal
):
    fixture = attempt_fixture
    clock_calls = []

    def broken_clock():
        clock_calls.append(True)
        if phase == "start" or len(clock_calls) == 2:
            raise OSError(PRIVATE)
        return 100.0

    # Only the optional event clock fails; existing stage clocks keep working.
    monkeypatch.setattr(
        auth, "time", SimpleNamespace(perf_counter=broken_clock, sleep=fixture.delays.append)
    )
    if terminal:
        fixture.steps["jwks"] = [httpx.Response(429, text=PRIVATE)]
    token, _ = invoke(monkeypatch, fixture, expected_error=ServiceError if terminal else None)
    events, summary = read_logs(capsys, fixture, token)
    assert events == []
    assert len(clock_calls) == (1 if phase == "start" else 2)
    assert fixture.endpoints == ["discovery", "jwks"]
    assert_attempt_timeouts(fixture, [2.0, 2.0])
    if terminal:
        assert summary["auth_failure"] == failure("http", "status", 429)
    else:
        assert summary["status"] == "ok" and "auth_failure" not in summary


def test_concurrent_runtime_requests_keep_attempt_ids_recovery_and_sink_isolated(
    attempt_fixture, monkeypatch, capsys
):
    first = attempt_fixture
    second = OIDCFixture(first.private, first.keys[0])
    barrier = Barrier(2)

    def first_fetch(request):
        barrier.wait(timeout=5)
        raise httpx.ReadTimeout(PRIVATE)

    def second_fetch(request):
        barrier.wait(timeout=5)
        return httpx.Response(200, json=second.document("jwks"))

    first.steps["jwks"] = [first_fetch]
    second.steps["jwks"] = [second_fetch]
    fixtures = {"first": first, "second": second}

    def handler(event, context):
        fixtures[event["side"]].verifier.verify(fixtures[event["side"]].token(KID))
        return {"tools": []}

    def run(side, request_id):
        fixture = fixtures[side]
        result = runtime.request_handler({"side": side}, SimpleNamespace(aws_request_id=request_id))
        # Fetch again on the same thread outside runtime scope; it must not keep the sink.
        fixture.now += 301
        fixture.verifier.verify(fixture.token(KID, iat=NOW + 301, exp=NOW + 601))
        return result

    monkeypatch.setattr(runtime, "_request", lambda: handler)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(run, "first", REQUEST_ID),
                pool.submit(run, "second", OTHER_REQUEST_ID),
            ]
            assert [future.result(timeout=10) for future in futures] == [
                {"tools": []},
                {"tools": []},
            ]
    finally:
        second.client.close()
    output = capsys.readouterr().out
    assert PRIVATE not in output
    rows = [json.loads(line) for line in output.splitlines()]
    assert {row["aws_request_id"] for row in rows} == {REQUEST_ID, OTHER_REQUEST_ID}
    first_events, first_summary = split_request_rows(
        [row for row in rows if row["aws_request_id"] == REQUEST_ID], REQUEST_ID
    )
    second_events, second_summary = split_request_rows(
        [row for row in rows if row["aws_request_id"] == OTHER_REQUEST_ID], OTHER_REQUEST_ID
    )
    assert len(first_events) == 2 and first_summary["auth_recovered"] is True
    assert len(second_events) == 1 and "auth_recovered" not in second_summary
    assert first.endpoints == ["discovery", "jwks", "jwks", "discovery", "jwks"]
    assert second.endpoints == ["discovery", "jwks", "discovery", "jwks"]


def test_nested_runtime_request_and_nonrequest_scopes_restore_outer_event_sink(
    attempt_fixture, monkeypatch, capsys
):
    outer = attempt_fixture
    inner = OIDCFixture(outer.private, outer.keys[0])
    nonrequest = OIDCFixture(outer.private, outer.keys[0])

    def handler(event, context):
        if event["side"] == "outer":
            runtime._observed("search", lambda: nonrequest.verifier.verify(nonrequest.token(KID)))
            runtime.request_handler(
                {"side": "inner"}, SimpleNamespace(aws_request_id=OTHER_REQUEST_ID)
            )
            outer.verifier.verify(outer.token(KID))
        else:
            inner.verifier.verify(inner.token(KID))
        return {"tools": []}

    monkeypatch.setattr(runtime, "_request", lambda: handler)
    try:
        assert runtime.request_handler(
            {"side": "outer"}, SimpleNamespace(aws_request_id=REQUEST_ID)
        ) == {"tools": []}
    finally:
        inner.client.close()
        nonrequest.client.close()
    output = capsys.readouterr().out
    assert PRIVATE not in output
    rows = [json.loads(line) for line in output.splitlines()]
    search_rows = [row for row in rows if row["operation"] == "search"]
    assert len(search_rows) == 1 and search_rows[0]["aws_request_id"] is None
    for request_id in (REQUEST_ID, OTHER_REQUEST_ID):
        events, summary = split_request_rows(
            [row for row in rows if row["aws_request_id"] == request_id], request_id
        )
        assert len(events) == 1 and events[0]["attempt"] == 1
        assert "auth_recovered" not in summary
    assert len(rows) == 5
    assert nonrequest.endpoints == ["discovery", "jwks"]

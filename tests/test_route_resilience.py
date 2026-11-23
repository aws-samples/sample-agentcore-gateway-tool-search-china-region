"""Adversarial routing boundaries without AWS calls or real credentials."""

import builtins
import importlib
import json
import signal
import ssl
import sys
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from semantic_service import facade
from semantic_service.catalog import OAuthTokenSource, _RefreshUnavailable
from semantic_service.route_telemetry import MeasuredSecrets, MeasuredTokenHTTP
from semantic_service.secrets import SecretsCache
from semantic_service.transport import RouteIdentity

IDENTITY = RouteIdentity("gateway", "target", "Search___search_tools")
ARGS = {"query": "private query", "_auth_context": "private envelope"}
SUCCESS = {"version": 1, "ok": True, "result": {"tools": []}}


@pytest.mark.parametrize(
    ("name", "replacement"),
    [
        ("FACADE_TIMEOUT_SECONDS", "12"),
        ("FACADE_TIMEOUT_SECONDS", "14"),
        ("FALLBACK_TIMEOUT_SECONDS", "6"),
        ("FALLBACK_TIMEOUT_SECONDS", "10"),
        ("FALLBACK_RESERVE_SECONDS", "11.0"),
        ("ECS_SECONDS", "5.0"),
        ("ROUTE_SECONDS", "13.0"),
        ("MIN_ATTEMPT_SECONDS", "5.0"),
        ("MIN_ATTEMPT_SECONDS", "0.0"),
        ("PLATFORM_RESERVE_SECONDS", "2.0"),
        ("ROUTE_SECONDS", 'float("nan")'),
        ("ECS_SECONDS", "True"),
    ],
)
def test_budget_source_mutations_fail_before_any_consumer_loads(name, replacement):
    from semantic_service import route_budget

    path = Path(route_budget.__file__)
    source = path.read_text()
    lines = source.splitlines()
    targets = [index for index, line in enumerate(lines) if line.startswith(f"{name} = ")]
    assert len(targets) == 1
    lines[targets[0]] = f"{name} = {replacement}"
    # No expected literal pin is involved: a coordinated pin update cannot
    # make internally inconsistent production constants valid.
    with pytest.raises(ValueError, match="routing budget"):
        # Fixed local test fixture source; never supplied by a request.
        exec(compile("\n".join(lines), str(path), "exec"), {})  # nosec B102


def test_consistent_budget_change_is_not_rejected_by_literal_pins():
    from semantic_service import route_budget

    replacements = {
        "ROUTE_SECONDS": "16.0",
        "ECS_SECONDS": "5.0",
        "FALLBACK_SECONDS": "11.0",
        "FALLBACK_RESERVE_SECONDS": "9.0",
        "FACADE_TIMEOUT_SECONDS": "17",
        "FALLBACK_TIMEOUT_SECONDS": "12",
    }
    source = Path(route_budget.__file__).read_text().splitlines()
    for index, line in enumerate(source):
        name = line.split(" = ", 1)[0]
        if name in replacements:
            source[index] = f"{name} = {replacements[name]}"
    namespace = {}
    # Fixed local test fixture source; never supplied by a request.
    exec(compile("\n".join(source), "<consistent-budgets>", "exec"), namespace)  # nosec B102
    assert namespace["ROUTE_SECONDS"] == 16.0


def run_oauth(token_action, *, secret_error=None, end_signal=False):
    records, fallback_calls, ecs_calls = [], [], []

    def emit(record):
        records.append(record)
        if end_signal and record["operation"] == "search_oauth" and record["phase"] == "end":
            signal.raise_signal(signal.SIGALRM)

    class Secrets:
        meta = SimpleNamespace(
            config=SimpleNamespace(
                connect_timeout=0.01,
                read_timeout=0.01,
                retries={"total_max_attempts": 1, "mode": "standard"},
            )
        )

        def get_secret_value(self, **kwargs):
            if secret_error is not None:
                raise secret_error
            return {
                "VersionId": "fixture-version",
                "SecretString": json.dumps(
                    {"client_id": "private-client", "client_secret": "private-secret"}
                ),
            }

    def respond(request):
        if request.url.host == "token.example.invalid":
            return token_action(request)
        ecs_calls.append(True)
        return httpx.Response(200, json=SUCCESS)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        tokens = OAuthTokenSource(
            SecretsCache(MeasuredSecrets(Secrets())),
            "fixture",
            "https://token.example.invalid/oauth",
            ("token.example.invalid",),
            ("search",),
            MeasuredTokenHTTP(http),
            timeout_seconds=0.5,
        )
        primary = facade.HTTPBackend("https://ecs.example.invalid/search", http, tokens, emit=emit)

        def fallback(body, deadline):
            fallback_calls.append(True)
            return SUCCESS

        ctx = IDENTITY.context("private mcp id")
        ctx.get_remaining_time_in_millis = lambda: 15000
        try:
            result = facade.Router(IDENTITY, primary, fallback, emit=records.append)(ARGS, ctx)
        except facade.ServiceError:
            result = None
    return result, records, fallback_calls, ecs_calls


@pytest.mark.parametrize("kind", ["401", "tls", "sdk", "503", "unknown", "transport"])
def test_oauth_final_log_alarm_preserves_failure_classification(kind):
    from botocore.exceptions import ClientError

    def action(request):
        if kind in {"401", "503"}:
            return httpx.Response(int(kind), json={"error": "invalid_client"})
        if kind == "tls":
            error = httpx.ConnectError("private endpoint")
            error.__cause__ = ssl.SSLCertVerificationError("private cert")
            raise error
        if kind == "transport":
            raise httpx.ConnectTimeout("private endpoint")
        raise ValueError("private unknown cause")

    error = (
        ClientError({"Error": {"Code": "AccessDeniedException", "Message": "private"}}, "Get")
        if kind == "sdk"
        else None
    )
    result, records, calls, ecs_calls = run_oauth(action, secret_error=error, end_signal=True)
    assert not ecs_calls
    if kind == "transport":
        assert result == {"tools": []} and calls == [True]
        assert records[-1]["fallback_reason"] == "connect_timeout"
    else:
        assert result is None and not calls


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ConnectError("private DNS detail"), "connect_error"),
        (httpx.ConnectTimeout("private NAT detail"), "connect_timeout"),
        (httpx.ReadTimeout("private endpoint detail"), "read_timeout"),
        (httpx.RemoteProtocolError("private connection detail"), "protocol_error"),
    ],
)
def test_real_oauth_wrapper_preserves_transport_fallback_classification(error, reason):
    def unavailable(request):
        raise error

    result, records, fallback_calls, ecs_calls = run_oauth(unavailable)
    assert result == {"tools": []} and fallback_calls == [True] and ecs_calls == []
    oauth = next(
        row for row in records if row["operation"] == "search_oauth" and row["phase"] == "end"
    )
    assert oauth["status"] == "oauth_transport_failed"
    assert oauth["transport_reason"] == reason
    assert records[-1]["fallback_reason"] == reason
    assert "private DNS detail" not in str(records)
    assert "private-secret" not in str(records)


def test_real_secret_wrapper_preserves_sdk_timeout_but_not_access_denied():
    from botocore.exceptions import ClientError, ReadTimeoutError

    result, records, calls, _ = run_oauth(
        lambda request: pytest.fail("secret timeout must precede token HTTP"),
        secret_error=ReadTimeoutError(endpoint_url="https://private.example.invalid"),
    )
    assert result == {"tools": []} and calls == [True]
    assert records[-1]["fallback_reason"] == "read_timeout"
    denied = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "private"}}, "Get")
    result, records, calls, _ = run_oauth(lambda request: pytest.fail(), secret_error=denied)
    assert result is None and not calls
    assert "private.example.invalid" not in str(records)


@pytest.mark.parametrize("snapshot_deadline", [False, True])
def test_sdk_service_rejection_cannot_reuse_earlier_transport_context(
    monkeypatch, snapshot_deadline
):
    from botocore.exceptions import ClientError

    from semantic_service import route_telemetry

    if snapshot_deadline:

        def interrupted(timings):
            raise facade.DeadlineExpired()

        monkeypatch.setattr(route_telemetry.Timings, "snapshot", interrupted)

    denied = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "private"}}, "Get")
    denied.__context__ = httpx.ConnectTimeout("private earlier transport")
    result, records, calls, ecs_calls = run_oauth(
        lambda request: pytest.fail("service denial precedes token HTTP"), secret_error=denied
    )
    assert result is None and not calls and not ecs_calls
    assert (
        next(
            row["status"]
            for row in records
            if row["operation"] == "search_oauth" and row["phase"] == "end"
        )
        == "oauth_failed"
    )


@pytest.mark.parametrize("code", [400, 401, 403])
def test_explicit_token_rejection_remains_closed_after_refresh_transport_failure(code):
    attempts = []

    def denied_then_unavailable(request):
        attempts.append(True)
        if len(attempts) == 1:
            return httpx.Response(code, json={"error": "invalid_client"})
        raise httpx.ConnectTimeout("private NAT detail")

    result, records, calls, ecs_calls = run_oauth(denied_then_unavailable)
    assert result is None and not calls and not ecs_calls
    oauth = next(
        row for row in records if row["operation"] == "search_oauth" and row["phase"] == "end"
    )
    assert oauth["status"] == "oauth_rejected"
    assert len(attempts) == (1 if code == 403 else 2)


def test_tls_cause_has_priority_over_generic_oauth_transport_failure():
    error = httpx.ConnectError("private endpoint")
    error.__cause__ = ssl.SSLCertVerificationError("private cert")

    def failed(request):
        raise error

    result, records, calls, ecs_calls = run_oauth(failed)
    assert result is None and not calls and not ecs_calls
    assert (
        next(
            row["status"]
            for row in records
            if row["operation"] == "search_oauth" and row["phase"] == "end"
        )
        == "oauth_tls_failed"
    )


def test_unknown_and_http_server_failures_do_not_claim_network_transport():
    result, records, calls, ecs_calls = run_oauth(
        lambda request: httpx.Response(503, json={"error": "unavailable"})
    )
    assert result is None and not calls and not ecs_calls
    assert (
        next(
            row["status"]
            for row in records
            if row["operation"] == "search_oauth" and row["phase"] == "end"
        )
        == "oauth_server_failed"
    )


def test_refresh_unavailable_without_explicit_rejection_can_use_fallback():
    records, calls = [], []

    class Tokens:
        def token(self, context):
            raise _RefreshUnavailable("private cache detail")

    primary = facade.HTTPBackend(
        "https://ecs.example.invalid/search", object(), Tokens(), emit=records.append
    )
    ctx = IDENTITY.context("private mcp id")
    ctx.get_remaining_time_in_millis = lambda: 15000
    result = facade.Router(
        IDENTITY, primary, lambda body, end: calls.append(True) or SUCCESS, emit=records.append
    )(ARGS, ctx)
    assert result == {"tools": []} and calls == [True]
    assert (
        next(
            row["status"]
            for row in records
            if row["operation"] == "search_oauth" and row["phase"] == "end"
        )
        == "oauth_refresh_unavailable"
    )


def test_rejection_evidence_survives_deadline_during_diagnostic_snapshot(monkeypatch):
    from semantic_service import route_telemetry

    original = route_telemetry.Timings.snapshot

    def interrupted(timings):
        if timings.oauth_rejected:
            raise facade.DeadlineExpired()
        return original(timings)

    monkeypatch.setattr(route_telemetry.Timings, "snapshot", interrupted)
    result, records, calls, _ = run_oauth(
        lambda request: httpx.Response(401, json={"error": "invalid_client"})
    )
    assert result is None and not calls
    assert (
        next(
            row["status"]
            for row in records
            if row["operation"] == "search_oauth" and row["phase"] == "end"
        )
        == "oauth_rejected"
    )


@pytest.mark.parametrize("failure", ["tls", "server"])
def test_security_evidence_survives_deadline_during_diagnostic_snapshot(monkeypatch, failure):
    from semantic_service import route_telemetry

    def interrupted(timings):
        raise facade.DeadlineExpired()

    def unavailable(request):
        if failure == "server":
            return httpx.Response(503, json={"error": "unavailable"})
        error = httpx.ConnectError("private endpoint")
        error.__cause__ = ssl.SSLCertVerificationError("private cert")
        raise error

    monkeypatch.setattr(route_telemetry.Timings, "snapshot", interrupted)
    result, records, calls, _ = run_oauth(unavailable)
    assert result is None and not calls
    assert (
        next(
            row["status"]
            for row in records
            if row["operation"] == "search_oauth" and row["phase"] == "end"
        )
        == f"oauth_{failure}_failed"
    )


def test_unknown_oauth_failure_never_falls_back():
    def failed(request):
        raise ValueError("private non-transport failure")

    result, records, calls, ecs_calls = run_oauth(failed)
    assert result is None and not calls and not ecs_calls
    assert (
        next(
            row["status"]
            for row in records
            if row["operation"] == "search_oauth" and row["phase"] == "end"
        )
        == "oauth_failed"
    )
    assert "private non-transport failure" not in str(records)


def test_successful_refresh_after_rejection_still_reaches_ecs():
    attempts = []

    def refreshed(request):
        attempts.append(True)
        if len(attempts) == 1:
            return httpx.Response(401, json={"error": "invalid_client"})
        return httpx.Response(
            200, json={"access_token": "private-token", "token_type": "Bearer", "expires_in": 300}
        )

    result, records, calls, ecs_calls = run_oauth(refreshed)
    assert result == {"tools": []} and not calls and ecs_calls == [True]
    assert (
        next(
            row["status"]
            for row in records
            if row["operation"] == "search_oauth" and row["phase"] == "end"
        )
        == "ok"
    )
    assert "private-token" not in str(records)


@pytest.mark.parametrize("kind", [KeyError, ValueError, facade.DeadlineExpired])
def test_initialization_failure_keeps_only_safe_original_exception_type(monkeypatch, kind):
    records = []

    def fail():
        raise kind("private environment value")

    monkeypatch.setattr(facade, "_build_router", fail)
    monkeypatch.setattr(facade, "emit_record", records.append)
    facade._router.cache_clear()
    sys.modules.pop("semantic_service.facade_lambda", None)
    try:
        with pytest.raises(facade.ServiceError):
            importlib.import_module("semantic_service.facade_lambda")
        assert len(records) == 1
        assert records[0]["operation"] == "search_facade_initialization"
        assert records[0]["exception_type"] == kind.__name__
        assert "private environment value" not in str(records)
        assert "search_route" not in str(records)
    finally:
        facade._router.cache_clear()
        sys.modules.pop("semantic_service.facade_lambda", None)


def test_module_import_failure_logs_only_safe_original_type(monkeypatch, capsys):
    real_import = builtins.__import__

    def failed_import(name, *args, **kwargs):
        if name == "semantic_service":
            raise ImportError("private module path")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failed_import)
    source = Path(facade.__file__).with_name("facade_lambda.py").read_text()
    with pytest.raises(RuntimeError, match="facade initialization failed"):
        # Fixed local test fixture source; never supplied by a request.
        exec(compile(source, "<entry-fixture>", "exec"), {})  # nosec B102
    output = capsys.readouterr().out
    record = json.loads(output)
    assert record["failure_stage"] == "module_import"
    assert record["exception_type"] == "ImportError"
    assert "private module path" not in output


def test_eager_route_zero_factory_means_no_invoke_factory_work():
    records = []
    route = facade.Router(
        IDENTITY,
        lambda body, deadline: SUCCESS,
        lambda body, deadline: SUCCESS,
        emit=records.append,
    )
    ctx = IDENTITY.context("fixture")
    ctx.get_remaining_time_in_millis = lambda: 15000
    assert facade.initialized_handler(route, ARGS, ctx) == {"tools": []}
    assert records[-1]["factory_ms"] == 0
    assert records[-1]["factory_ms_scope"] == "invocation"


@pytest.mark.parametrize(
    ("code", "value", "expected_error"),
    [
        (200, SUCCESS, None),
        (
            401,
            {"version": 1, "ok": False, "error": "unauthenticated", "retryable": False},
            facade.AuthenticationError,
        ),
        (
            403,
            {"version": 1, "ok": False, "error": "unauthorized", "retryable": False},
            facade.AuthorizationError,
        ),
        (
            409,
            {"version": 1, "ok": False, "error": "replay rejected", "retryable": False},
            facade.AuthenticationError,
        ),
        (200, {"version": 1, "ok": True, "result": {"tools": "invalid"}}, facade.ServiceError),
        (0, None, facade.ServiceError),
    ],
)
def test_http_completed_outcome_survives_transport_log_alarm(code, value, expected_error):
    calls, signals = [], []

    def respond(request):
        if code == 0:
            error = httpx.ConnectError("private TLS detail")
            error.__cause__ = ssl.SSLCertVerificationError("private certificate")
            raise error
        return httpx.Response(code, json=value)

    def emit(row):
        if row["operation"] == "search_transport":
            signals.append(True)
            signal.raise_signal(signal.SIGALRM)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        primary = facade.HTTPBackend(
            "https://ecs.example.invalid/search",
            client,
            SimpleNamespace(token=lambda context: "private-token"),
            emit=emit,
        )
        ctx = IDENTITY.context("fixture")
        ctx.get_remaining_time_in_millis = lambda: 15000
        route = facade.Router(
            IDENTITY, primary, lambda *_: calls.append(True) or SUCCESS, emit=lambda row: None
        )
        if expected_error is None:
            assert route(ARGS, ctx) == {"tools": []}
        else:
            with pytest.raises(expected_error):
                route(ARGS, ctx)
    assert signals == [True] and calls == []


@pytest.mark.parametrize("valid", [False, True])
def test_attempt_cleanup_alarm_preserves_only_valid_success_or_known_denial(monkeypatch, valid):
    original = facade.enforce_deadline

    @contextmanager
    def cleanup_alarm(deadline):
        with original(deadline):
            try:
                yield
            finally:
                signal.raise_signal(signal.SIGALRM)

    monkeypatch.setattr(facade, "enforce_deadline", cleanup_alarm)
    value = (
        SUCCESS
        if valid
        else {"version": 1, "ok": False, "error": "unauthorized", "retryable": False}
    )
    calls = []
    ctx = IDENTITY.context("fixture")
    ctx.get_remaining_time_in_millis = lambda: 15000
    route = facade.Router(
        IDENTITY, lambda *_: value, lambda *_: calls.append(True) or SUCCESS, emit=lambda row: None
    )
    if valid:
        assert route(ARGS, ctx) == {"tools": []}
    else:
        with pytest.raises(facade.AuthorizationError):
            route(ARGS, ctx)
    assert calls == []


def test_lambda_rejection_survives_ownership_cleanup_alarm(monkeypatch):
    from semantic_service import invoke_ownership

    denied = {"version": 1, "ok": False, "error": "unauthorized", "retryable": False}
    client = SimpleNamespace(
        invoke=lambda **kwargs: {"StatusCode": 200, "Payload": BytesIO(json.dumps(denied).encode())}
    )
    backend = facade.LambdaBackend("offline-fixture", client)
    original = invoke_ownership._Resources.close

    def alarm(resources):
        signal.raise_signal(signal.SIGALRM)
        original(resources)

    monkeypatch.setattr(invoke_ownership._Resources, "close", alarm)
    ctx = IDENTITY.context("fixture")
    ctx.get_remaining_time_in_millis = lambda: 15000

    def primary(*args):
        raise facade.TransportUnavailable()

    route = facade.Router(IDENTITY, primary, backend, emit=lambda row: None)
    with pytest.raises(facade.AuthorizationError):
        route(ARGS, ctx)


@pytest.mark.parametrize("valid", [False, True])
def test_http_outcome_survives_response_context_close_alarm(monkeypatch, valid):
    value = (
        SUCCESS
        if valid
        else {"version": 1, "ok": False, "error": "unauthorized", "retryable": False}
    )
    signals, calls = [], []
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200 if valid else 403, json=value)
        )
    ) as client:
        original = client.stream

        @contextmanager
        def interrupted(*args, **kwargs):
            with original(*args, **kwargs) as response:
                try:
                    yield response
                finally:
                    signals.append(True)
                    signal.raise_signal(signal.SIGALRM)

        monkeypatch.setattr(client, "stream", interrupted)
        backend = facade.HTTPBackend(
            "https://ecs.example.invalid/search",
            client,
            SimpleNamespace(token=lambda context: "fixture"),
        )
        ctx = IDENTITY.context("fixture")
        ctx.get_remaining_time_in_millis = lambda: 15000
        route = facade.Router(
            IDENTITY, backend, lambda *_: calls.append(True) or SUCCESS, emit=lambda row: None
        )
        if valid:
            assert route(ARGS, ctx) == {"tools": []}
        else:
            with pytest.raises(facade.AuthorizationError):
                route(ARGS, ctx)
    assert signals == [True] and not calls


@pytest.mark.parametrize("code", [200, 400, 401, 403, 409, 429])
def test_header_rejection_survives_alarm_before_body_completion(code):
    calls, signals = [], []

    class Partial(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"version":1,'
            signals.append(True)
            signal.raise_signal(signal.SIGALRM)
            pytest.fail("deadline must stop reading partial body")

    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(code, stream=Partial()))
    ) as client:
        backend = facade.HTTPBackend(
            "https://ecs.example.invalid/search",
            client,
            SimpleNamespace(token=lambda context: "fixture"),
        )
        ctx = IDENTITY.context("fixture")
        ctx.get_remaining_time_in_millis = lambda: 15000
        route = facade.Router(
            IDENTITY, backend, lambda *_: calls.append(True) or SUCCESS, emit=lambda row: None
        )
        if code == 200:
            assert route(ARGS, ctx) == {"tools": []}
            assert calls == [True]  # An unvalidated partial body is no completed result.
        else:
            with pytest.raises(facade.ServiceError):
                route(ARGS, ctx)
            assert not calls
    assert signals == [True]


def test_controlled_capacity_response_keeps_transport_reason():
    value = {"version": 1, "ok": False, "error": "capacity", "retryable": True}
    rows = []
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503, json=value))
    ) as client:
        backend = facade.HTTPBackend(
            "https://ecs.example.invalid/search",
            client,
            SimpleNamespace(token=lambda context: "fixture"),
            emit=rows.append,
        )
        ctx = IDENTITY.context("fixture")
        ctx.get_remaining_time_in_millis = lambda: 15000
        route = facade.Router(IDENTITY, backend, lambda *_: SUCCESS, emit=rows.append)
        assert route(ARGS, ctx) == {"tools": []}
    assert (
        next(row["status"] for row in rows if row["operation"] == "search_transport") == "capacity"
    )
    assert rows[-1]["fallback_reason"] == "capacity"

"""Regressions for cold fallback budget and invocation ownership."""

import importlib
import json
import subprocess  # nosec B404
import sys
import time

import pytest

from semantic_service import facade, route_budget
from semantic_service.transport import RouteIdentity

IDENTITY = RouteIdentity("gateway", "target", "Search___search_tools")
ARGS = {"query": "private query", "_auth_context": "private envelope"}
SUCCESS = {"version": 1, "ok": True, "result": {"tools": []}}


def context(remaining_ms=15000):
    result = IDENTITY.context("private mcp id")
    result.get_remaining_time_in_millis = lambda: remaining_ms
    result.aws_request_id = "12345678-1234-1234-1234-123456789abc"
    return result


def test_shared_budget_keeps_explicit_cold_reserve_and_platform_headroom():
    assert route_budget.ROUTE_SECONDS == 14
    assert route_budget.ECS_SECONDS == 4
    assert route_budget.FALLBACK_RESERVE_SECONDS == 8
    assert route_budget.FALLBACK_SECONDS == 10
    assert route_budget.FACADE_TIMEOUT_SECONDS == 15
    assert route_budget.FALLBACK_TIMEOUT_SECONDS == 11
    assert route_budget.FALLBACK_RESERVE_SECONDS > 4.12342


@pytest.mark.parametrize("factory_seconds", [0.0, 4.902161, 5.159504])
def test_real_cold_factory_cost_keeps_eight_seconds_for_cold_fallback(monkeypatch, factory_seconds):
    clock = [100.0 + factory_seconds]
    monkeypatch.setattr(facade.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(facade.time, "time", lambda: 1000.0 + clock[0] - 100.0)
    primary_deadlines = []
    fallback_windows = []
    records = []

    def primary(body, end):
        primary_deadlines.append((clock[0], end, body["deadline_ms"]))
        clock[0] = end
        raise facade.TransportUnavailable("read_timeout")

    def secondary(body, end):
        fallback_windows.append(end - clock[0])
        # Maximum observed Search Init + REPORT, not client end-to-end latency.
        clock[0] += 4.12342
        return SUCCESS

    assert facade.Router(IDENTITY, primary, secondary, emit=records.append)(
        ARGS,
        context((15 - factory_seconds) * 1000),
        cold_start=factory_seconds > 0,
        factory_ms=factory_seconds * 1000,
        invocation_started=100.0,
    ) == {"tools": []}
    assert len(primary_deadlines) == len(fallback_windows) == 1
    assert 0 < primary_deadlines[0][1] - primary_deadlines[0][0] <= 4
    assert primary_deadlines[0][2] == pytest.approx(
        (1000 + primary_deadlines[0][1] - 100) * 1000, abs=1
    )
    assert fallback_windows[0] >= 8
    assert records[-1]["fallback_started"] is True
    assert records[-1]["fallback_reserve_satisfied"] is True


def test_short_platform_budget_skips_ecs_and_reports_partial_fallback_reserve():
    records = []
    calls = []

    def primary(body, end):
        pytest.fail("ECS must not consume the remaining fallback window")

    def secondary(body, end):
        calls.append(end - time.monotonic())
        return SUCCESS

    assert facade.Router(IDENTITY, primary, secondary, emit=records.append)(
        ARGS, context(3500)
    ) == {"tools": []}
    assert len(calls) == 1 and 3 <= calls[0] < 3.5
    assert records[-1]["ecs_attempt_id"] is None
    assert records[-1]["primary_not_started"] == "fallback_reserve"
    assert records[-1]["fallback_reserve_satisfied"] is False


def test_real_watchdog_stops_primary_and_reaches_reserved_fallback(monkeypatch):
    monkeypatch.setattr(facade, "ROUTE_SECONDS", 0.4)
    monkeypatch.setattr(facade, "ECS_SECONDS", 0.1)
    monkeypatch.setattr(facade, "FALLBACK_RESERVE_SECONDS", 0.2, raising=False)
    monkeypatch.setattr(facade, "FALLBACK_SECONDS", 0.3, raising=False)
    monkeypatch.setattr(facade, "MIN_ATTEMPT_SECONDS", 0.02)
    calls = []

    def primary(body, end):
        time.sleep(2)
        pytest.fail("real signal deadline did not interrupt primary")

    def secondary(body, end):
        calls.append(end - time.monotonic())
        return SUCCESS

    started = time.monotonic()
    assert facade.Router(IDENTITY, primary, secondary, emit=lambda row: None)(
        ARGS, context(15000)
    ) == {"tools": []}
    assert time.monotonic() - started < 0.5
    assert calls[0] >= 0.19


@pytest.mark.parametrize("alarm", [False, True])
def test_oauth_deadline_is_budget_failure_and_can_fallback(monkeypatch, alarm):
    records = []

    class Tokens:
        def token(self, request):
            if alarm:
                raise facade.DeadlineExpired()
            from semantic_service.catalog import _CatalogDeadlineExceeded

            raise _CatalogDeadlineExceeded("private dependency text")

    class HTTP:
        def stream(self, *args, **kwargs):
            pytest.fail("transport must not start without token")

    backend = facade.HTTPBackend(
        "https://private.example.invalid/search", HTTP(), Tokens(), emit=records.append
    )
    route_records = []
    assert facade.Router(IDENTITY, backend, lambda body, end: SUCCESS, emit=route_records.append)(
        ARGS, context()
    ) == {"tools": []}
    assert [row["status"] for row in records if row["phase"] == "end"] == ["deadline_exceeded"]
    assert route_records[-1]["fallback_reason"] == "deadline_exceeded"
    assert route_records[-1]["fallback_started"] is True
    assert "private dependency text" not in str(records)


def test_token_returning_after_its_deadline_never_starts_ecs_http(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(facade.time, "monotonic", lambda: clock[0])
    records = []

    class Tokens:
        def token(self, request):
            clock[0] = request.deadline + 0.001
            return "private token"

    class HTTP:
        def stream(self, *args, **kwargs):
            pytest.fail("expired OAuth work must not start ECS HTTP")

    backend = facade.HTTPBackend(
        "https://private.example.invalid/search", HTTP(), Tokens(), emit=records.append
    )
    assert facade.Router(IDENTITY, backend, lambda body, end: SUCCESS, emit=lambda row: None)(
        ARGS, context()
    ) == {"tools": []}
    assert records[-1]["status"] == "deadline_exceeded"


def test_fallback_clamps_work_to_its_own_shared_budget(monkeypatch):
    from semantic_service import ecs_backend, fallback

    captured = []

    class Worker:
        def handle(self, request, **kwargs):
            captured.append(request)
            return SUCCESS

    monkeypatch.setattr(ecs_backend, "backend_factory", lambda name: Worker())
    monkeypatch.setattr(fallback, "emit_record", lambda row: None)
    started = time.time()
    request = {
        "version": 1,
        "arguments": ARGS,
        "custom": IDENTITY.context("private mcp id").client_context.custom,
        "deadline_ms": int((started + 30) * 1000),
        "attempt_id": "a" * 32,
    }
    assert fallback.handler(request, context(20000)) == SUCCESS
    assert captured[0]["deadline_ms"] <= (started + 10.01) * 1000


def test_eager_entry_constructs_once_before_invocation_and_logs_init(monkeypatch):
    calls, records = [], []

    class ReadyRouter:
        initialization_timings = {"runtime.aws_session": {"count": 1, "duration_ms": 2}}

        def __call__(self, event, ctx, **kwargs):
            calls.append(("invoke", kwargs))
            return {"tools": []}

    ready = ReadyRouter()
    monkeypatch.setattr(facade, "_router", lambda: calls.append(("init", {})) or ready)
    monkeypatch.setattr(facade, "emit_record", records.append)
    monkeypatch.setattr(facade, "_COLD_START", True)
    sys.modules.pop("semantic_service.facade_lambda", None)
    try:
        entry = importlib.import_module("semantic_service.facade_lambda")
        assert [kind for kind, _ in calls] == ["init"]
        assert records[-1]["operation"] == "search_facade_initialization"
        assert records[-1]["status"] == "ok"
        assert records[-1]["stage_timings"]["runtime.aws_session"]["count"] == 1
        assert entry.handler(ARGS, context()) == {"tools": []}
        assert entry.handler(ARGS, context()) == {"tools": []}
        assert [kind for kind, _ in calls] == ["init", "invoke", "invoke"]
        assert calls[1][1]["factory_ms"] == 0
        assert calls[1][1]["cold_start"] is True
        assert calls[2][1]["cold_start"] is False
    finally:
        sys.modules.pop("semantic_service.facade_lambda", None)


def test_init_failure_is_not_a_fabricated_route_finish(monkeypatch):
    records = []

    def failed():
        raise ValueError("private secret material")

    monkeypatch.setattr(facade, "_router", failed)
    monkeypatch.setattr(facade, "emit_record", records.append)
    sys.modules.pop("semantic_service.facade_lambda", None)
    try:
        with pytest.raises(facade.ServiceError):
            importlib.import_module("semantic_service.facade_lambda")
        assert len(records) == 1
        assert records[0]["operation"] == "search_facade_initialization"
        assert records[0]["status"] == "failed"
        assert "private secret material" not in str(records)
        assert "search_route" not in str(records)
    finally:
        sys.modules.pop("semantic_service.facade_lambda", None)


def test_fallback_import_does_not_construct_facade_or_read_its_environment(monkeypatch):
    from semantic_service import fallback

    monkeypatch.setattr(
        facade, "_router", lambda: pytest.fail("fallback import constructed facade")
    )
    monkeypatch.delenv("SEMSEARCH_ECS_URL", raising=False)
    monkeypatch.delenv("SEMSEARCH_ECS_OAUTH_SECRET_ID", raising=False)
    importlib.reload(fallback)
    assert "semantic_service.facade_lambda" not in sys.modules


def configured_factory(monkeypatch, *, fail_session=False):
    import httpx
    from botocore import session

    calls, requests = [], []
    values = {
        "SEMSEARCH_REGION": "cn-north-1",
        "SEMSEARCH_ACCOUNT": "123456789012",
        "SEMSEARCH_FALLBACK_ALIAS_ARN": (
            "arn:aws-cn:lambda:cn-north-1:123456789012:function:example:live"
        ),
        "SEMSEARCH_ECS_OAUTH_SECRET_ID": (
            "arn:aws-cn:secretsmanager:cn-north-1:123456789012:secret:example-AbCdEf"
        ),
        "OIDC_TOKEN_ENDPOINT": "https://token.example.invalid/oauth",
        "OIDC_ALLOWED_HOSTS": "token.example.invalid",
        "OIDC_MACHINE_SCOPES": "search",
        "SEMSEARCH_ECS_URL": "https://private.example.invalid/search",
        "SEMSEARCH_GATEWAY_ID": IDENTITY.gateway_id,
        "SEMSEARCH_SEARCH_TARGET_ID": IDENTITY.target_id,
        "SEMSEARCH_SEARCH_TOOL_NAME": IDENTITY.tool_name,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    class Secrets:
        def get_secret_value(self, **kwargs):
            calls.append("secret_read")
            return {
                "VersionId": "test-version",
                "SecretString": json.dumps(
                    {"client_id": "private client", "client_secret": "private secret"}
                ),
            }

    class Session:
        def __init__(self):
            calls.append("session")
            if fail_session:
                raise ValueError("private session error")

        def set_config_variable(self, name, value):
            assert (name, value) == ("region", "cn-north-1")
            calls.append("region")

        def get_credentials(self):
            calls.append("credential_chain")
            return object()

        def get_available_partitions(self):
            calls.append("endpoints")
            return ["aws-cn"]

        def get_component(self, name):
            assert name == "data_loader"
            calls.append("loader")
            return self

        def list_available_services(self, model_type):
            calls.append(model_type)
            return ["lambda", "secretsmanager"]

        def create_client(self, name, **kwargs):
            calls.append(name)
            return Secrets() if name == "secretsmanager" else object()

    def respond(request):
        requests.append(request.url.host)
        if request.url.host == "token.example.invalid":
            return httpx.Response(
                200,
                json={"access_token": "private-token", "token_type": "Bearer", "expires_in": 300},
            )
        return httpx.Response(200, json=SUCCESS)

    http = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(session, "Session", Session)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: http)
    facade._router.cache_clear()
    return calls, requests, http


def test_initialization_is_segmented_and_secret_token_stay_inside_ecs_attempt(monkeypatch):
    calls, requests, http = configured_factory(monkeypatch)
    records = []
    monkeypatch.setattr(facade, "emit_record", records.append)
    try:
        route = facade._router()
        assert calls == [
            "session",
            "region",
            "credential_chain",
            "endpoints",
            "loader",
            "service-2",
            "endpoint-rule-set-1",
            "secretsmanager",
            "lambda",
        ]
        assert requests == []
        assert set(route.initialization_timings) >= {
            "runtime.import",
            "handler.factory",
            "runtime.aws_client",
            "runtime.aws_session",
            "runtime.aws_client.secretsmanager",
            "runtime.aws_client.lambda",
            "runtime.http_client",
            "runtime.oauth_source",
        }
        assert route.initialization_timings["runtime.aws_client"]["count"] == 2
        assert route.initialization_timings["handler.factory"]["count"] == 1
        assert route(ARGS, context()) == {"tools": []}
        oauth = next(
            row for row in records if row["operation"] == "search_oauth" and row["phase"] == "end"
        )
        assert oauth["stage_timings"]["aws.secrets"]["count"] == 1
        assert oauth["stage_timings"]["oauth.token_exchange"]["count"] == 1
        assert requests == ["token.example.invalid", "private.example.invalid"]
        assert "private secret" not in str(records)
        assert "private-token" not in str(records)
    finally:
        http.close()
        facade._router.cache_clear()


def test_failed_initialization_keeps_safe_failed_stage_timing(monkeypatch):
    calls, _, http = configured_factory(monkeypatch, fail_session=True)
    records = []
    monkeypatch.setattr(facade, "emit_record", records.append)
    sys.modules.pop("semantic_service.facade_lambda", None)
    try:
        with pytest.raises(facade.ServiceError):
            importlib.import_module("semantic_service.facade_lambda")
        assert calls == ["session"]
        stage = records[-1]["stage_timings"]["runtime.aws_session"]
        assert stage["count"] == stage["errors"] == 1
        assert "private session error" not in str(records)
    finally:
        http.close()
        facade._router.cache_clear()
        sys.modules.pop("semantic_service.facade_lambda", None)


def test_timing_helper_does_not_import_httpx_before_measured_import_stage():
    # Fixed local test program and fixture paths; argv is not request-controlled.
    # nosemgrep: dangerous-subprocess-use-audit
    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-c",
            # Adjacent literals intentionally form one code snippet or ARN value.
            # nosemgrep: string-concat-in-list
            "import sys; sys.path.insert(0, 'src'); import semantic_service.route_telemetry; "
            "assert 'httpx' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_fallback_hard_deadline_includes_cold_factory_and_finishes_log(monkeypatch):
    from semantic_service import ecs_backend, fallback

    records = []

    def slow_factory(name):
        time.sleep(0.3)
        pytest.fail("fallback factory continued beyond its assigned deadline")

    monkeypatch.setattr(ecs_backend, "backend_factory", slow_factory)
    monkeypatch.setattr(fallback, "FALLBACK_SECONDS", 0.05)
    monkeypatch.setattr(fallback, "emit_record", records.append)
    request = {
        "version": 1,
        "arguments": ARGS,
        "custom": IDENTITY.context("private mcp id").client_context.custom,
        "deadline_ms": int((time.time() + 3) * 1000),
        "attempt_id": "b" * 32,
    }
    started = time.monotonic()
    assert fallback.handler(request, context()) == {
        "version": 1,
        "ok": False,
        "error": "deadline exceeded",
        "retryable": True,
    }
    assert time.monotonic() - started < 0.2
    assert len(records) == 1
    assert records[0]["status"] == "deadline exceeded"
    assert records[0]["failure_stage"] == "factory"

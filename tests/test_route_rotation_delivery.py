"""Real OAuth rotation and bounded fallback diagnostics without AWS access."""

import json
import signal
import time
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from botocore.exceptions import ClientError, HTTPClientError

from semantic_service import facade, route_budget
from semantic_service.catalog import OAuthTokenSource
from semantic_service.route_telemetry import MeasuredSecrets, MeasuredTokenHTTP
from semantic_service.secrets import SecretsCache
from semantic_service.transport import RouteIdentity

IDENTITY = RouteIdentity("gateway", "target", "Search___search_tools")
ARGS = {"query": "private query", "_auth_context": "private envelope"}
SUCCESS = {"version": 1, "ok": True, "result": {"tools": []}}


def context():
    ctx = IDENTITY.context("fixture")
    ctx.get_remaining_time_in_millis = lambda: 15000
    return ctx


def run_oauth(*, token_action=None, secret_error=None, clock=None, final_alarm=False):
    records, fallback_calls, ecs_calls, token_calls, secret_calls = [], [], [], [], []

    class Secrets:
        meta = SimpleNamespace(
            config=SimpleNamespace(
                connect_timeout=0.5,
                read_timeout=1.0,
                retries={"total_max_attempts": 1, "mode": "standard"},
            )
        )

        def get_secret_value(self, **kwargs):
            secret_calls.append(kwargs)
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
            token_calls.append(request)
            if token_action is None:
                pytest.fail("secret failure must precede token HTTP")
            return token_action(request)
        ecs_calls.append(True)
        return httpx.Response(200, json=SUCCESS)

    def emit(row):
        records.append(row)
        if final_alarm and row["operation"] == "search_oauth" and row["phase"] == "end":
            signal.raise_signal(signal.SIGALRM)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        cache = SecretsCache(MeasuredSecrets(Secrets()), clock=clock or time.monotonic)
        if secret_error is None:
            # The token call must use a cached credential that the IdP rejects.
            cache.get("fixture")
        tokens = OAuthTokenSource(
            cache,
            "fixture",
            "https://token.example.invalid/oauth",
            ("token.example.invalid",),
            ("search",),
            MeasuredTokenHTTP(http),
            timeout_seconds=0.5,
        )
        primary = facade.HTTPBackend("https://ecs.example.invalid/search", http, tokens, emit=emit)
        route = facade.Router(
            IDENTITY,
            primary,
            lambda *_: fallback_calls.append(True) or SUCCESS,
            emit=records.append,
        )
        try:
            result = route(ARGS, context())
        except facade.ServiceError:
            result = None
    return result, records, fallback_calls, ecs_calls, token_calls, secret_calls


@pytest.mark.parametrize("final_alarm", [False, True])
def test_cached_credential_rotation_budget_failure_reaches_fallback(monkeypatch, final_alarm):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def stale_credentials(request):
        # Leaves 1.8 s for SecretsCache's actual 0.5 + 1.0 + 0.5 s refresh
        # floor. A second token exchange never starts; this is not a second
        # explicit IdP denial.
        now[0] += 2.2
        return httpx.Response(401, json={"error": "invalid_client"})

    result, records, fallback_calls, ecs_calls, token_calls, secret_calls = run_oauth(
        token_action=stale_credentials, clock=lambda: now[0], final_alarm=final_alarm
    )
    assert result == {"tools": []} and fallback_calls == [True]
    assert not ecs_calls and len(token_calls) == len(secret_calls) == 1
    oauth = next(
        row for row in records if row["operation"] == "search_oauth" and row["phase"] == "end"
    )
    assert oauth["status"] == "oauth_refresh_unavailable"
    assert records[-1]["fallback_reason"] == "transport_unavailable"
    assert "private-secret" not in str(records)


@pytest.mark.parametrize("code", [400, 401, 403])
def test_completed_second_idp_rejection_stays_closed(code):
    responses = [401, code]
    result, records, calls, ecs_calls, token_calls, secret_calls = run_oauth(
        token_action=lambda request: httpx.Response(
            responses.pop(0), json={"error": "invalid_client"}
        )
    )
    assert result is None and not calls and not ecs_calls
    assert len(token_calls) == len(secret_calls) == 2
    assert records[-1]["fallback_started"] is False


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("ThrottlingException", 429),
        ("ThrottlingException", 400),
        ("ThrottlingException", None),
        ("InternalServiceError", 500),
        ("InternalServiceError", None),
        ("ServiceUnavailableException", 503),
        ("UnexpectedInfrastructureCode", 502),
    ],
)
@pytest.mark.parametrize("final_alarm", [False, True])
def test_secrets_service_transient_failure_reaches_iam_fallback(code, status, final_alarm):
    response = {"Error": {"Code": code, "Message": "private service error"}}
    if status is not None:
        response["ResponseMetadata"] = {"HTTPStatusCode": status}
    result, records, calls, ecs_calls, token_calls, _ = run_oauth(
        secret_error=ClientError(response, "GetSecretValue"), final_alarm=final_alarm
    )
    assert result == {"tools": []} and calls == [True]
    assert not token_calls and not ecs_calls
    oauth = next(
        row for row in records if row["operation"] == "search_oauth" and row["phase"] == "end"
    )
    assert oauth["status"] == "oauth_secret_unavailable"
    assert records[-1]["fallback_reason"] == "transport_unavailable"
    assert "private service error" not in str(records)


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("AccessDeniedException", 403),
        ("AccessDeniedException", 503),
        ("DecryptionFailure", 500),
        ("InvalidRequestException", 400),
        ("ResourceNotFoundException", 404),
        ("UnexpectedInfrastructureCode", 400),
    ],
)
def test_secrets_permission_and_configuration_failures_stay_closed(code, status):
    result, records, calls, ecs_calls, token_calls, _ = run_oauth(
        secret_error=ClientError(
            {
                "Error": {"Code": code, "Message": "private service denial"},
                "ResponseMetadata": {"HTTPStatusCode": status},
            },
            "GetSecretValue",
        )
    )
    assert result is None and not calls and not ecs_calls and not token_calls
    assert records[-1]["fallback_started"] is False


def test_generic_sdk_http_transport_wrapper_reaches_fallback():
    result, records, calls, ecs_calls, token_calls, _ = run_oauth(
        secret_error=HTTPClientError(error="private urllib3 transport")
    )
    assert result == {"tools": []} and calls == [True]
    assert not ecs_calls and not token_calls
    oauth = next(
        row for row in records if row["operation"] == "search_oauth" and row["phase"] == "end"
    )
    assert oauth["status"] == "oauth_transport_failed"
    assert "private urllib3 transport" not in str(records)


def test_sdk_tls_wrapper_is_never_generic_transport_fallback():
    from botocore.exceptions import SSLError

    result, records, calls, ecs_calls, token_calls, _ = run_oauth(
        secret_error=SSLError(endpoint_url="private endpoint", error="private TLS error")
    )
    assert result is None and not calls and not ecs_calls and not token_calls
    oauth = next(
        row for row in records if row["operation"] == "search_oauth" and row["phase"] == "end"
    )
    assert oauth["status"] == "oauth_tls_failed"
    assert "private TLS error" not in str(records)


@pytest.mark.parametrize(
    ("name", "replacement"),
    [
        ("CLOCK_SKEW_SECONDS", "3600.0"),
        ("CLOCK_SKEW_SECONDS", "1.001"),
        ("FALLBACK_TIMEOUT_SECONDS", "600"),
        ("FALLBACK_TIMEOUT_SECONDS", "FACADE_TIMEOUT_SECONDS"),
        ("MIN_ATTEMPT_SECONDS", "ECS_SECONDS"),
        ("RESULT_DELIVERY_SECONDS", "0.0"),
        ("RESULT_DELIVERY_SECONDS", "float('inf')"),
        ("RESULT_DELIVERY_SECONDS", "ECS_SECONDS - MIN_ATTEMPT_SECONDS"),
    ],
)
def test_delivery_and_runtime_budget_relationships_reject_invalid_values(name, replacement):
    lines = Path(route_budget.__file__).read_text().splitlines()
    matching = [i for i, line in enumerate(lines) if line.startswith(f"{name} = ")]
    assert len(matching) == 1
    lines[matching[0]] = f"{name} = {replacement}"
    with pytest.raises(ValueError, match="routing budget"):
        # Fixed local test fixture source; never supplied by a request.
        # Only repository source with fixed test substitutions is executed locally.
        # nosemgrep: exec-detected
        exec(compile("\n".join(lines), "<invalid-budget-fixture>", "exec"), {})  # nosec B102


@pytest.mark.parametrize("cleanup", ["ownership", "attempt"])
def test_validated_fallback_capacity_reason_survives_cleanup_alarm(monkeypatch, cleanup):
    from semantic_service import invoke_ownership

    denied = {"version": 1, "ok": False, "error": "capacity", "retryable": True}
    client = SimpleNamespace(
        invoke=lambda **kwargs: {"StatusCode": 200, "Payload": BytesIO(json.dumps(denied).encode())}
    )
    backend = facade.LambdaBackend("offline-fixture", client)
    if cleanup == "ownership":
        original = invoke_ownership._Resources.close

        def alarm(resources):
            signal.raise_signal(signal.SIGALRM)
            original(resources)

        monkeypatch.setattr(invoke_ownership._Resources, "close", alarm)
    else:
        original_deadline = facade.enforce_deadline

        @contextmanager
        def interrupted(deadline):
            with original_deadline(deadline):
                try:
                    yield
                finally:
                    signal.raise_signal(signal.SIGALRM)

        monkeypatch.setattr(facade, "enforce_deadline", interrupted)

    records = []
    route = facade.Router(IDENTITY, lambda *_: SUCCESS, backend, emit=records.append)
    with pytest.raises(facade.TransportUnavailable) as failure:
        route._attempt(
            backend, {"attempt_id": "a" * 32}, time.monotonic() + 2, "fallback", None, None
        )
    assert failure.value.reason == "capacity"
    assert records[-1]["status"] == "capacity"


def test_fallback_reserve_diagnostic_does_not_round_shortfall_up(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def primary(body, deadline):
        now[0] = deadline + 0.0005
        raise facade.TransportUnavailable("read_timeout")

    # Force the primary to finish at route_end - reserve, then consume 0.5 ms
    # of the protected reserve before fallback starts.
    monkeypatch.setattr(facade, "ROUTE_SECONDS", 12.0)
    rows = []
    assert facade.Router(IDENTITY, primary, lambda *_: SUCCESS, emit=rows.append)(
        ARGS, context()
    ) == {"tools": []}
    assert rows[-1]["fallback_budget_ms"] == pytest.approx(7999.5)
    assert rows[-1]["fallback_reserve_satisfied"] is False

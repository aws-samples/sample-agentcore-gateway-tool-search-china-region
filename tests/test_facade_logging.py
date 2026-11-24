"""Pin emitted diagnostics while real credential handling exercises each outcome."""

import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from semantic_service import facade
from semantic_service.catalog import OAuthTokenSource
from semantic_service.route_telemetry import MeasuredSecrets, MeasuredTokenHTTP
from semantic_service.secrets import SecretsCache
from semantic_service.transport import RouteIdentity


@pytest.mark.parametrize(
    ("outcome", "status", "reason", "fails", "exchanges", "search_calls"),
    [
        ("success", "ok", None, False, 1, 1),
        ("rejected", "oauth_rejected", None, True, 2, 0),
        ("server_error", "oauth_server_failed", None, True, 1, 0),
        ("timeout", "oauth_transport_failed", "connect_timeout", False, 1, 0),
        ("deadline", "deadline_exceeded", "deadline_exceeded", False, 1, 0),
        ("malformed", "oauth_failed", None, True, 1, 0),
        ("secret_error", "oauth_failed", None, True, 0, 0),
    ],
)
def test_facade_stdout_contains_only_exchange_diagnostics(
    capsys, caplog, outcome, status, reason, fails, exchanges, search_calls
):
    caplog.set_level("DEBUG")
    client_id, client_secret = "private-client-marker", "private-secret-marker"
    access_token = "private-access-marker"
    query, envelope, message_id = "private-query-marker", "private-envelope-marker", "private-id"
    encoded_credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    basic = "Basic " + encoded_credentials
    success = {"version": 1, "ok": True, "result": {"tools": []}}
    exchanged, searched = [], []

    class Secrets:
        meta = SimpleNamespace(
            config=SimpleNamespace(
                connect_timeout=0.01,
                read_timeout=0.01,
                retries={"total_max_attempts": 1, "mode": "standard"},
            )
        )

        def get_secret_value(self, **kwargs):
            if outcome == "secret_error":
                raise RuntimeError(client_secret)
            return {
                "VersionId": "fixture-version",
                "SecretString": json.dumps(
                    {"client_id": client_id, "client_secret": client_secret}
                ),
            }

    def respond(request):
        if request.url.host == "token.example.invalid":
            assert request.headers["Authorization"] == basic
            exchanged.append(True)
            if outcome == "timeout":
                raise httpx.ConnectTimeout(client_secret)
            if outcome == "deadline":
                raise facade.DeadlineExpired()
            if outcome == "rejected":
                return httpx.Response(401, text=client_secret)
            if outcome == "server_error":
                return httpx.Response(500, text=client_secret)
            if outcome == "malformed":
                return httpx.Response(200, text=client_secret)
            return httpx.Response(
                200,
                json={"access_token": access_token, "token_type": "Bearer", "expires_in": 60},
            )
        assert request.url.host == "ecs.example.invalid"
        assert request.headers["Authorization"] == f"Bearer {access_token}"
        assert json.loads(request.content)["arguments"] == {
            "query": query,
            "_auth_context": envelope,
        }
        searched.append(True)
        return httpx.Response(200, json=success)

    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as http:
        tokens = OAuthTokenSource(
            SecretsCache(MeasuredSecrets(Secrets())),
            "fixture",
            "https://token.example.invalid/oauth",
            ("token.example.invalid",),
            ("search",),
            MeasuredTokenHTTP(http),
            timeout_seconds=0.5,
        )
        identity = RouteIdentity("gateway", "target", "Search___search_tools")
        context = identity.context(message_id)
        context.get_remaining_time_in_millis = lambda: 15000
        primary = facade.HTTPBackend("https://ecs.example.invalid/search", http, tokens)
        route = facade.Router(identity, primary, lambda *_: success)
        if fails:
            with pytest.raises(facade.ServiceError):
                route({"query": query, "_auth_context": envelope}, context)
        else:
            assert route({"query": query, "_auth_context": envelope}, context) == {"tools": []}

    output = capsys.readouterr()
    assert output.err == ""
    assert len(exchanged) == exchanges
    assert len(searched) == search_calls
    for private in (
        client_id,
        client_secret,
        access_token,
        encoded_credentials,
        basic,
        query,
        envelope,
        message_id,
    ):
        assert private not in output.out + output.err + caplog.text
    records = [json.loads(line) for line in output.out.splitlines()]
    observations = [row for row in records if row["operation"] == "search_oauth"]
    assert [row["phase"] for row in observations] == ["start", "end"]
    assert observations[0]["status"] == "started"
    assert observations[0]["stage_timings"] == {}
    assert observations[0]["transport_reason"] is None
    assert observations[1]["status"] == status
    assert observations[1]["transport_reason"] == reason
    expected_stages = {"aws.secrets"}
    if exchanges:
        expected_stages.add("oauth.token_exchange")
    assert set(observations[1]["stage_timings"]) == expected_stages
    for row in observations:
        assert set(row) == {
            "operation",
            "phase",
            "attempt_id",
            "backend",
            "timestamp_ms",
            "duration_ms",
            "remaining_ms",
            "status",
            "stage_timings",
            "transport_reason",
        }
        assert row["backend"] == "ecs"
        assert row["attempt_id"] == records[0]["attempt_id"]
        assert type(row["timestamp_ms"]) is int
        assert row["duration_ms"] >= 0
        assert row["remaining_ms"] >= 0
        for counters in row["stage_timings"].values():
            assert set(counters) == {"count", "duration_ms", "max_ms", "errors"}
            assert all(type(value) in (int, float) and value >= 0 for value in counters.values())

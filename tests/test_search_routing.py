"""Offline route controls: backend failures cannot weaken identity or public results."""

import json
import ssl
from types import SimpleNamespace

import pytest

from semantic_service.facade import (
    AuthenticationError,
    AuthorizationError,
    IndexNotReadyError,
    InputError,
    RateLimitError,
    Router,
    ServiceError,
    TransportUnavailable,
    check_response,
)
from semantic_service.transport import RouteIdentity, decode_request, encode_json

IDENTITY = RouteIdentity("gateway", "target", "Search___search_tools")
ARGUMENTS = {"query": "商品", "_auth_context": "opaque"}


def context():
    result = IDENTITY.context('原始 " ID \n')
    result.get_remaining_time_in_millis = lambda: 10000
    return result


class Primary:
    def __init__(self, result=None, error=None):
        self.result = result or {"version": 1, "ok": True, "result": {"tools": []}}
        self.error = error
        self.calls = []

    def __call__(self, body, deadline):
        self.calls.append(body)
        if self.error:
            raise self.error
        return self.result


def router(primary, fallback):
    return Router(IDENTITY, primary, fallback)


def test_success_preserves_result_identity_and_original_id():
    result = {"tools": [{"name": "商品", "description": "原文"}]}
    primary = Primary({"version": 1, "ok": True, "result": result})
    fallback = Primary()
    assert router(primary, fallback)(ARGUMENTS, context()) == result
    body = primary.calls[0]
    assert body["arguments"] == ARGUMENTS
    assert body["custom"]["bedrockAgentCoreMcpMessageId"] == '原始 " ID \n'
    assert body["custom"]["bedrockAgentCoreTargetId"] == "target"
    assert not fallback.calls


def test_transport_failure_falls_back_once_with_same_signed_request():
    primary = Primary(error=TransportUnavailable())
    fallback = Primary()
    assert router(primary, fallback)(ARGUMENTS, context()) == {"tools": []}
    assert len(primary.calls) == len(fallback.calls) == 1
    assert primary.calls[0]["arguments"] == fallback.calls[0]["arguments"]
    assert primary.calls[0]["custom"] == fallback.calls[0]["custom"]
    assert fallback.calls[0]["deadline_ms"] > primary.calls[0]["deadline_ms"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("unauthenticated", AuthenticationError),
        ("unauthorized", AuthorizationError),
        ("replay rejected", AuthenticationError),
        ("replay unavailable", ServiceError),
        ("deadline exceeded", ServiceError),
        ("clock skew", ServiceError),
        ("internal failure", ServiceError),
        ("invalid input", InputError),
        ("index not ready", IndexNotReadyError),
        ("rate limited", RateLimitError),
    ],
)
def test_semantic_failure_never_falls_back(error, expected):
    primary = Primary({"version": 1, "ok": False, "error": error, "retryable": False})
    fallback = Primary()
    with pytest.raises(expected) as caught:
        router(primary, fallback)(ARGUMENTS, context())
    assert type(caught.value) is expected
    assert not fallback.calls


def test_capacity_can_fallback_but_unknown_retry_flag_cannot():
    fallback = Primary()
    assert router(
        Primary({"version": 1, "ok": False, "error": "capacity", "retryable": True}), fallback
    )(ARGUMENTS, context()) == {"tools": []}
    assert len(fallback.calls) == 1
    fallback.calls.clear()
    with pytest.raises(ServiceError):
        router(
            Primary({"version": 1, "ok": False, "error": "unauthorized", "retryable": True}),
            fallback,
        )(ARGUMENTS, context())
    assert not fallback.calls


@pytest.mark.parametrize(
    "error", [ssl.SSLCertVerificationError(), ValueError("malformed"), RuntimeError("unknown")]
)
def test_unclassified_or_tls_failure_cannot_switch_backend(error):
    fallback = Primary()
    with pytest.raises(ServiceError):
        router(Primary(error=error), fallback)(ARGUMENTS, context())
    assert not fallback.calls


def test_identity_rejection_before_any_route():
    primary, fallback = Primary(), Primary()
    ctx = context()
    ctx.client_context.custom["bedrockAgentCoreTargetId"] = "other"
    with pytest.raises(ServiceError):
        router(primary, fallback)(ARGUMENTS, ctx)
    assert not primary.calls and not fallback.calls


def test_too_little_lambda_time_does_not_start_backend():
    primary, fallback = Primary(), Primary()
    ctx = context()
    ctx.get_remaining_time_in_millis = lambda: 50
    with pytest.raises(ServiceError):
        router(primary, fallback)(ARGUMENTS, ctx)
    assert not primary.calls and not fallback.calls


def test_fallback_failure_does_not_recurse():
    primary, fallback = Primary(error=TransportUnavailable()), Primary(error=TransportUnavailable())
    with pytest.raises(ServiceError):
        router(primary, fallback)(ARGUMENTS, context())
    assert len(primary.calls) == len(fallback.calls) == 1


@pytest.mark.parametrize(
    "value",
    [
        {"version": True, "ok": True, "result": {"tools": []}},
        {"version": 1, "ok": True, "result": {"tools": []}, "secret": "bad"},
        {"version": 1, "ok": True, "result": {"tools": "bad"}},
        {"version": 1, "ok": False, "error": "unknown", "retryable": True},
    ],
)
def test_strict_response_contract(value):
    with pytest.raises(ServiceError):
        check_response(value)


def test_wire_roundtrip_and_duplicate_key_rejection():
    body = {
        "version": 1,
        "arguments": ARGUMENTS,
        "custom": context().client_context.custom,
        "deadline_ms": 1700000000000,
        "attempt_id": "a" * 32,
    }
    assert decode_request(encode_json(body)) == body
    with pytest.raises(ValueError):
        decode_request(b'{"version":1,"version":1}')
    body["arguments"] = {"query": "x", "_auth_context": "x", "backend": "fallback"}
    with pytest.raises(ValueError):
        decode_request(json.dumps(body).encode())


def test_missing_context_cannot_be_reconstructed_from_envelope():
    primary, fallback = Primary(), Primary()
    with pytest.raises(ServiceError):
        router(primary, fallback)(ARGUMENTS, SimpleNamespace())
    assert not primary.calls and not fallback.calls


def test_oauth_deadline_is_budget_failure_and_allows_one_fallback():
    import httpx

    from semantic_service.facade import DeadlineExpired, HTTPBackend

    class Tokens:
        def token(self, context):
            raise DeadlineExpired()

    with httpx.Client() as client:
        backend = HTTPBackend("https://search.example/search", client, Tokens())
        fallback = Primary()
        assert router(backend, fallback)(ARGUMENTS, context()) == {"tools": []}
        assert len(fallback.calls) == 1


def test_fallback_context_is_clamped_and_not_forwarded_as_gateway_context(monkeypatch):
    import time

    from semantic_service import ecs_backend, fallback

    calls = []

    class Worker:
        def handle(self, body, *, backend, platform_request_id=None):
            calls.append((body, backend, platform_request_id))
            return {"version": 1, "ok": True, "result": {"tools": []}}

    monkeypatch.setattr(ecs_backend, "backend_factory", lambda _: Worker())
    body = {
        "version": 1,
        "arguments": ARGUMENTS,
        "custom": context().client_context.custom,
        "deadline_ms": int(time.time() * 1000 + 9000),
        "attempt_id": "a" * 32,
    }
    ctx = context()
    ctx.get_remaining_time_in_millis = lambda: 2000
    ctx.aws_request_id = "12345678-1234-1234-1234-123456789abc"
    assert fallback.handler(body, ctx)["ok"] is True
    assert calls[0][0]["deadline_ms"] < body["deadline_ms"]
    assert calls[0][1] == "fallback"
    assert calls[0][2] == ctx.aws_request_id
    assert fallback.handler(ARGUMENTS, ctx)["ok"] is False


@pytest.mark.parametrize(
    "envelope",
    [None, 42, "", " " * 10, "x" * 8193],
    ids=["null", "number", "empty", "whitespace", "oversized"],
)
def test_invalid_envelope_preserves_lambda_authentication_error(envelope):
    primary, fallback = Primary(), Primary()
    with pytest.raises(AuthenticationError):
        router(primary, fallback)({"query": "x", "_auth_context": envelope}, context())
    assert not primary.calls and not fallback.calls


def test_missing_envelope_preserves_lambda_authentication_error():
    with pytest.raises(AuthenticationError):
        router(Primary(), Primary())({"query": "x"}, context())


def test_route_logs_preserve_platform_id_and_both_attempts():
    logs = []
    ctx = context()
    ctx.aws_request_id = "12345678-1234-1234-1234-123456789abc"
    primary = Primary(error=TransportUnavailable())
    fallback = Primary()
    Router(IDENTITY, primary, fallback, emit=logs.append)(ARGUMENTS, ctx)
    assert logs[-1]["aws_request_id"] == ctx.aws_request_id
    assert logs[-1]["ecs_attempt_id"] == primary.calls[0]["attempt_id"]
    assert logs[-1]["attempt_id"] == fallback.calls[0]["attempt_id"]


def test_route_budget_constants_have_one_consistent_source():
    from semantic_service.route_budget import (
        CLOCK_SKEW_SECONDS,
        ECS_SECONDS,
        MIN_ATTEMPT_SECONDS,
        PLATFORM_RESERVE_SECONDS,
        ROUTE_SECONDS,
    )

    assert (ROUTE_SECONDS, ECS_SECONDS) == (14.0, 4.0)
    assert (MIN_ATTEMPT_SECONDS, PLATFORM_RESERVE_SECONDS, CLOCK_SKEW_SECONDS) == (0.5, 0.15, 1.0)

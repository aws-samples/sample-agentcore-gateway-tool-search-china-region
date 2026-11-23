"""Completed Search delivery must fit the real facade HTTP listening window."""

import socket
import ssl
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import test_ecs_backend as fixtures

from semantic_service.auth import EnvelopeBinding, EnvelopeCodec, Principal
from semantic_service.ecs_backend import Backend
from semantic_service.ecs_server import SearchServer, make_tls_context
from semantic_service.ecs_workers import ProcessApplication, WorkerPool
from semantic_service.facade import HTTPBackend, Router
from semantic_service.transport import RouteIdentity

IDENTITY = RouteIdentity("gateway-fixture", "target-fixture", "search___search_tools")
_CONNECT = socket.socket.connect
_GETADDRINFO = socket.getaddrinfo
RESULT = {"tools": [{"name": "ecs-only", "description": "completed once"}]}


class DeliveredAfterExecution(Backend):
    def execute(self, admitted):
        # Search and final authorization complete before the execution deadline.
        # Only the private result handoff crosses it; no fabricated Admission.
        time.sleep(max(0, admitted.end - time.monotonic() - 0.5))
        result = super().execute(admitted)
        time.sleep(0.7)
        return result


def delivery_backend():
    now = int(time.time())
    principal = Principal(
        "https://service.example.invalid",
        "facade",
        frozenset({"search"}),
        frozenset(),
        now,
        now + 60,
    )
    return DeliveredAfterExecution(
        identity=IDENTITY,
        envelope=EnvelopeCodec(fixtures.Keys()),
        replay=SimpleNamespace(consume=lambda *_: None),
        search=lambda *_: RESULT,
        verifier=SimpleNamespace(verify=lambda _: principal),
        subject="facade",
        scope="search",
    )


def test_completed_search_crosses_tls_once_without_fallback(tmp_path, monkeypatch):
    """Real Router -> httpx -> TLS server -> spawn workers -> HTTP response."""

    def connect(sock, address):
        assert address[0] == "127.0.0.1"
        return _CONNECT(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        assert host == "127.0.0.1"
        return _GETADDRINFO(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    app = object.__new__(ProcessApplication)
    app.admission = WorkerPool(delivery_backend, size=1, startup_seconds=10)
    app.search = WorkerPool(delivery_backend, size=1, startup_seconds=10)
    server = SearchServer(("127.0.0.1", 0), app, make_tls_context(tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fallback_calls = []
    now = int(time.time())
    principal = Principal(
        "https://users.example.invalid", "reader", frozenset({"read"}), frozenset(), now, now + 60
    )
    token = EnvelopeCodec(fixtures.Keys()).issue(
        principal,
        EnvelopeBinding(IDENTITY.gateway_id, IDENTITY.target_id, IDENTITY.tool_name, "id", "query"),
        policy_epoch=1,
    )
    context = IDENTITY.context("id")
    context.get_remaining_time_in_millis = lambda: 15000
    context.aws_request_id = "12345678-1234-1234-1234-123456789abc"

    def fallback(body, end):
        fallback_calls.append(body)
        return {"version": 1, "ok": True, "result": {"tools": []}}

    try:
        pem = ssl.get_server_certificate(server.server_address)
        with httpx.Client(verify=ssl.create_default_context(cadata=pem), trust_env=False) as http:
            backend = HTTPBackend(
                "https://search.example/search", http, SimpleNamespace(token=lambda _: "fixture")
            )
            backend.url = f"https://127.0.0.1:{server.server_port}/search"
            result = Router(IDENTITY, backend, fallback)(
                {"query": "query", "_auth_context": token}, context
            )
        assert fallback_calls == [], "completed ECS Search must not be billed again on fallback"
        assert result == RESULT
        assert app.search.healthy()
    finally:
        server.graceful_stop(timeout=2)
        thread.join(timeout=2)
        app.close()


@pytest.mark.parametrize("backend", ["ecs", "fallback"])
def test_delivery_reserve_only_reduces_ecs_execution_budget(monkeypatch, backend):
    from semantic_service import ecs_backend

    now = int(time.time())
    principal = Principal(
        "https://users.example.invalid", "reader", frozenset({"read"}), frozenset(), now, now + 60
    )
    token = EnvelopeCodec(fixtures.Keys()).issue(
        principal,
        EnvelopeBinding(IDENTITY.gateway_id, IDENTITY.target_id, IDENTITY.tool_name, "id", "query"),
        policy_epoch=1,
    )
    request = {
        "version": 1,
        "arguments": {"query": "query", "_auth_context": token},
        "custom": IDENTITY.context("id").client_context.custom,
        "deadline_ms": (now + 4) * 1000,
        "attempt_id": "a" * 32,
    }
    monkeypatch.setattr(ecs_backend.time, "time", lambda: now)
    monkeypatch.setattr(ecs_backend.time, "monotonic", lambda: 100)
    admitted = delivery_backend().admit(request, authorization="Bearer fixture", backend=backend)
    assert admitted.end == pytest.approx(103.5 if backend == "ecs" else 104)

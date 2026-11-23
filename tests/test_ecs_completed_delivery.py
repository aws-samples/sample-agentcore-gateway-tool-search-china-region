"""A completed authorized Search can survive a delayed private response handoff."""

import time
from dataclasses import replace
from types import SimpleNamespace

import test_ecs_backend as fixtures

from semantic_service.auth import EnvelopeBinding, EnvelopeCodec, Principal
from semantic_service.ecs_backend import Backend
from semantic_service.ecs_workers import ProcessApplication, WorkerPool
from semantic_service.facade import Router
from semantic_service.transport import RouteIdentity

IDENTITY = RouteIdentity("gateway-fixture", "target-fixture", "search___search_tools")


class DelayedDelivery(Backend):
    def execute(self, admitted):
        result = super().execute(admitted)
        time.sleep(max(0.0, admitted.end - time.monotonic()) + 0.15)
        return result


def backend_factory():
    now = int(time.time())
    principal = Principal(
        "https://service.example.invalid",
        "facade",
        frozenset({"search"}),
        frozenset(),
        now,
        now + 60,
    )
    return DelayedDelivery(
        identity=IDENTITY,
        envelope=EnvelopeCodec(fixtures.Keys()),
        replay=SimpleNamespace(consume=lambda *_: None),
        search=lambda *_: {"tools": []},
        verifier=SimpleNamespace(verify=lambda _: principal),
        subject="facade",
        scope="search",
    )


def test_completed_backend_survives_process_handoff_without_fallback():
    app = object.__new__(ProcessApplication)
    app.admission = WorkerPool(backend_factory, size=1, startup_seconds=5)
    app.search = WorkerPool(backend_factory, size=1, startup_seconds=5)
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
    fallback_calls = []

    def primary(body, end):
        admitted = app.admit(body, authorization="Bearer fixture")
        # Model the tighter local ECS ceiling while the facade still has time
        # to receive a completed result. Authentication gets its full window.
        return app.execute(replace(admitted, end=time.monotonic() + 0.8))

    def fallback(body, end):
        fallback_calls.append(body)
        return {"version": 1, "ok": True, "result": {"tools": []}}

    try:
        assert Router(IDENTITY, primary, fallback)(
            {"query": "query", "_auth_context": token}, context
        ) == {"tools": []}
        assert fallback_calls == []
        assert app.search.healthy() and app.search.utilization() == 0
    finally:
        app.close()


def slow_serialization_factory():
    from semantic_service import ecs_backend

    original = ecs_backend.encode_json
    execute_end = [0.0]

    def encode(value):
        if isinstance(value, dict) and value.get("ok") is True:
            time.sleep(max(0.0, execute_end[0] - time.monotonic()) + 0.15)
        return original(value)

    ecs_backend.encode_json = encode
    backend = backend_factory()

    # Isolate serialization delay from the separate delivery delay fixture.
    class SerializationBackend(Backend):
        def execute(self, admitted):
            execute_end[0] = admitted.end
            return super().execute(admitted)

    backend.__class__ = SerializationBackend
    return backend


def test_completion_marker_precedes_local_response_serialization():
    app = object.__new__(ProcessApplication)
    app.admission = WorkerPool(backend_factory, size=1, startup_seconds=5)
    app.search = WorkerPool(slow_serialization_factory, size=1, startup_seconds=5)
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
        "custom": vars(IDENTITY.context("id").client_context)["custom"],
        "deadline_ms": int(time.time() * 1000) + 4000,
        "attempt_id": "a" * 32,
    }
    try:
        admitted = app.admit(request, authorization="Bearer fixture")
        response = app.execute(replace(admitted, end=time.monotonic() + 0.8))
        assert response["ok"] is True
    finally:
        app.close()


class LostDelivery(Backend):
    def execute(self, admitted):
        super().execute(admitted)
        time.sleep(2)
        raise AssertionError("delivery watchdog must reclaim this process")


def lost_delivery_factory():
    backend = backend_factory()
    backend.__class__ = LostDelivery
    return backend


def test_completed_but_undeliverable_result_cannot_trigger_duplicate_fallback():
    import pytest

    from semantic_service.facade import ServiceError

    app = object.__new__(ProcessApplication)
    app.admission = WorkerPool(backend_factory, size=1, startup_seconds=5)
    app.search = WorkerPool(lost_delivery_factory, size=1, startup_seconds=5)
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
    fallback_calls = []
    primary_responses = []

    def primary(body, end):
        admitted = app.admit(body, authorization="Bearer fixture")
        response = app.execute(replace(admitted, end=time.monotonic() + 0.8))
        # Router maps arbitrary callback exceptions to ServiceError. Assertions
        # here would be swallowed by the expected outer error and falsely pass.
        primary_responses.append(response)
        return response

    def fallback(body, end):
        fallback_calls.append(body)
        return {"version": 1, "ok": True, "result": {"tools": []}}

    try:
        with pytest.raises(ServiceError):
            Router(IDENTITY, primary, fallback)({"query": "query", "_auth_context": token}, context)
        assert primary_responses == [
            {
                "version": 1,
                "ok": False,
                "error": "internal failure",
                "retryable": False,
            }
        ]
        assert not fallback_calls
        app.search.wait_ready(5)
        assert app.search.healthy()
    finally:
        app.close()

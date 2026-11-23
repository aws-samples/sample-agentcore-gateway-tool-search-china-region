"""Backend admission preserves signed Gateway identity and fails closed."""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from semantic_service.auth import (
    AuthenticationError,
    EnvelopeBinding,
    EnvelopeCodec,
    Principal,
    ServiceError,
)
from semantic_service.ecs_backend import Backend, DDBReplayStore
from semantic_service.transport import RouteIdentity


class Keys:
    def current(self):
        return "fixture", b"k" * 32

    def resolve(self, kid):
        if kid != "fixture":
            raise AuthenticationError()
        return b"k" * 32


class MemoryDDB:
    def __init__(self):
        self.calls = []
        self.items = {}
        self.broken = False

    def put_item(self, **kwargs):
        self.calls.append(kwargs)
        if self.broken:
            raise RuntimeError("private dependency details")
        key = kwargs["Item"]["pk"]["S"]
        now = int(kwargs["ExpressionAttributeValues"][":now"]["N"])
        if key in self.items and int(self.items[key]["expires_at"]["N"]) >= now:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = kwargs["Item"]
        return {}


@pytest.fixture
def service():
    now = int(time.time())
    identity = RouteIdentity("gateway-fixture", "target-fixture", "search___search_tools")
    codec = EnvelopeCodec(Keys())
    binding = EnvelopeBinding(
        identity.gateway_id, identity.target_id, identity.tool_name, "original-id", "find tools"
    )
    principal = Principal(
        "https://users.example.invalid", "reader", frozenset({"read"}), frozenset(), now, now + 60
    )
    token = codec.issue(principal, binding, policy_epoch=1)
    calls = []

    def search(event, context):
        calls.append((event, context))
        assert 0 < context.get_remaining_time_in_millis() <= 4000
        return {"tools": []}

    ddb = MemoryDDB()
    verifier = SimpleNamespace(
        verify=lambda token: Principal(
            "https://service.example.invalid",
            "facade-fixture",
            frozenset({"search.invoke"}),
            frozenset(),
            now,
            now + 60,
        )
    )
    backend = Backend(
        identity=identity,
        envelope=codec,
        replay=DDBReplayStore(ddb, "replay-fixture"),
        search=search,
        verifier=verifier,
        subject="facade-fixture",
        scope="search.invoke",
    )
    request = {
        "version": 1,
        "arguments": {"query": "find tools", "_auth_context": token},
        "custom": vars(identity.context("original-id").client_context)["custom"],
        "deadline_ms": int(time.time() * 1000) + 4000,
        "attempt_id": "a" * 32,
    }
    return SimpleNamespace(
        backend=backend, request=request, calls=calls, ddb=ddb, verifier=verifier
    )


def invoke(service, **kwargs):
    return service.backend.handle(
        service.request, authorization="Bearer service-token-fixture", **kwargs
    )


def test_success_preserves_arguments_original_mcp_id_and_result(service):
    assert invoke(service) == {"version": 1, "ok": True, "result": {"tools": []}}
    event, context = service.calls[0]
    assert event is service.request["arguments"]
    assert context.client_context.custom == service.request["custom"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["custom"].update(bedrockAgentCoreMcpMessageId="rewritten"),
        lambda r: r["custom"].update(bedrockAgentCoreTargetId="different-target"),
        lambda r: r["arguments"].update(query="changed query"),
        lambda r: r["arguments"].update(_auth_context="bad-signature"),
    ],
)
def test_identity_or_binding_tamper_is_rejected_before_replay_and_search(service, mutate):
    mutate(service.request)
    result = invoke(service)
    assert result["error"] == "unauthenticated"
    assert result["retryable"] is False
    assert service.calls == service.ddb.calls == []


@pytest.mark.parametrize("authorization", [None, "", "Basic abc", "Bearer ", "Bearer a b"])
def test_missing_or_malformed_service_bearer_rejected(service, authorization):
    assert service.backend.handle(service.request, authorization=authorization)["ok"] is False
    assert not service.calls and not service.ddb.calls


@pytest.mark.parametrize("claim", ["subject", "scope"])
def test_service_token_must_have_specific_subject_and_scope(service, claim):
    principal = service.verifier.verify("fixture")
    service.verifier.verify = lambda token: Principal(
        principal.issuer,
        "another-client" if claim == "subject" else principal.subject,
        frozenset() if claim == "scope" else principal.scopes,
        principal.groups,
        principal.issued_at,
        principal.expires_at,
    )
    assert invoke(service)["error"] == "unauthenticated"
    assert not service.calls and not service.ddb.calls


def test_replay_denied_on_same_backend_but_one_fallback_attempt_allowed(service):
    assert invoke(service)["ok"]
    service.request["attempt_id"] = "b" * 32
    assert invoke(service)["error"] == "replay rejected"
    assert invoke(service, backend="fallback")["ok"]
    assert invoke(service, backend="fallback")["error"] == "replay rejected"
    assert len(service.calls) == 2


def test_signature_base64_alias_cannot_bypass_replay(service):
    assert invoke(service)["ok"]
    token = service.request["arguments"]["_auth_context"]
    header, body, signature = token.split(".")
    service.request["arguments"]["_auth_context"] = ".".join((header, body, signature + "="))
    assert invoke(service)["error"] == "replay rejected"
    assert len(service.calls) == 1


def test_replay_hash_is_canonical_and_never_stores_envelope(service):
    assert invoke(service)["ok"]
    item = service.ddb.calls[0]["Item"]
    assert set(item) == {"pk", "expires_at"}
    assert item["pk"]["S"].startswith("ecs/")
    assert len(item["pk"]["S"]) == 68
    token = service.request["arguments"]["_auth_context"]
    assert token not in json.dumps(service.ddb.calls)
    assert "reader" not in json.dumps(service.ddb.calls)
    assert service.ddb.calls[0]["ConditionExpression"] == (
        "attribute_not_exists(pk) OR expires_at < :now"
    )
    assert int(item["expires_at"]["N"]) - int(time.time()) >= 119


def test_replay_storage_failure_allows_one_fallback_attempt(service):
    service.ddb.broken = True
    assert invoke(service) == {
        "version": 1,
        "ok": False,
        "error": "replay unavailable",
        "retryable": True,
    }
    assert not service.calls


@pytest.mark.parametrize("deadline", [True, 0, -1, "4000", 10**16])
def test_bad_or_expired_deadline_denies_before_execution(service, deadline):
    service.request["deadline_ms"] = deadline
    assert invoke(service)["ok"] is False
    assert not service.calls and not service.ddb.calls


def test_control_plane_failure_is_fixed_and_nonretryable(service, capsys):
    def failure(*args):
        raise ServiceError()

    service.backend.search = failure
    result = invoke(service)
    assert result == {"version": 1, "ok": False, "error": "internal failure", "retryable": False}
    assert "service-token-fixture" not in capsys.readouterr().out


def test_completed_search_is_not_discarded_by_a_post_return_wall_clock_jump(service, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr("semantic_service.ecs_backend.time.time", lambda: clock[0])

    def completed(*args):
        clock[0] += 5
        return {"tools": []}

    service.backend.search = completed
    assert invoke(service) == {"version": 1, "ok": True, "result": {"tools": []}}


def test_fallback_admission_rejects_route_budget_used_as_backend_budget(service):
    service.request["deadline_ms"] = int(time.time() * 1000) + 12500
    assert invoke(service, backend="fallback")["error"] == "clock skew"
    assert not service.calls and not service.ddb.calls


def test_fallback_clock_skew_tolerance_cannot_extend_execution_allowance(monkeypatch):
    monkeypatch.setattr("semantic_service.ecs_backend.time.time", lambda: 1000)
    assert Backend._deadline(1_010_500, "fallback") == 1_010_000


@pytest.mark.parametrize("offset", [-0.001, 0.0, 0.001, 0.5])
def test_completed_search_is_kept_without_a_late_clock_observation(service, monkeypatch, offset):
    admitted = service.backend.admit(service.request, authorization="Bearer service-token-fixture")
    clock = [time.monotonic()]
    monkeypatch.setattr("semantic_service.ecs_backend.time.monotonic", lambda: clock[0])

    def complete(*args):
        clock[0] = admitted.end + offset
        return {"tools": []}

    service.backend.search = complete
    result = service.backend.execute(admitted)
    assert result["ok"] is True


def test_completed_search_survives_time_spent_after_execution_returns(service, monkeypatch):
    from semantic_service import ecs_backend

    clock = [time.monotonic()]
    monkeypatch.setattr(ecs_backend.time, "monotonic", lambda: clock[0])

    class Context:
        def set(self, value):
            return object()

        def reset(self, token):
            clock[0] += 5

    monkeypatch.setattr(ecs_backend, "_EXECUTION_END", Context())
    assert invoke(service)["ok"] is True


def test_admission_deadline_is_not_reset_after_service_authentication(service, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr("semantic_service.ecs_backend.time.time", lambda: clock[0])
    original = service.verifier.verify

    def slow_verify(token):
        clock[0] += 5
        return original(token)

    service.verifier.verify = slow_verify
    assert invoke(service)["error"] == "deadline exceeded"
    assert not service.calls and not service.ddb.calls


def test_invalid_backend_never_creates_unbounded_replay_namespace(service):
    assert invoke(service, backend="client-selected")["ok"] is False
    assert not service.calls and not service.ddb.calls


def test_fallback_backend_does_not_require_service_bearer_but_still_verifies_binding(service):
    service.backend.verifier = None
    assert service.backend.handle(service.request, backend="fallback")["ok"] is True
    changed = deepcopy(service.request)
    changed["arguments"]["query"] = "tampered"
    assert service.backend.handle(changed, backend="fallback")["error"] == "unauthenticated"


def test_ecs_never_accepts_missing_service_verifier(service):
    service.backend.verifier = None
    assert service.backend.handle(service.request)["error"] == "unauthenticated"
    assert not service.calls and not service.ddb.calls


def test_fallback_preserves_remaining_facade_budget_for_cold_start(service):
    service.request["deadline_ms"] = int(time.time() * 1000) + 8900
    observed = []
    service.backend.search = lambda _args, context: (
        observed.append(context.get_remaining_time_in_millis()) or {"tools": []}
    )
    assert invoke(service, backend="fallback")["ok"] is True
    assert 8500 < observed[0] <= 8900


def test_attempt_log_and_existing_embedding_log_can_be_joined_without_query(service, capsys):
    assert invoke(service)["ok"]
    context = service.calls[0][1]
    assert context.aws_request_id == str(uuid.UUID(hex=service.request["attempt_id"]))
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [record["phase"] for record in records] == ["start", "end"]
    logged = records[-1]
    assert logged["attempt_id"] == service.request["attempt_id"]
    assert logged["aws_request_id"] == context.aws_request_id
    assert len(logged["correlation_id"]) == 64
    assert "find tools" not in json.dumps(logged)
    assert "original-id" not in json.dumps(logged)


def test_completed_search_is_not_undone_by_either_clock_moving(service, monkeypatch):
    wall, monotonic = [time.time()], [time.monotonic()]
    monkeypatch.setattr("semantic_service.ecs_backend.time.time", lambda: wall[0])
    monkeypatch.setattr("semantic_service.ecs_backend.time.monotonic", lambda: monotonic[0])

    def slow(*args):
        wall[0] -= 60
        monotonic[0] += 5
        return {"tools": []}

    service.backend.search = slow
    assert invoke(service)["ok"] is True


def test_budget_expires_while_waiting_for_jwks_lock_without_starting_refresh(service):
    calls = []
    original = service.verifier.verify
    service.verifier.verify = lambda token: calls.append(token) or original(token)
    service.request["deadline_ms"] = int(time.time() * 1000) + 50
    service.backend._verifier_lock.acquire()
    release = threading.Timer(0.2, service.backend._verifier_lock.release)
    release.start()
    try:
        assert invoke(service)["error"] == "deadline exceeded"
        assert calls == []
    finally:
        release.join(1)


@pytest.fixture
def factory_inputs(monkeypatch, service):
    from semantic_service import runtime
    from semantic_service.ecs_backend import backend_factory

    values = {
        "SEMSEARCH_GATEWAY_ID": "gateway-fixture",
        "SEMSEARCH_SEARCH_TARGET_ID": "target-fixture",
        "SEMSEARCH_SEARCH_TOOL_NAME": "search___search_tools",
        "SEMSEARCH_REPLAY_TABLE": "replay-fixture",
        "OIDC_ISSUER": "https://service.example.invalid",
        "OIDC_JWKS_URI": "https://service.example.invalid/jwks",
        "OIDC_DISCOVERY_URI": "https://service.example.invalid/discovery",
        "OIDC_AUDIENCE": "backend-fixture",
        "OIDC_ALLOWED_HOSTS": "service.example.invalid",
        "SEMSEARCH_ECS_OAUTH_SUBJECT": "facade-fixture",
        "SEMSEARCH_ECS_OAUTH_SCOPE": "search.invoke",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    controls, loads = [], []
    marker = object()

    def read_control():
        controls.append("read")
        return marker

    snapshots = SimpleNamespace(current=lambda control: loads.append(control) or marker)
    handler = SimpleNamespace(
        search_tool=SimpleNamespace(
            identity=SimpleNamespace(
                gateway=SimpleNamespace(gateway_id="gateway-fixture"), target_id="target-fixture"
            ),
            name="search___search_tools",
        ),
        snapshots=snapshots,
    )
    monkeypatch.setattr(runtime, "_search", lambda: handler)
    monkeypatch.setattr(runtime, "_store", lambda: SimpleNamespace(read_control=read_control))
    monkeypatch.setattr(runtime, "_aws", lambda name: service.ddb)
    monkeypatch.setattr(runtime, "_http", lambda: object())
    monkeypatch.setattr(runtime, "_envelope", lambda operation: service.backend.envelope)
    backend_factory.cache_clear()
    yield SimpleNamespace(handler=handler, controls=controls, loads=loads, marker=marker)
    backend_factory.cache_clear()


def test_ecs_factory_preloads_snapshot_before_exposing_readiness(factory_inputs):
    from semantic_service.ecs_backend import backend_factory

    backend_factory("ecs")
    assert factory_inputs.controls == ["read"]
    assert factory_inputs.loads == [factory_inputs.marker]


def test_snapshot_initialization_failure_prevents_ecs_readiness(factory_inputs):
    from semantic_service.ecs_backend import backend_factory

    def failure(control):
        raise ServiceError()

    factory_inputs.handler.snapshots.current = failure
    with pytest.raises(ServiceError):
        backend_factory("ecs")


def test_fallback_factory_keeps_original_lazy_search_initialization(factory_inputs):
    from semantic_service.ecs_backend import backend_factory

    backend_factory("fallback")
    assert factory_inputs.controls == factory_inputs.loads == []


def test_ecs_serializes_snapshot_cache_mutation_without_serializing_search(factory_inputs):
    from semantic_service.ecs_backend import backend_factory

    active, maximum = 0, 0
    lock = threading.Lock()

    def snapshot(control):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return control

    factory_inputs.handler.snapshots.current = snapshot
    backend_factory("ecs")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(factory_inputs.handler.snapshots.current, range(8)))
    assert results == list(range(8))
    assert maximum == 1


def test_backend_reuses_lambda_search_final_authorization_and_result_semantics():
    from service.test_identity import (
        BUSINESS,
        SEARCH,
        Authorizer,
        codec,
        context,
        event,
        handler,
        view,
    )

    identity = RouteIdentity("gateway-fixture", "search-target", SEARCH.name)
    original = event()
    request = {
        "version": 1,
        "arguments": original,
        "custom": context().client_context.custom,
        "deadline_ms": int(time.time() * 1000) + 4000,
        "attempt_id": "a" * 32,
    }
    expected = handler()(original, context())
    authorizer = Authorizer()
    backend = Backend(
        identity=identity,
        envelope=codec(),
        replay=DDBReplayStore(MemoryDDB(), "replay-fixture"),
        search=handler(authorizer),
    )
    assert backend.handle(request, backend="fallback")["result"] == expected
    assert expected == {"tools": [BUSINESS.descriptor]}
    assert authorizer.calls >= 2
    request["arguments"] = event()
    backend.search = handler(Authorizer([view(), view(allowed=[])]))
    result = backend.handle(request, backend="fallback")
    assert result["error"] == "unauthorized" and result["retryable"] is False


def test_replay_rejects_invalid_token_before_ddb_write():
    store = DDBReplayStore(MemoryDDB(), "replay-fixture")
    with pytest.raises(AuthenticationError):
        store.consume(base64.b64encode(b"not-json").decode(), "ecs")


def test_authenticated_admission_does_not_consume_replay_or_search(service):
    admitted = service.backend.admit(service.request, authorization="Bearer service-token-fixture")
    assert not service.calls and not service.ddb.calls
    assert service.backend.execute(admitted)["ok"] is True
    assert len(service.calls) == len(service.ddb.calls) == 1


def test_fallback_keeps_real_platform_request_id(service, capsys):
    actual = "12345678-1234-1234-1234-123456789012"
    assert invoke(service, backend="fallback", platform_request_id=actual)["ok"]
    assert service.calls[0][1].aws_request_id == actual
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert all(row["aws_request_id"] == actual for row in records)
    assert all("timestamp_ms" in row and "remaining_ms" in row for row in records)


def test_clock_skew_is_retryable_infrastructure_error(service):
    service.request["deadline_ms"] = int(time.time() * 1000) + 6000
    assert invoke(service) == {"version": 1, "ok": False, "error": "clock skew", "retryable": True}
    assert service.calls == service.ddb.calls == []


def test_admission_factory_does_not_build_or_preload_search(factory_inputs, monkeypatch):
    from semantic_service import runtime
    from semantic_service.ecs_backend import backend_factory

    def forbidden():
        raise AssertionError("admission must not allocate retrieval cache")

    monkeypatch.setattr(runtime, "_search", forbidden)
    backend_factory("admission")
    assert factory_inputs.controls == factory_inputs.loads == []


def test_snapshot_lock_obeys_current_execution_deadline(service):
    from semantic_service.ecs_backend import _SynchronizedSnapshots

    snapshots = _SynchronizedSnapshots(SimpleNamespace(current=lambda value: value))
    snapshots._lock.acquire()
    release = threading.Timer(0.2, snapshots._lock.release)
    release.start()
    service.request["deadline_ms"] = int(time.time() * 1000) + 50
    service.backend.search = lambda *_args: snapshots.current(object())
    try:
        started = time.monotonic()
        assert invoke(service)["error"] == "deadline exceeded"
        assert time.monotonic() - started < 0.15
    finally:
        release.join(1)


def test_backend_logs_distinguish_replay_rejection_from_search_start(service, capsys):
    assert invoke(service)["ok"]
    assert all(
        row["search_started"] for row in map(json.loads, capsys.readouterr().out.splitlines())
    )
    assert invoke(service)["error"] == "replay rejected"
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(rows) == 1 and rows[0]["phase"] == "end"
    assert rows[0]["search_started"] is False

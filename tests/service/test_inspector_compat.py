"""Offline regressions for the reported Inspector request shapes.

The report records wire status and parameter shapes, not Lambda events. These
tests reconstruct those shapes with synthetic identities; they do not assert
that a live Gateway preserves numeric IDs in the target's trusted context.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_identity import (
    BINDING,
    BUSINESS,
    NOW,
    PRINCIPAL,
    SEARCH,
    Authorizer,
    Keys,
    codec,
    context,
    handler,
    interceptor,
    interceptor_event,
    view,
)

from semantic_core.catalog import canonical_json
from semantic_service import ecs_backend, facade, fallback, request_runtime, runtime
from semantic_service._mcp_id import correlation_id, require_mcp_id
from semantic_service.auth import AuthenticationError, AuthorizationError, EnvelopeCodec, InputError
from semantic_service.ecs_backend import Backend, DDBReplayStore
from semantic_service.transport import RouteIdentity, decode_request, encode_json


@pytest.mark.parametrize(
    "request_id", [1, 0, -1, 2**63, "1", 10**256 - 1, -(10**256 - 1), "x" * 256]
)
@pytest.mark.parametrize("method", ["initialize", "tools/list", "tools/call"])
def test_inspector_ids_preserve_json_type(method, request_id):
    params = (
        {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "fixture"}}
        if method == "initialize"
        else {"name": SEARCH.name, "arguments": {"query": "cancel"}}
        if method == "tools/call"
        else {}
    )
    request = interceptor_event(method, params=params)
    request["mcp"]["gatewayRequest"]["body"]["id"] = request_id
    original = copy.deepcopy(request)
    result = interceptor()(request, None)["mcp"]
    if method == "tools/list":
        echoed = result["transformedGatewayResponse"]["body"]["id"]
    else:
        forwarded = result["transformedGatewayRequest"]["body"]
        echoed = forwarded["id"]
        if method == "tools/call":
            assert handler()(
                forwarded["params"]["arguments"],
                context(bedrockAgentCoreMcpMessageId=request_id),
            ) == {"tools": [BUSINESS.descriptor]}
    assert type(echoed) is type(request_id)
    assert echoed == request_id
    assert request == original


@pytest.mark.parametrize(
    "params",
    [
        {"_meta": {"progressToken": 1}},
        {"_meta": {"progressToken": "1", "extension": {"nested": [True, None]}}},
        {"cursor": "opaque"},
        {"cursor": ""},
        {"cursor": "opaque", "_meta": {}},
    ],
)
def test_inspector_list_metadata_and_cursor_return_same_single_page(params):
    request = interceptor_event("tools/list", params=params)
    result = interceptor()(request, None)["mcp"]["transformedGatewayResponse"]
    assert result == {
        "statusCode": 200,
        "body": {
            "jsonrpc": "2.0",
            "id": "mcp-1",
            "result": {"tools": [SEARCH.descriptor]},
        },
    }


@pytest.mark.parametrize("name", [SEARCH.name, BUSINESS.name])
def test_call_metadata_is_ignored_without_trusting_or_forwarding_it(name, capsys):
    request = interceptor_event(
        params={
            "name": name,
            "arguments": {"query": "cancel", "_auth_context": "forged"},
            "_meta": {"progressToken": 1, "_auth_context": "private-marker", "subject": "admin"},
        }
    )
    original = copy.deepcopy(request)
    body = interceptor()(request, None)["mcp"]["transformedGatewayRequest"]["body"]
    assert "_meta" not in body["params"]
    arguments = body["params"]["arguments"]
    if name == SEARCH.name:
        assert codec().verify(arguments["_auth_context"], BINDING).principal == PRINCIPAL
    else:
        assert "_auth_context" not in arguments
    assert request == original
    assert "private-marker" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "extra",
    [
        {"_meta": None},
        {"_meta": []},
        {"_meta": "private-marker"},
        {"unknown": "private-marker"},
    ],
)
@pytest.mark.parametrize("method", ["tools/list", "tools/call"])
def test_invalid_or_unknown_protocol_parameters_remain_input_errors(method, extra):
    params = (
        {"name": SEARCH.name, "arguments": {"query": "cancel"}} if method == "tools/call" else {}
    )
    with pytest.raises(InputError):
        interceptor()(interceptor_event(method, params=params | extra), None)


@pytest.mark.parametrize("cursor", [None, 1, True, [], {}])
def test_list_cursor_must_be_a_string(cursor):
    with pytest.raises(InputError):
        interceptor()(interceptor_event("tools/list", params={"cursor": cursor}), None)


def test_numeric_and_string_id_bindings_never_collide(monkeypatch):
    monkeypatch.setattr("semantic_service.auth.secrets.token_urlsafe", lambda _: "fixed-nonce")
    bindings = [replace(BINDING, mcp_id=value) for value in (1, "1")]
    tokens = [codec().issue(PRINCIPAL, binding, policy_epoch=1) for binding in bindings]
    assert canonical_json(bindings[0].claims()) != canonical_json(bindings[1].claims())
    assert tokens[0] != tokens[1]
    assert correlation_id(1) != correlation_id("1")
    for index, binding in enumerate(bindings):
        assert codec().verify(tokens[index], binding).principal == PRINCIPAL
        if index == 0:
            with pytest.raises(AuthenticationError):
                codec().verify(tokens[1], binding)
        else:
            assert codec().verify(tokens[0], binding).principal == PRINCIPAL
        assert handler()(
            {"query": binding.query, "_auth_context": tokens[index]},
            context(bedrockAgentCoreMcpMessageId=binding.mcp_id),
        ) == {"tools": [BUSINESS.descriptor]}
        if index == 0:
            with pytest.raises(AuthenticationError):
                handler()(
                    {"query": binding.query, "_auth_context": tokens[1]},
                    context(bedrockAgentCoreMcpMessageId=binding.mcp_id),
                )
        else:
            assert handler()(
                {"query": binding.query, "_auth_context": tokens[0]},
                context(bedrockAgentCoreMcpMessageId=binding.mcp_id),
            ) == {"tools": [BUSINESS.descriptor]}


@pytest.mark.parametrize("request_id", [1, 0, -1, "1"])
def test_private_transport_preserves_id_type(request_id):
    identity = RouteIdentity(BINDING.gateway_id, BINDING.target_id, BINDING.tool_name)
    binding = replace(BINDING, mcp_id=request_id)
    request = {
        "version": 1,
        "arguments": {
            "query": binding.query,
            "_auth_context": codec().issue(PRINCIPAL, binding, policy_epoch=1),
        },
        "custom": identity.context(request_id).client_context.custom,
        "deadline_ms": 1,
        "attempt_id": "a" * 32,
    }
    restored = decode_request(encode_json(request))
    actual = identity.validate_custom(restored["custom"])
    assert type(actual) is type(request_id)
    assert actual == request_id
    assert handler()(restored["arguments"], identity.context(actual)) == {
        "tools": [BUSINESS.descriptor]
    }


def test_request_runtime_propagates_input_error_without_http_mapping(monkeypatch, capsys):
    monkeypatch.setattr(runtime, "_request", interceptor)
    request = interceptor_event("tools/list", params={"unknown": "private-marker"})
    with pytest.raises(InputError, match="invalid input"):
        request_runtime.request_handler(request, None)
    record = json.loads(capsys.readouterr().out)
    assert record["operation"] == "request"
    assert record["status"] == "invalid input"
    assert "private-marker" not in json.dumps(record)


@pytest.mark.parametrize("request_id", [1, "1"])
def test_machine_listing_preserves_cursor_and_id_but_ignores_metadata(request_id):
    machine = replace(PRINCIPAL, subject="machine-fixture")
    request = interceptor_event(
        "tools/list", params={"cursor": "opaque", "_meta": {"progressToken": 1}}
    )
    request["mcp"]["gatewayRequest"]["body"]["id"] = request_id
    body = interceptor(machine)(request, None)["mcp"]["transformedGatewayRequest"]["body"]
    assert body["params"] == {"cursor": "opaque"}
    assert type(body["id"]) is type(request_id)
    assert body["id"] == request_id


@pytest.mark.parametrize("method", ["initialize", "tools/list", "tools/call", "ping"])
@pytest.mark.parametrize("request_id", [1.0, None, True, False, {}, []])
def test_invalid_id_types_remain_rejected(method, request_id):
    request = interceptor_event(method)
    request["mcp"]["gatewayRequest"]["body"]["id"] = request_id
    with pytest.raises(InputError):
        interceptor()(request, None)


@pytest.mark.parametrize("method", ["initialize", "tools/list", "tools/call", "ping"])
def test_requests_must_have_an_id(method):
    request = interceptor_event(method)
    request["mcp"]["gatewayRequest"]["body"].pop("id")
    with pytest.raises(InputError):
        interceptor()(request, None)


@pytest.mark.parametrize("request_id", [1, "1", 0, -1])
@pytest.mark.parametrize("route", ["ecs", "fallback"])
@pytest.mark.parametrize("stringified", [False, True])
def test_numeric_ids_survive_facade_and_backend(request_id, route, stringified, capsys):
    identity = RouteIdentity(BINDING.gateway_id, BINDING.target_id, BINDING.tool_name)
    consumed = []
    service_principal = replace(PRINCIPAL, scopes=frozenset({"invoke"}))
    worker = Backend(
        identity=identity,
        envelope=codec(),
        replay=DDBReplayStore(
            SimpleNamespace(put_item=lambda **kwargs: consumed.append(kwargs)), "fixture"
        ),
        search=handler(),
        verifier=SimpleNamespace(verify=lambda _: service_principal) if route == "ecs" else None,
        subject=service_principal.subject,
        scope="invoke",
    )
    binding = replace(BINDING, mcp_id=request_id)
    token = codec().issue(PRINCIPAL, binding, policy_epoch=1)
    context_id = str(request_id) if stringified else request_id
    invocation_context = identity.context(context_id)
    invocation_context.get_remaining_time_in_millis = lambda: 15000
    invocation_context.aws_request_id = "12345678-1234-1234-1234-123456789abc"

    def primary(body, end):
        if route == "fallback":
            raise facade.TransportUnavailable("read_timeout")
        return worker.handle(
            decode_request(encode_json(body)), authorization="Bearer fixture", backend="ecs"
        )

    def secondary(body, end):
        assert route == "fallback"
        return worker.handle(decode_request(encode_json(body)), backend="fallback")

    result = facade.Router(identity, primary, secondary)(
        {"query": binding.query, "_auth_context": token}, invocation_context
    )
    assert result == {"tools": [BUSINESS.descriptor]}
    assert len(consumed) == 1
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    correlations = {record["correlation_id"] for record in records if "correlation_id" in record}
    assert correlations == {correlation_id(context_id)}
    admissions = [record for record in records if record.get("phase") == "admission"]
    assert bool(admissions) is (stringified and type(request_id) is int)
    for record in admissions:
        assert record["binding_match"] == "mcp_id_str_coerced"


@pytest.mark.parametrize("request_id", [1, "1", 0, -1])
def test_fallback_entry_preserves_id_and_correlation(request_id, monkeypatch, capsys):
    identity = RouteIdentity(BINDING.gateway_id, BINDING.target_id, BINDING.tool_name)
    observed = []

    def handle(request, **kwargs):
        actual = identity.validate_custom(request["custom"])
        assert actual == request_id
        assert type(actual) is type(request_id)
        observed.append(request)
        return {"version": 1, "ok": True, "result": {"tools": []}}

    monkeypatch.setattr(ecs_backend, "backend_factory", lambda _: SimpleNamespace(handle=handle))
    binding = replace(BINDING, mcp_id=request_id)
    invocation_context = identity.context(request_id)
    invocation_context.get_remaining_time_in_millis = lambda: 15000
    request = {
        "version": 1,
        "arguments": {
            "query": binding.query,
            "_auth_context": codec().issue(PRINCIPAL, binding, policy_epoch=1),
        },
        "custom": invocation_context.client_context.custom,
        "deadline_ms": int(time.time() * 1000) + 4000,
        "attempt_id": "a" * 32,
    }
    assert fallback.handler(request, invocation_context)["ok"] is True
    assert len(observed) == 1
    record = json.loads(capsys.readouterr().out)
    assert record["correlation_id"] == correlation_id(request_id)


@pytest.mark.parametrize("claimed_id,context_id", [(1, "01"), ("1", 1), (1, True), (1, 1.0)])
def test_backend_rejects_type_confusion_before_replay_or_search(claimed_id, context_id):
    identity = RouteIdentity(BINDING.gateway_id, BINDING.target_id, BINDING.tool_name)
    worker = Backend(
        identity=identity,
        envelope=codec(),
        replay=SimpleNamespace(consume=lambda *_: pytest.fail("mismatch consumed replay slot")),
        search=lambda *_: pytest.fail("mismatch reached search"),
    )
    binding = replace(BINDING, mcp_id=claimed_id)
    custom = identity.context(claimed_id).client_context.custom
    custom["bedrockAgentCoreMcpMessageId"] = context_id
    request = {
        "version": 1,
        "arguments": {
            "query": binding.query,
            "_auth_context": codec().issue(PRINCIPAL, binding, policy_epoch=1),
        },
        "custom": custom,
        "deadline_ms": int(time.time() * 1000) + 4000,
        "attempt_id": "a" * 32,
    }
    result = worker.handle(request, backend="fallback")
    assert result["ok"] is False
    assert result["error"] == "unauthenticated"


@pytest.mark.parametrize("request_id", [10**256, -(10**256), "", " ", "x" * 257, "\ud800"])
@pytest.mark.parametrize("method", ["initialize", "tools/list", "tools/call", "ping"])
def test_invalid_ids_fail_before_authorization_and_quota(method, request_id, monkeypatch, capsys):
    authorizer = Authorizer()
    worker = interceptor(auth=authorizer)
    worker.limiter = SimpleNamespace(admit=lambda *_a, **_k: pytest.fail("quota consumed"))
    monkeypatch.setattr(runtime, "_request", lambda: worker)
    request = interceptor_event(method)
    request["mcp"]["gatewayRequest"]["body"]["id"] = request_id
    with pytest.raises(InputError):
        request_runtime.request_handler(request, None)
    assert authorizer.calls == 0
    record = json.loads(capsys.readouterr().out)
    assert record["status"] == "invalid input"


@pytest.mark.parametrize("request_id", ["\ud800", "\udfff", "a\ud800b"])
def test_id_validation_rejects_isolated_surrogates(request_id):
    with pytest.raises(ValueError):
        require_mcp_id(request_id)


@pytest.mark.parametrize("params", [None, [], {"unknown": True}, {"cursor": 1}, {"_meta": None}])
def test_machine_listing_rejects_invalid_params(params):
    machine = replace(PRINCIPAL, subject="machine-fixture")
    request = interceptor_event("tools/list")
    request["mcp"]["gatewayRequest"]["body"]["params"] = params
    with pytest.raises(InputError):
        interceptor(machine)(request, None)


@pytest.mark.parametrize("method", ["tools/list", "tools/call"])
def test_missing_search_permission_precedes_malformed_params(method):
    authorizer = Authorizer([view([])])
    with pytest.raises(AuthorizationError):
        interceptor(auth=authorizer)(interceptor_event(method, params={"unknown": True}), None)
    assert authorizer.calls == 1


@pytest.mark.parametrize("request_id", [0, 1, -1, 10**256 - 1])
def test_integer_correlations_do_not_match_legacy_same_value_strings(request_id):
    assert correlation_id(request_id) != hashlib.sha256(str(request_id).encode()).hexdigest()
    assert correlation_id(request_id) != correlation_id(str(request_id))


@pytest.mark.parametrize(
    "signed_id,context_id,tamper,expected",
    [
        (1, "1", False, True),
        (-1, "-1", False, True),
        (0, "0", False, True),
        (1, "01", False, False),
        (1, "+1", False, False),
        (1, " 1", False, False),
        (1, "1 ", False, False),
        (1, "1.0", False, False),
        (0, "-0", False, False),
        ("1", 1, False, False),
        (1, True, False, False),
        (1, 1.0, False, False),
        (1, "1", True, False),
        (10**256 - 1, str(10**256 - 1), False, True),
        (-(10**256 - 1), str(-(10**256 - 1)), False, False),
    ],
)
@pytest.mark.parametrize("route", ["search", "backend"])
def test_one_way_match_diagnostic_is_authenticated_fixed_and_log_only(
    signed_id, context_id, tamper, expected, route, monkeypatch, capsys
):
    binding = replace(BINDING, mcp_id=signed_id)
    token = codec().issue(PRINCIPAL, binding, policy_epoch=1)
    if tamper:
        header, body, _signature = token.split(".")
        token = f"{header}.{body}.{'A' * 43}"
    arguments = {"query": binding.query, "_auth_context": token}
    if route == "search":
        monkeypatch.setattr(runtime, "_search", handler)
        if expected:
            assert runtime.search_handler(
                arguments, context(bedrockAgentCoreMcpMessageId=context_id)
            ) == {"tools": [BUSINESS.descriptor]}
        else:
            with pytest.raises(AuthenticationError):
                runtime.search_handler(arguments, context(bedrockAgentCoreMcpMessageId=context_id))
    else:
        identity = RouteIdentity(BINDING.gateway_id, BINDING.target_id, BINDING.tool_name)
        worker = Backend(
            identity=identity,
            envelope=codec(),
            replay=SimpleNamespace(consume=lambda *_: pytest.fail("replay consumed")),
            search=lambda *_: pytest.fail("search started"),
        )
        custom = identity.context(signed_id).client_context.custom
        custom["bedrockAgentCoreMcpMessageId"] = context_id
        request = {
            "version": 1,
            "arguments": arguments,
            "custom": custom,
            "deadline_ms": int(time.time() * 1000) + 4000,
            "attempt_id": "a" * 32,
        }
        # ECS admission runs separately from Backend.handle's observer.
        if expected:
            worker.admit(
                request,
                backend="fallback",
                platform_request_id="12345678-1234-1234-1234-123456789abc",
            )
        else:
            with pytest.raises(AuthenticationError):
                worker.admit(request, backend="fallback")
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    diagnostic = [record for record in records if "binding_match" in record]
    assert bool(diagnostic) is expected
    for record in diagnostic:
        assert record["binding_match"] == "mcp_id_str_coerced"
        assert record["status"] == "ok"
        assert not ({"mcp_id", "query", "_auth_context", "signed_id", "context_id"} & record.keys())
        if route == "backend":
            assert record["attempt_id"] == "a" * 32
            assert record["backend"] == "fallback"
            assert record["aws_request_id"] == "12345678-1234-1234-1234-123456789abc"
            assert record["correlation_id"] == correlation_id(context_id)
    assert token not in json.dumps(records)
    assert binding.query not in json.dumps(records)


def test_binding_diagnostic_output_failure_does_not_change_admission(monkeypatch):
    identity = RouteIdentity(BINDING.gateway_id, BINDING.target_id, BINDING.tool_name)
    worker = Backend(
        identity=identity,
        envelope=codec(),
        replay=SimpleNamespace(consume=lambda *_: pytest.fail("replay consumed")),
        search=lambda *_: pytest.fail("search started"),
    )
    token = codec().issue(PRINCIPAL, replace(BINDING, mcp_id=1), policy_epoch=1)
    request = {
        "version": 1,
        "arguments": {"query": BINDING.query, "_auth_context": token},
        "custom": identity.context("1").client_context.custom,
        "deadline_ms": int(time.time() * 1000) + 4000,
        "attempt_id": "a" * 32,
    }

    def broken_log(_):
        raise OSError("fixture output unavailable")

    monkeypatch.setattr(ecs_backend, "emit_record", broken_log)
    assert worker.admit(request, backend="fallback").mcp_id == "1"


@pytest.mark.parametrize("context_id", [1, "1", "01"])
def test_search_log_output_failure_preserves_authentication_and_result(context_id, monkeypatch):
    token = codec().issue(PRINCIPAL, replace(BINDING, mcp_id=1), policy_epoch=1)
    arguments = {"query": BINDING.query, "_auth_context": token}
    monkeypatch.setattr(runtime, "_search", handler)

    def broken_print(*_args, **_kwargs):
        raise OSError("fixture output unavailable")

    monkeypatch.setattr("builtins.print", broken_print)
    if context_id == "01":
        with pytest.raises(AuthenticationError):
            runtime.search_handler(arguments, context(bedrockAgentCoreMcpMessageId=context_id))
    else:
        assert runtime.search_handler(
            arguments, context(bedrockAgentCoreMcpMessageId=context_id)
        ) == {"tools": [BUSINESS.descriptor]}


def _signed_claims(changes):
    token = codec().issue(PRINCIPAL, replace(BINDING, mcp_id=1), policy_epoch=1)
    header, encoded, _signature = token.split(".")
    body = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    body.update(changes)
    encoded = base64.urlsafe_b64encode(canonical_json(body)).rstrip(b"=").decode()
    signed = f"{header}.{encoded}"
    signature = (
        base64.urlsafe_b64encode(hmac.digest(Keys().current()[1], signed.encode("ascii"), "sha256"))
        .rstrip(b"=")
        .decode()
    )
    return f"{signed}.{signature}"


@pytest.mark.parametrize(
    "changes",
    [
        {"mcp_id": True},
        {"mcp_id": 1.0},
        {"mcp_id": None},
        {"mcp_id": "1"},
        {"mcp_id": 10**256},
        {"query_hash": "wrong"},
        {"gateway": "wrong"},
        {"target": "wrong"},
        {"tool": "wrong"},
        {"exp": NOW - 31, "iat": NOW - 61},
        {"exp": NOW + 61, "iat": NOW + 31},
        {"nonce": ""},
        {"principal_exp": NOW},
        {"epoch": True},
    ],
)
@pytest.mark.parametrize("route", ["search", "backend"])
def test_coercion_never_bypasses_other_claims_or_logs_false_success(
    changes, route, monkeypatch, capsys
):
    arguments = {"query": BINDING.query, "_auth_context": _signed_claims(changes)}
    # A signed string must never match an integer context, even with a valid HMAC.
    context_id = 1 if changes.get("mcp_id") == "1" else "1"
    if route == "search":
        monkeypatch.setattr(runtime, "_search", handler)
        with pytest.raises(AuthenticationError):
            runtime.search_handler(arguments, context(bedrockAgentCoreMcpMessageId=context_id))
    else:
        identity = RouteIdentity(BINDING.gateway_id, BINDING.target_id, BINDING.tool_name)
        worker = Backend(
            identity=identity,
            envelope=codec(),
            replay=SimpleNamespace(consume=lambda *_: pytest.fail("replay consumed")),
            search=lambda *_: pytest.fail("search started"),
        )
        request = {
            "version": 1,
            "arguments": arguments,
            "custom": identity.context(context_id).client_context.custom,
            "deadline_ms": int(time.time() * 1000) + 4000,
            "attempt_id": "a" * 32,
        }
        with pytest.raises(AuthenticationError):
            worker.admit(request, backend="fallback")
    output = capsys.readouterr().out
    assert "binding_match" not in output
    assert arguments["_auth_context"] not in output
    assert BINDING.query not in output


@pytest.mark.parametrize("signed_id", [0, 1, -1, 10**256 - 1, -(10**255 - 1)])
def test_codec_accepts_only_canonical_one_way_integer_text(signed_id):
    binding = replace(BINDING, mcp_id=signed_id)
    token = codec().issue(PRINCIPAL, binding, policy_epoch=1)
    assert codec().verify(token, replace(binding, mcp_id=str(signed_id))).principal == PRINCIPAL
    with pytest.raises(AuthenticationError):
        EnvelopeCodec(Keys(), clock=lambda: NOW + 61).verify(
            token, replace(binding, mcp_id=str(signed_id))
        )


def test_binding_match_diagnostic_does_not_leak_between_requests(monkeypatch, capsys):
    monkeypatch.setattr(runtime, "_search", handler)
    token = codec().issue(PRINCIPAL, replace(BINDING, mcp_id=1), policy_epoch=1)
    arguments = {"query": BINDING.query, "_auth_context": token}
    for context_id in ("1", 1, "01"):
        if context_id == "01":
            with pytest.raises(AuthenticationError):
                runtime.search_handler(arguments, context(bedrockAgentCoreMcpMessageId=context_id))
        else:
            runtime.search_handler(arguments, context(bedrockAgentCoreMcpMessageId=context_id))
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert ["binding_match" in record for record in records] == [True, False, False]


def test_runtime_only_suppresses_output_errors():
    with pytest.raises(TypeError):
        runtime._observed("request", lambda: {"ok": True}, fields={"bug": object()})

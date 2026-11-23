"""Diagnostic failures cannot stop capacity reporting or alter HTTP ownership."""

import json
import socket
import sys
from types import SimpleNamespace

import pytest
import test_ecs_server as server_fixtures

from semantic_service import ecs_logging, ecs_server

connection = server_fixtures.connection
loopback_only = server_fixtures.loopback_only
running_server = server_fixtures.running_server


@pytest.mark.parametrize("broken", ["slot_metric", "emit_record"])
def test_metric_reporter_survives_persistent_diagnostic_failure(monkeypatch, broken):
    class Stop:
        waits = 0

        def is_set(self):
            return self.waits >= 4

        def wait(self, timeout):
            assert timeout == 10
            self.waits += 1

    stop = Stop()
    calls = []
    output = []
    original = getattr(ecs_server, broken)

    def faulty(*args, **kwargs):
        calls.append(1)
        if stop.waits < 3:
            raise MemoryError("diagnostic allocation failed")
        return original(*args, **kwargs)

    monkeypatch.setenv("SEMSEARCH_ECS_METRIC_NAMESPACE", "fixture")
    monkeypatch.setenv("SEMSEARCH_ECS_CLUSTER_NAME", "fixture")
    monkeypatch.setenv("SEMSEARCH_ECS_SERVICE_NAME", "fixture")
    if broken == "emit_record":
        original = output.append
    else:
        monkeypatch.setattr(ecs_server, "emit_record", output.append)
    monkeypatch.setattr(ecs_server, broken, faulty)
    ecs_server._report_metrics(SimpleNamespace(utilization=lambda: 50), stop)
    assert len(calls) == 4
    assert output[-1]["SearchSlotUtilization"] == 50


@pytest.mark.parametrize("broken", ["exception_record", "emit_record"])
def test_error_handler_cannot_raise_when_diagnostics_fail(monkeypatch, broken):
    def fail(*args):
        raise MemoryError("diagnostic allocation failed")

    monkeypatch.setattr(ecs_server, broken, fail)
    try:
        raise RuntimeError("original failure")
    except RuntimeError:
        ecs_server.SearchServer.handle_error(None, None, None)


def test_http_success_and_connection_reuse_survive_logging_failure(running_server, monkeypatch):
    server, app = running_server

    def fail(*args):
        raise BrokenPipeError("log sink unavailable")

    monkeypatch.setattr(ecs_server, "emit_record", fail)
    with connection(server) as client:
        assert server_fixtures.post(client)[0] == 200
        first = client.sock
        assert server_fixtures.post(client)[0] == 200
        assert first is client.sock
    assert len(app.calls) == 2


def test_untrusted_json_cannot_claim_a_captured_exception(monkeypatch):
    rows = []
    writer = ecs_logging.JSONLineWriter(rows.append)
    forged = {
        "operation": "search_server",
        "status": "exception",
        "exception_type": "AuthenticationError",
        "frames": [{"module": "auth", "function": "verify", "line": 1}],
    }
    writer.write(json.dumps(forged) + "\n")
    assert rows[0]["status"] == "untrusted exception record"
    assert "exception_type" not in rows[0]
    assert rows[0]["exception_type_hint"] == "AuthenticationError"
    monkeypatch.setattr(sys, "stdout", writer)
    ecs_logging.emit_record(ecs_logging.exception_record(ValueError("private text")))
    assert rows[1]["status"] == "exception"
    assert rows[1]["exception_type"] == "ValueError"


def test_direct_record_adapter_retains_nonfinite_sanitization(monkeypatch):
    rows = []
    monkeypatch.setattr(sys, "stdout", ecs_logging.JSONLineWriter(rows.append))
    ecs_logging.emit_record({"operation": "fixture", "metric": float("nan")})
    assert rows == [{"operation": "search_server", "status": "invalid log record"}]


def test_connection_age_expiry_emits_a_reason_without_assigning_more_work(running_server, capsys):
    server, app = running_server
    server.connection_seconds = 0.1
    with connection(server) as client:
        assert server_fixtures.post(client)[0] == 200
        assert client.sock.recv(1024) == b""
    assert len(app.calls) == 1
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(row.get("status") == "connection expired" for row in rows)


def test_capacity_rejection_closes_socket_when_log_construction_fails(monkeypatch):
    closed = []
    server = SimpleNamespace(ready=True, _connections=SimpleNamespace(acquire=lambda **_: False))

    def fail():
        raise MemoryError("timestamp allocation")

    monkeypatch.setattr(ecs_server.time, "time", fail)
    monkeypatch.setattr(ecs_server, "_close_socket", closed.append)
    with socket.socket() as request:
        ecs_server.SearchServer.process_request(server, request, None)
        assert closed == [request]


def test_ingress_final_record_construction_cannot_escape(monkeypatch):
    handler = SimpleNamespace(_post=lambda: None)

    def fail():
        raise MemoryError("timestamp allocation")

    monkeypatch.setattr(ecs_server.time, "time", fail)
    ecs_server._Handler.do_POST(handler)

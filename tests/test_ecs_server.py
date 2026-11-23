"""Exercise real loopback TLS sockets without enabling external network access."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from semantic_service.auth import AuthenticationError
from semantic_service.ecs_backend import Admission
from semantic_service.ecs_server import SearchServer, healthcheck, make_tls_context

_CONNECT = socket.socket.connect
_GETADDRINFO = socket.getaddrinfo


@pytest.fixture
def loopback_only(monkeypatch):
    def connect(sock, address):
        assert address[0] == "127.0.0.1", "socket tests allow only loopback"
        return _CONNECT(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        assert host == "127.0.0.1", "socket tests allow only loopback"
        return _GETADDRINFO(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


class App:
    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False

    def admit(self, body, *, authorization):
        if authorization != "Bearer fixture":
            raise AuthenticationError()
        return Admission(body, "fixture", "ecs", body["deadline_ms"], time.monotonic() + 4)

    def execute(self, admitted):
        return self.handle(admitted.request, authorization="Bearer fixture", backend="ecs")

    def healthy(self):
        return True

    def handle(self, body, *, authorization, backend):
        self.calls.append((body, authorization, backend))
        self.entered.set()
        if self.block:
            assert self.release.wait(2)
        return {"version": 1, "ok": True, "result": {"tools": []}}


@pytest.fixture
def running_server(loopback_only, tmp_path):
    app = App()
    server = SearchServer(
        ("127.0.0.1", 0),
        app,
        make_tls_context(tmp_path),
        max_concurrent=1,
        max_connections=8,
        socket_seconds=0.3,
        request_seconds=1,
        body_seconds=0.5,
        health_address=("127.0.0.1", 0),
    )
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    yield server, app
    app.release.set()
    server.graceful_stop(timeout=2)
    thread.join(2)
    assert not thread.is_alive()


def connection(server):
    return closing(
        http.client.HTTPSConnection(
            "127.0.0.1",
            server.server_port,
            timeout=2,
            # only the loopback fixture server.
            context=ssl._create_unverified_context(),  # nosec B323
        )
    )


def body():
    return json.dumps(
        {
            "version": 1,
            "arguments": {"query": "find tools", "_auth_context": "fixture"},
            "custom": {},
            "deadline_ms": int(time.time() * 1000) + 4000,
            "attempt_id": "a" * 32,
        }
    )


def post(client, payload=None, headers=None, path="/search"):
    client.request(
        "POST",
        path,
        body=body() if payload is None else payload,
        headers=headers or {"Content-Type": "application/json", "Authorization": "Bearer fixture"},
    )
    response = client.getresponse()
    return response.status, json.loads(response.read())


def test_tls_health_readiness_and_reused_search_connection(running_server):
    server, app = running_server
    with connection(server) as client:
        assert post(client) == (200, {"version": 1, "ok": True, "result": {"tools": []}})
        first_socket = client.sock
        assert post(client)[0] == 200
        assert client.sock is first_socket
    assert len(app.calls) == 2
    assert app.calls[0][1:] == ("Bearer fixture", "ecs")


@pytest.mark.parametrize(
    ("payload", "headers", "path", "expected"),
    [
        ("{}", {"Content-Type": "text/plain"}, "/search", 400),
        ("{", {"Content-Type": "application/json"}, "/search", 400),
        ("x" * 32769, {"Content-Type": "application/json"}, "/search", 413),
        (
            "{}",
            {"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
            "/search",
            400,
        ),
        ("{}", {"Content-Type": "application/json", "Expect": "100-continue"}, "/search", 400),
        ("{}", {"Content-Type": "application/json"}, "/other", 404),
    ],
    ids=["media-type", "malformed", "large", "chunked", "expect", "path"],
)
def test_invalid_http_requests_never_reach_backend(
    running_server, payload, headers, path, expected
):
    server, app = running_server
    with connection(server) as client:
        status, result = post(client, payload, headers, path)
    assert status == expected
    assert result["retryable"] is False
    assert app.calls == []


def test_duplicate_content_length_is_rejected_before_reading_body(running_server):
    server, app = running_server
    with connection(server) as client:
        client.putrequest("POST", "/search")
        client.putheader("Content-Type", "application/json")
        client.putheader("Content-Length", "2")
        client.putheader("Content-Length", "5")
        client.endheaders(b"{}")
        response = client.getresponse()
        assert response.status == 400
        response.read()
    assert not app.calls


def test_capacity_returns_retryable_and_health_remains_available(running_server):
    server, app = running_server
    app.block = True
    results = []

    def first():
        with connection(server) as client:
            results.append(post(client))

    thread = threading.Thread(target=first)
    thread.start()
    assert app.entered.wait(1)
    with connection(server) as client:
        assert post(client) == (
            503,
            {"version": 1, "ok": False, "error": "capacity", "retryable": True},
        )
    with connection(server.health_server) as client:
        client.request("GET", "/ready")
        response = client.getresponse()
        assert response.status == 200
        response.read()
    app.release.set()
    thread.join(2)
    assert results[0][0] == 200
    assert len(app.calls) == 1


def test_slow_tls_handshake_does_not_block_another_connection(running_server):
    server, _app = running_server
    stalled = socket.socket()
    stalled.connect(("127.0.0.1", server.server_port))
    try:
        started = time.monotonic()
        with connection(server.health_server) as client:
            client.request("GET", "/ready")
            response = client.getresponse()
            assert response.status == 200
            response.read()
        assert time.monotonic() - started < 0.7
    finally:
        stalled.close()


def test_partial_body_times_out_and_does_not_reach_backend(running_server):
    server, app = running_server
    with connection(server) as client:
        client.putrequest("POST", "/search")
        client.putheader("Content-Type", "application/json")
        client.putheader("Content-Length", "200")
        client.endheaders(b"{")
        started = time.monotonic()
        response = client.getresponse()
        assert response.status == 408
        response.read()
        assert time.monotonic() - started < 1
    assert app.calls == []


def test_graceful_stop_drains_admitted_work_and_readiness_drops(running_server):
    server, app = running_server
    app.block = True
    results = []

    def first():
        with connection(server) as client:
            results.append(post(client))

    thread = threading.Thread(target=first)
    thread.start()
    assert app.entered.wait(1)
    server.ready = False
    with connection(server.health_server) as client:
        client.request("GET", "/ready")
        response = client.getresponse()
        assert response.status == 503
        response.read()
    stopping = threading.Thread(target=server.graceful_stop, kwargs={"timeout": 2})
    stopping.start()
    app.release.set()
    thread.join(2)
    stopping.join(2)
    assert results[0][0] == 200
    assert not stopping.is_alive()


def test_generated_tls_key_is_removed_after_ssl_context_load(tmp_path):
    context = make_tls_context(tmp_path)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert list(tmp_path.iterdir()) == []


def test_default_tls_key_directory_does_not_follow_environment(monkeypatch, tmp_path):
    from semantic_service import ecs_server

    created = []
    original = ecs_server.tempfile.TemporaryDirectory

    def record_directory(*args, **kwargs):
        directory = original(*args, **kwargs)
        created.append(Path(directory.name))
        return directory

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(ecs_server.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(ecs_server.tempfile, "TemporaryDirectory", record_directory)
    make_tls_context()
    assert len(created) == 1
    assert created[0].parent.resolve() == Path("/tmp").resolve()  # nosec B108
    assert not created[0].exists()
    assert list(tmp_path.iterdir()) == []


def test_startup_keeps_search_and_health_on_all_task_interfaces(monkeypatch):
    import ipaddress
    from types import SimpleNamespace

    from semantic_service import ecs_server

    addresses = {}

    def record_server(address, app, context, **kwargs):
        addresses["search"] = address
        addresses["health"] = kwargs["health_address"]
        raise RuntimeError("captured listeners before opening sockets")

    monkeypatch.setenv("SEMSEARCH_ECS_BIND_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("SEMSEARCH_ECS_PORT", "8443")
    monkeypatch.setenv("SEMSEARCH_ECS_HEALTH_PORT", "9443")
    monkeypatch.setattr(ecs_server.sys, "argv", ["ecs-server"])
    monkeypatch.setattr(ecs_server.signal, "signal", lambda *args: None)
    monkeypatch.setattr(ecs_server, "install_stdout", lambda: None)
    monkeypatch.setattr(
        ecs_server, "ProcessApplication", lambda **kwargs: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(ecs_server, "make_tls_context", lambda: None)
    monkeypatch.setattr(ecs_server, "SearchServer", record_server)
    with pytest.raises(RuntimeError, match="captured listeners"):
        ecs_server.main()
    assert addresses["search"][0] == addresses["health"][0]
    assert ipaddress.ip_address(addresses["search"][0]).is_unspecified
    assert addresses["search"][1] == 8443
    assert addresses["health"][1] == 9443


def test_healthcheck_command_uses_real_local_tls_probe(running_server):
    server, _app = running_server
    assert healthcheck(server.health_server.server_port) == 0
    server.ready = False
    assert healthcheck(server.health_server.server_port) == 1


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("invalid input", 400),
        ("unauthenticated", 401),
        ("unauthorized", 403),
        ("replay rejected", 409),
        ("rate limited", 429),
        ("index not ready", 503),
        ("internal failure", 503),
        ("replay unavailable", 503),
        ("deadline exceeded", 503),
    ],
)
def test_backend_errors_keep_fixed_http_and_business_semantics(running_server, kind, status):
    server, app = running_server
    expected = {"version": 1, "ok": False, "error": kind, "retryable": False}
    app.handle = lambda *args, **kwargs: expected
    with connection(server) as client:
        assert post(client) == (status, expected)


def test_container_runs_existing_locked_artifact_without_installing_dependencies():
    dockerfile = Path("containers/search/Dockerfile").read_text()
    assert "COPY .artifacts/lambda.zip " in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3" in dockerfile
    assert 'CMD ["python", "-m", "semantic_service.ecs_health"]' in dockerfile
    assert "EXPOSE 8443 9443" in dockerfile
    assert "pip install" not in dockerfile
    assert 'ENTRYPOINT ["python", "-m", "semantic_service.ecs_server"]' in dockerfile


def test_public_port_does_not_expose_health(running_server):
    server, _app = running_server
    for path in ("/health", "/ready"):
        with connection(server) as client:
            client.request("GET", path)
            response = client.getresponse()
            assert response.status == 404
            response.read()


def test_partial_unauthenticated_bodies_do_not_take_search_slots(running_server):
    server, app = running_server
    with connection(server) as slow, connection(server) as valid:
        slow.putrequest("POST", "/search")
        slow.putheader("Content-Type", "application/json")
        slow.putheader("Content-Length", "32000")
        slow.endheaders(b"{")
        assert post(valid)[0] == 200
    assert len(app.calls) == 1


def test_failed_authentication_never_takes_search_permit(running_server):
    server, app = running_server
    assert server.capacity.acquire(blocking=False)
    try:
        with connection(server) as client:
            assert (
                post(
                    client,
                    headers={"Content-Type": "application/json", "Authorization": "Bearer invalid"},
                )[0]
                == 401
            )
    finally:
        server.capacity.release()
    assert not app.calls


def test_connection_exhaustion_logs_and_cannot_starve_health(running_server, capsys):
    server, _app = running_server
    held = []
    try:
        while server._connections.acquire(blocking=False):
            held.append(1)
        with connection(server) as client:
            with pytest.raises((OSError, http.client.HTTPException)):
                post(client)
        assert healthcheck(server.health_server.server_port) == 0
    finally:
        for _ in held:
            server._connections.release()
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(row.get("status") == "connection capacity" for row in rows)


def test_dripping_body_has_wall_clock_limit(running_server):
    server, app = running_server
    with connection(server) as client:
        client.putrequest("POST", "/search")
        client.putheader("Content-Type", "application/json")
        client.putheader("Content-Length", "32000")
        client.endheaders(b"{")
        started = time.monotonic()
        for _ in range(20):
            try:
                client.send(b" ")
            except OSError:
                break
            time.sleep(0.05)
        assert time.monotonic() - started < 0.85
    assert not app.calls


def test_health_module_is_lightweight():
    source = Path("src/semantic_service/ecs_health.py").read_text()
    assert "ecs_backend" not in source and "cryptography" not in source


class ProcessAuth:
    def admit(self, request, *, authorization):
        if authorization != "Bearer fixture":
            raise AuthenticationError()
        return Admission(
            request,
            "fixture",
            "ecs",
            request["deadline_ms"],
            time.monotonic() + max(0, request["deadline_ms"] / 1000 - time.time()),
        )


class ProcessSearch:
    def execute(self, admitted):
        if admitted.request["arguments"]["query"] == "wait":
            while True:
                time.sleep(0.01)
        return {"version": 1, "ok": True, "result": {"tools": []}}


def process_auth_factory():
    return ProcessAuth()


def process_search_factory():
    return ProcessSearch()


def test_http_wall_deadline_reclaims_real_process_and_restores_capacity(loopback_only, tmp_path):
    import os

    from semantic_service.ecs_workers import ProcessApplication, WorkerPool

    app = object.__new__(ProcessApplication)
    app.admission = WorkerPool(process_auth_factory, size=1, startup_seconds=5)
    app.search = WorkerPool(process_search_factory, size=1, startup_seconds=5)
    server = SearchServer(
        ("127.0.0.1", 0),
        app,
        make_tls_context(tmp_path),
        max_concurrent=1,
        max_connections=8,
        socket_seconds=0.15,
        request_seconds=0.75,
        body_seconds=0.1,
        health_address=("127.0.0.1", 0),
    )
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    serving.start()
    original_pid = app.search._slots[0].process.pid

    def request():
        value = json.loads(body())
        value["arguments"]["query"] = "wait"
        value["deadline_ms"] = int(time.time() * 1000) + 2000
        with connection(server) as client:
            try:
                post(client, json.dumps(value))
            except (OSError, http.client.HTTPException):
                pass

    waiting = threading.Thread(target=request)
    try:
        waiting.start()
        for _ in range(100):
            if app.utilization() == 100:
                break
            time.sleep(0.005)
        assert app.utilization() == 100
        time.sleep(0.95)
        with pytest.raises(ProcessLookupError):
            os.kill(original_pid, 0)
        app.search.wait_ready(5)
        waiting.join(2)
        assert not waiting.is_alive()
        assert healthcheck(server.health_server.server_port) == 0
        with connection(server) as client:
            assert post(client)[0] == 200
    finally:
        server.graceful_stop(timeout=1)
        serving.join(2)
        app.close()
        waiting.join(2)


def test_ingress_records_whether_execution_was_assigned(running_server, capsys):
    server, _app = running_server
    with connection(server) as client:
        assert (
            post(
                client,
                headers={"Content-Type": "application/json", "Authorization": "Bearer invalid"},
            )[0]
            == 401
        )
    with connection(server) as client:
        assert post(client)[0] == 200
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    rows = [row for row in records if row.get("operation") == "search_ingress"]
    assert [(row["status"], row["execution_assigned"]) for row in rows] == [
        ("unauthenticated", False),
        ("ok", True),
    ]

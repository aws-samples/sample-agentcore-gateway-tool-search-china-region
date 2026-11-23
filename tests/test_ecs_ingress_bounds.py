"""Header bytes, lines and elapsed time are bounded before materialization."""

import http.client
import json
import socket
import ssl
import threading
import time

import pytest
import test_ecs_server as server_fixtures

connection = server_fixtures.connection
loopback_only = server_fixtures.loopback_only
running_server = server_fixtures.running_server


@pytest.mark.parametrize(
    "headers",
    [
        b"X-Field: " + b"x" * 18000 + b"\r\n",
        b"X-Field: " + b"x" * 1024 + b"\r\n" + (b"X-Field: " + b"x" * 1024 + b"\r\n") * 20,
        b"X-Field: x\r\n" * 65,
    ],
    ids=["single-line", "aggregate", "line-count"],
)
def test_headers_are_rejected_before_backend_and_without_waiting_for_body(running_server, headers):
    server, app = running_server
    with connection(server) as client:
        client.connect()
        client.sock.sendall(
            b"POST /search HTTP/1.1\r\n"
            b"Host: fixture\r\nContent-Type: application/json\r\n"
            b"Content-Length: 999\r\n" + headers + b"\r\n"
        )
        response = http.client.HTTPResponse(client.sock)
        response.begin()
        assert response.status == 431
        response.read()
    assert not app.calls


def test_dripping_headers_have_a_wall_clock_limit(running_server):
    server, app = running_server
    server.header_seconds = 0.12
    # literal loopback below.
    context = ssl._create_unverified_context()  # nosec B323
    raw = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
    with context.wrap_socket(raw, server_hostname="fixture") as sock:
        sock.sendall(b"POST /search HTTP/1.1\r\nX-Slow: ")
        stopped = threading.Event()

        def drip():
            while not stopped.wait(0.025):
                try:
                    sock.sendall(b"x")
                except OSError:
                    return

        thread = threading.Thread(target=drip)
        thread.start()
        started = time.monotonic()
        try:
            try:
                received = sock.recv(4096)
            except (ConnectionError, ssl.SSLError):
                received = b""
            assert received == b""
            assert time.monotonic() - started < 0.35
        finally:
            stopped.set()
            thread.join(1)
    assert not app.calls


def test_health_and_ready_both_reject_an_unavailable_application(running_server):
    server, app = running_server
    app.healthy = lambda: False
    for path in ("/health", "/ready"):
        with connection(server.health_server) as client:
            client.request("GET", path)
            response = client.getresponse()
            assert response.status == 503
            response.read()


def test_healthy_probes_keep_their_existing_success_payloads(running_server):
    server, _app = running_server
    for path, status in (("/health", "ok"), ("/ready", "ready")):
        with connection(server.health_server) as client:
            client.request("GET", path)
            response = client.getresponse()
            assert response.status == 200
            assert json.loads(response.read()) == {"status": status}


def test_header_reader_never_requests_more_than_remaining_byte_budget():
    import io

    from semantic_service.ecs_server import _HeaderReader

    class RecordingStream(io.BytesIO):
        requested = []

        def readline(self, size=-1):
            self.requested.append(size)
            return super().readline(size)

    stream = RecordingStream(b"X: " + b"a" * 1000000)
    reader = _HeaderReader(stream)
    with pytest.raises(http.client.LineTooLong):
        reader.readline(65537)
    assert stream.requested == [16385]
    assert stream.tell() == 16385


def test_reused_connection_is_closed_after_a_bounded_number_of_requests(running_server):
    server, _app = running_server
    server.max_requests = 2
    with connection(server) as client:
        assert server_fixtures.post(client)[0] == 200
        first = client.sock
        assert first is not None
        assert server_fixtures.post(client)[0] == 200
        assert client.sock is None


def test_active_connection_cannot_extend_its_total_age_indefinitely(running_server):
    server, _app = running_server
    server.connection_seconds = 0.2
    with connection(server) as client:
        assert server_fixtures.post(client)[0] == 200
        sock = client.sock
        assert sock is not None
        started = time.monotonic()
        # Keep activity below per-request and idle timeouts.
        time.sleep(0.08)
        assert server_fixtures.post(client)[0] == 200
        time.sleep(0.08)
        assert server_fixtures.post(client)[0] == 200
        try:
            received = sock.recv(4096)
        except (ConnectionError, ssl.SSLError):
            received = b""
        assert received == b""
        assert time.monotonic() - started < 0.35


def test_request_accepted_near_connection_age_limit_can_finish(running_server):
    from concurrent.futures import ThreadPoolExecutor

    server, app = running_server
    server.connection_seconds = 0.2
    with connection(server) as client:
        assert server_fixtures.post(client)[0] == 200
        app.block = True
        app.entered.clear()
        time.sleep(0.1)
        with ThreadPoolExecutor(max_workers=1) as threads:
            response = threads.submit(server_fixtures.post, client)
            assert app.entered.wait(0.2)
            time.sleep(0.15)
            app.release.set()
            assert response.result(timeout=1)[0] == 200
        assert client.sock is None

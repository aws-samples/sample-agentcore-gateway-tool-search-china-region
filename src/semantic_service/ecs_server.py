"""Bounded TLS service for the private ECS Search transport."""

from __future__ import annotations

import argparse
import http.client
import io
import ipaddress
import json
import os
import signal
import socket
import ssl
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Protocol, cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from semantic_service.auth import ServiceError
from semantic_service.ecs_backend import (
    Admission,
    ClockSkew,
    DeadlineExceeded,
    ReplayUnavailable,
    failure,
)
from semantic_service.ecs_health import healthcheck
from semantic_service.ecs_logging import emit_record, exception_record, install_stdout, slot_metric
from semantic_service.ecs_workers import ProcessApplication
from semantic_service.transport import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    decode_request,
    encode_json,
)


class Application(Protocol):
    def admit(self, request: object, *, authorization: str | None = None) -> object: ...
    def execute(self, admitted: Admission) -> dict[str, object]: ...
    def healthy(self) -> bool: ...


def _emit(record: dict[str, object] | BaseException | Callable[[], dict[str, object]]) -> None:
    """Diagnostics, including record construction, never own HTTP resources."""
    try:
        if isinstance(record, BaseException):
            record = exception_record(record)
        elif callable(record):
            record = record()
        emit_record(record)
    # Diagnostic failure must not strand the socket or replace the request result.
    except Exception:  # nosec B110
        pass


def _report_metrics(application: ProcessApplication, stopping: threading.Event) -> None:
    while not stopping.is_set():
        _emit(
            lambda: slot_metric(
                application.utilization(),
                namespace=os.environ["SEMSEARCH_ECS_METRIC_NAMESPACE"],
                cluster=os.environ["SEMSEARCH_ECS_CLUSTER_NAME"],
                service=os.environ["SEMSEARCH_ECS_SERVICE_NAME"],
            )
        )
        stopping.wait(10)


def make_tls_context(directory: Path | None = None) -> ssl.SSLContext:
    """Encrypt NLB-to-task traffic using a process-local ephemeral certificate.

    NLB TLS target groups do not validate this certificate. Task ingress is
    restricted to NLB security groups. The listener's reachability is controlled
    by infrastructure. OAuth and REQUEST signatures authenticate callers and
    requests, not the backend response.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "search.internal")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("search.internal"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(["http/1.1"])
    # SSLContext retains parsed material. Delete files immediately after load;
    # never bake a reusable private key into the image or write the root volume.
    # The task mounts /tmp as a private uid-10001 volume; ignore TMPDIR/TEMP/TMP.
    with tempfile.TemporaryDirectory(
        prefix="search-tls-",
        dir=directory or "/tmp",  # nosec B108
    ) as scratch:
        cert_path, key_path = Path(scratch) / "cert.pem", Path(scratch) / "key.pem"
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        key_path.chmod(0o600)
        context.load_cert_chain(cert_path, key_path)
    return context


def _close_socket(connection: socket.socket) -> None:
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.close()


class _HeaderReader(io.BufferedIOBase):
    """Bound request line and headers before http.client materializes them."""

    def __init__(self, stream: io.BufferedIOBase) -> None:
        super().__init__()
        self.stream = stream
        self.remaining = 16384
        self.lines = 0

    def readline(self, size: int | None = -1) -> bytes:
        if self.lines >= 64:
            raise http.client.HTTPException("header line limit")
        allowed = self.remaining + 1
        line = self.stream.readline(allowed if size is None or size < 0 else min(size, allowed))
        self.lines += 1
        self.remaining -= len(line)
        if self.remaining < 0:
            raise http.client.LineTooLong("request header")
        return line


class SearchServer(ThreadingMixIn, HTTPServer):
    """Bound sockets separately from Search work so health checks remain usable."""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(
        self,
        address: tuple[str, int],
        application: Application,
        tls: ssl.SSLContext,
        *,
        max_concurrent: int = 4,
        max_connections: int = 128,
        socket_seconds: float = 2.0,
        request_seconds: float = 6.0,
        body_seconds: float = 1.0,
        header_seconds: float | None = None,
        connection_seconds: float = 60.0,
        max_requests: int = 100,
        health_address: tuple[str, int] | None = None,
        health_only: bool = False,
        readiness: Callable[[], bool] | None = None,
    ) -> None:
        header_seconds = min(1.0, request_seconds) if header_seconds is None else header_seconds
        if (
            type(max_concurrent) is not int
            or type(max_connections) is not int
            or not 1 <= max_concurrent < max_connections <= 512
            or not 0 < socket_seconds <= request_seconds <= 10
            or not 0 < body_seconds <= request_seconds
            or not 0 < header_seconds <= request_seconds
            or not 0 < connection_seconds <= 300
            or type(max_requests) is not int
            or not 1 <= max_requests <= 1000
        ):
            raise ValueError("invalid bounded server configuration")
        self.application, self.tls = application, tls
        self.socket_seconds, self.request_seconds = socket_seconds, request_seconds
        self.body_seconds, self.health_only = body_seconds, health_only
        self.header_seconds = header_seconds
        self.connection_seconds, self.max_requests = connection_seconds, max_requests
        self._readiness = readiness
        self.health_server: SearchServer | None = None
        self._health_thread: threading.Thread | None = None
        self.capacity = threading.BoundedSemaphore(max_concurrent)
        self._connections = threading.BoundedSemaphore(max_connections)
        self._active: set[socket.socket] = set()
        self._condition = threading.Condition()
        self._stop_lock = threading.Lock()
        self._stopped = False
        self.ready = True
        super().__init__(address, _Handler)
        if health_address is not None:
            self.health_server = SearchServer(
                health_address,
                application,
                tls,
                max_concurrent=1,
                max_connections=8,
                socket_seconds=socket_seconds,
                request_seconds=request_seconds,
                body_seconds=body_seconds,
                header_seconds=header_seconds,
                connection_seconds=connection_seconds,
                max_requests=max_requests,
                health_only=True,
                # The health listener receives a callback, invoked at probe time.
                readiness=self.is_ready,  # nosemgrep: is-function-without-parentheses
            )
            self._health_thread = threading.Thread(
                target=self.health_server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
            )
            self._health_thread.start()

    def is_ready(self) -> bool:
        if self._readiness is not None:
            return self._readiness()
        return self.ready and self.application.healthy()

    def server_bind(self) -> None:
        # HTTPServer's default reverse DNS lookup is unnecessary for this service.
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name, self.server_port = "search.internal", self.server_address[1]

    def process_request(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        if not isinstance(request, socket.socket):
            raise ValueError("TCP request required")
        if not self._connections.acquire(blocking=False):
            _emit(
                lambda: {
                    "operation": "search_server",
                    "status": "connection capacity",
                    "timestamp_ms": int(time.time() * 1000),
                }
            )
            _close_socket(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._connections.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        if not isinstance(request, socket.socket):
            raise ValueError("TCP request required")
        try:
            request.settimeout(self.socket_seconds)
            connection = self.tls.wrap_socket(
                request, server_side=True, do_handshake_on_connect=False
            )
            with self._condition:
                self._active.add(connection)
            try:
                connection.do_handshake()
                self.finish_request(connection, client_address)
            finally:
                with self._condition:
                    self._active.discard(connection)
                    self._condition.notify_all()
                _close_socket(connection)
        except (OSError, ValueError):
            # TLS/peer errors never expose socket addresses or bearer material.
            pass
        finally:
            self._connections.release()
            self.shutdown_request(request)

    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exception()
        if error is not None:
            _emit(error)
        _emit({"operation": "search_server", "status": "internal failure"})

    def graceful_stop(self, timeout: float = 10.0) -> None:
        """Called outside serve_forever: stop admission, then bound draining."""
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            self.ready = False
            self.shutdown()
            self.server_close()
            deadline = time.monotonic() + max(0.0, min(timeout, 20.0))
            with self._condition:
                while self._active and time.monotonic() < deadline:
                    self._condition.wait(timeout=max(0.0, deadline - time.monotonic()))
                remaining = tuple(self._active)
            for connection in remaining:
                _close_socket(connection)
            if self.health_server is not None:
                self.health_server.graceful_stop(timeout=0)
                if self._health_thread is not None:
                    self._health_thread.join(1)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Search"
    sys_version = ""
    server: SearchServer

    def log_message(self, format: str, *args: object) -> None:
        # Default HTTP logs contain request paths and peer data supplied by clients.
        pass

    def setup(self) -> None:
        super().setup()
        self._connection_end = time.monotonic() + self.server.connection_seconds

    def handle_one_request(self) -> None:
        # A total timer also bounds a peer that drips bytes faster than the idle
        # socket timeout. It runs per HTTP request, preserving connection reuse.
        # A soft connection age stops new headers. Once headers are accepted,
        # the existing request ceiling permits completion, bounding total
        # lifetime to connection_seconds + request_seconds after TLS.
        remaining_age = self._connection_end - time.monotonic()
        if remaining_age <= 0:
            _emit({"operation": "search_server", "status": "connection expired"})
            self.close_connection = True
            return
        self._request_count = getattr(self, "_request_count", 0) + 1
        self._request_end = time.monotonic() + self.server.request_seconds
        timer = threading.Timer(self.server.request_seconds, _close_socket, (self.connection,))
        timer.daemon = True
        timer.start()
        self._body_stream = self.rfile
        self.rfile = _HeaderReader(self._body_stream)
        self.requestline, self.request_version, self.command = "", "HTTP/1.1", ""

        def expire_headers() -> None:
            if remaining_age <= self.server.header_seconds:
                _emit({"operation": "search_server", "status": "connection expired"})
            _close_socket(self.connection)

        self._header_timer = threading.Timer(
            min(self.server.header_seconds, remaining_age), expire_headers
        )
        self._header_timer.daemon = True
        self._header_timer.start()
        try:
            super().handle_one_request()
        except http.client.HTTPException:
            self.send_error(431)
        except (OSError, ValueError):
            self.close_connection = True
        finally:
            self._finish_headers()
            timer.cancel()

    def _finish_headers(self) -> None:
        self.rfile = self._body_stream
        self._header_timer.cancel()
        self._header_timer.join(0.1)

    def parse_request(self) -> bool:
        try:
            return super().parse_request()
        finally:
            # Restore normal body reads before dispatch; header limits never
            # consume or count the next request on a reused connection.
            self._finish_headers()

    def handle_expect_100(self) -> bool:
        self._reply(400, failure("invalid input"), close=True)
        return False

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        if code == 431:
            _emit({"operation": "search_server", "status": "headers too large"})
        self._reply(code, failure("invalid input"), close=True)

    def _reply(self, status: int, value: object, *, close: bool = False) -> None:
        if isinstance(value, dict):
            self._ingress_status = str(value.get("error", "ok"))
        encoded = encode_json(value)
        if len(encoded) > MAX_RESPONSE_BYTES:
            status, encoded = 500, encode_json(failure("internal failure"))
            close = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        if (
            close
            or not self.server.ready
            or self._request_count >= self.server.max_requests
            or time.monotonic() >= self._connection_end
        ):
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.wfile.flush()

    def do_GET(self) -> None:
        if not self.server.health_only:
            self._reply(404, failure("invalid input"), close=True)
        elif self.path in {"/health", "/ready"}:
            ready = self.server.is_ready()
            status = "ok" if self.path == "/health" else "ready"
            self._reply(
                200 if ready else 503,
                {"status": status if ready else "unavailable"},
            )
        else:
            self._reply(404, failure("invalid input"), close=True)

    def do_POST(self) -> None:
        started = time.monotonic()
        self._ingress_status = "internal failure"
        self._execution_assigned = False
        self._attempt: str | None = None
        self._end: float | None = None
        try:
            self._post()
        except ServiceError as error:
            self._reply(
                401 if error.message == "unauthenticated" else 503,
                failure(
                    error.message,
                    retryable=isinstance(error, (DeadlineExceeded, ReplayUnavailable, ClockSkew)),
                ),
                close=True,
            )
        except (OSError, ValueError):
            self.close_connection = True
        except Exception as error:
            _emit(error)
            self._reply(500, failure("internal failure"), close=True)
        finally:
            _emit(
                lambda: {
                    "operation": "search_ingress",
                    "phase": "end",
                    "status": self._ingress_status,
                    "execution_assigned": self._execution_assigned,
                    "attempt_id": self._attempt,
                    "timestamp_ms": int(time.time() * 1000),
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    "remaining_ms": round(max(0.0, (self._end - time.monotonic()) * 1000), 3)
                    if self._end is not None
                    else None,
                }
            )

    def _post(self) -> None:
        if self.server.health_only or self.path != "/search":
            self._reply(404, failure("invalid input"), close=True)
            return
        if not self.server.is_ready():
            self._reply(503, failure("capacity", retryable=True), close=True)
            return
        sizes = self.headers.get_all("Content-Length", [])
        auth_headers = self.headers.get_all("Authorization", [])
        if (
            len(sizes) != 1
            or len(auth_headers) > 1
            or not sizes[0].isascii()
            or not sizes[0].isdigit()
            or self.headers.get("Transfer-Encoding") is not None
            or self.headers.get("Expect") is not None
            or self.headers.get("Content-Type") != "application/json"
        ):
            self._reply(400, failure("invalid input"), close=True)
            return
        size = int(sizes[0])
        if not 0 < size <= MAX_REQUEST_BYTES:
            self._reply(413, failure("invalid input"), close=True)
            return
        body_expired = threading.Event()

        def expire_body() -> None:
            body_expired.set()
            _close_socket(self.connection)

        timer = threading.Timer(self.server.body_seconds, expire_body)
        timer.daemon = True
        timer.start()
        try:
            raw = self.rfile.read(size)
            if body_expired.is_set():
                self._ingress_status = "body timeout"
                self.close_connection = True
                return
            if len(raw) != size:
                self._reply(400, failure("invalid input"), close=True)
                return
            request = decode_request(raw)
        except TimeoutError:
            self._reply(408, failure("invalid input"), close=True)
            self._ingress_status = "body timeout"
            return
        except OSError:
            self._ingress_status = "body timeout" if body_expired.is_set() else "body disconnected"
            self.close_connection = True
            return
        except (ServiceError, ValueError, UnicodeError):
            self._reply(400, failure("invalid input"), close=True)
            return
        finally:
            timer.cancel()
            timer.join(0.1)
        self._attempt = cast(str, request["attempt_id"])
        # Propagate the HTTP wall ceiling into both process watchdogs. Closing a
        # socket alone cannot cancel work; neither admission nor Search may keep
        # the original, later transport deadline after this local cap expires.
        local_remaining = max(0.0, self._request_end - time.monotonic())
        request["deadline_ms"] = min(
            cast(int, request["deadline_ms"]), int((time.time() + local_remaining) * 1000)
        )
        admitted = self.server.application.admit(
            request, authorization=auth_headers[0] if auth_headers else None
        )
        if isinstance(admitted, dict):
            self._result(admitted)
            return
        if not isinstance(admitted, Admission):
            raise ServiceError()
        self._end = admitted.end
        # Both the complete bounded body and cryptographic authentication precede
        # this permit. Replay is consumed only after Search execution is assigned.
        if not self.server.capacity.acquire(blocking=False):
            self._reply(503, failure("capacity", retryable=True), close=True)
            return
        try:
            self._execution_assigned = True
            self._result(self.server.application.execute(admitted))
        finally:
            self.server.capacity.release()

    def _result(self, response: dict[str, object]) -> None:
        status = 200
        if response.get("ok") is not True:
            status = {
                "invalid input": 400,
                "unauthenticated": 401,
                "unauthorized": 403,
                "replay rejected": 409,
                "rate limited": 429,
            }.get(str(response.get("error")), 503)
        self._reply(status, response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    options = parser.parse_args()
    port = int(os.environ.get("SEMSEARCH_ECS_PORT", "8443"))
    health_port = int(os.environ.get("SEMSEARCH_ECS_HEALTH_PORT", "9443"))
    if not 1024 <= port <= 65535 or not 1024 <= health_port <= 65535 or port == health_port:
        raise ValueError("unprivileged TLS port required")
    if options.healthcheck:
        return healthcheck(health_port)
    # Fargate assigns the ENI per task; there is no stable address to bake into
    # the image. Private subnet + NLB-only security group bound ingress.
    # Both listeners must serve the task ENI; a loopback-only Search listener
    # would disagree with the NLB readiness listener.
    install_stdout()
    workers = int(os.environ.get("SEMSEARCH_ECS_WORKERS", "4"))
    stopping = threading.Event()
    app: ProcessApplication | None = None
    server: SearchServer | None = None

    def stop(signum: int, frame: object) -> None:
        if not stopping.is_set():
            stopping.set()
            if server is not None:
                server.ready = False
                threading.Thread(target=server.graceful_stop, daemon=True).start()

    # Own termination before the first child exists. Startup waits observe this
    # event and clean up both partially constructed pools before main exits.
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        app = ProcessApplication(workers=workers, stop_event=stopping)
        if stopping.is_set():
            return 0
        server = SearchServer(
            ("0.0.0.0", port),  # nosec B104
            app,
            make_tls_context(),
            max_concurrent=workers,
            health_address=(
                # private NLB health target and fixed loopback probe.
                "0.0.0.0",  # nosec B104
                health_port,
            ),
        )
        if stopping.is_set():
            server.ready = False
            threading.Thread(target=server.graceful_stop, daemon=True).start()

        threading.Thread(target=_report_metrics, args=(app, stopping), daemon=True).start()
        print(json.dumps({"operation": "search_server", "status": "ready"}))
        server.serve_forever(poll_interval=0.1)
        # graceful_stop drains on its separate thread; main stays alive.
        with server._stop_lock:
            pass
        return 0
    except RuntimeError:
        if stopping.is_set():
            return 0
        raise
    finally:
        stopping.set()
        if app is not None:
            app.close()


if __name__ == "__main__":
    raise SystemExit(main())

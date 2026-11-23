"""Exercise the real TLS server and facade adapter together without cloud access."""

import socket
import ssl
import threading
import time

import httpx
import pytest

from semantic_service.ecs_backend import Admission, failure
from semantic_service.ecs_server import SearchServer, make_tls_context
from semantic_service.facade import (
    AuthenticationError,
    HTTPBackend,
    Router,
    ServiceError,
    TransportUnavailable,
)
from semantic_service.transport import RouteIdentity, encode_json

_CONNECT = socket.socket.connect
_GETADDRINFO = socket.getaddrinfo


class Tokens:
    def token(self, context):
        return "test-token"


class Application:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def healthy(self):
        return True

    def admit(self, request, **kwargs):
        self.calls.append((request, kwargs))
        return Admission(request, "original", "ecs", request["deadline_ms"], time.monotonic() + 4)

    def execute(self, admitted):
        return self.result


@pytest.fixture
def backend_server(tmp_path, monkeypatch):
    servers = []

    def connect(sock, address):
        assert address[0] == "127.0.0.1"
        return _CONNECT(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        assert host == "127.0.0.1"
        return _GETADDRINFO(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    def create(response):
        app = Application(response)
        server = SearchServer(("127.0.0.1", 0), app, make_tls_context(tmp_path))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        # Trust only this test's ephemeral certificate. Production uses public CA
        # validation and the configured fixed DNS name, never verify=False.
        pem = ssl.get_server_certificate(server.server_address)
        ca = ssl.create_default_context(cadata=pem)
        http = httpx.Client(verify=ca, trust_env=False)
        backend = HTTPBackend("https://search.example/search", http, Tokens())
        backend.url = f"https://127.0.0.1:{server.server_port}/search"
        return backend, app, http

    yield create
    for server, thread in servers:
        server.graceful_stop(timeout=1)
        thread.join(timeout=2)


def invoke(backend):
    identity = RouteIdentity("gateway", "target", "Search___search_tools")
    ctx = identity.context("原id")
    ctx.get_remaining_time_in_millis = lambda: 10000

    def fallback(body, deadline):
        raise AssertionError("No fallback is allowed for an explicit authorization error")

    return Router(identity, backend, fallback)({"query": "商品", "_auth_context": "opaque"}, ctx)


def test_real_tls_error_preserves_public_authentication_error(backend_server):
    backend, app, http = backend_server(failure("unauthenticated"))
    with http, pytest.raises(AuthenticationError):
        invoke(backend)
    assert len(app.calls) == 1


def test_real_tls_success_preserves_response_and_connection_reuse(backend_server):
    result = {"tools": [{"name": "商品", "description": "原值"}]}
    backend, app, http = backend_server({"version": 1, "ok": True, "result": result})
    with http:
        assert invoke(backend) == invoke(backend) == result
    assert len(app.calls) == 2
    assert app.calls[0][1] == {"authorization": "Bearer test-token"}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ConnectError("down"), TransportUnavailable),
        (httpx.ReadTimeout("slow"), TransportUnavailable),
        (httpx.RemoteProtocolError("invalid"), TransportUnavailable),
        (httpx.LocalProtocolError("invalid"), TransportUnavailable),
        (httpx.ProtocolError("invalid"), TransportUnavailable),
        (httpx.CloseError("closed"), TransportUnavailable),
    ],
)
def test_http_exception_classification(error, expected):
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def fail(request):
        raise error

    with httpx.Client(transport=httpx.MockTransport(fail)) as http:
        backend = HTTPBackend("https://search.example/search", http, Tokens())
        with pytest.raises(expected):
            backend({"attempt_id": "a" * 32}, time.monotonic() + 1)


def test_certificate_error_never_falls_back():
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def fail(request):
        try:
            raise ssl.SSLCertVerificationError("untrusted")
        except ssl.SSLError as cause:
            raise httpx.ConnectError("connect failed") from cause

    with httpx.Client(transport=httpx.MockTransport(fail)) as http:
        backend = HTTPBackend("https://search.example/search", http, Tokens())
        with pytest.raises(ServiceError):
            backend({"attempt_id": "a" * 32}, time.monotonic() + 1)


def test_full_public_response_limit_fits_internal_envelope():
    result = {"tools": [{"name": "x", "description": ""}]}
    overhead = len(encode_json(result))
    result["tools"][0]["description"] = "x" * (524288 - overhead)
    assert len(encode_json(result)) == 524288
    response = {"version": 1, "ok": True, "result": result}
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as http:
        backend = HTTPBackend("https://search.example/search", http, Tokens())
        assert backend({"attempt_id": "a" * 32}, time.monotonic() + 1) == response

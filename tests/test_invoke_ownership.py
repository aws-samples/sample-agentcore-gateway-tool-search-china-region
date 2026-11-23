"""Real local HTTP: aborts inside SDK Invoke must not retain unread connections."""

import inspect
import json
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from botocore.config import Config
from botocore.session import Session
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.response import HTTPResponse

from semantic_service import facade

_CONNECT = socket.socket.connect
_GETADDRINFO = socket.getaddrinfo


@pytest.fixture(autouse=True)
def isolated_sdk_environment(tmp_path, monkeypatch):
    # Host profiles must neither be read nor affect this local HTTP fixture.
    config, credentials = tmp_path / "config", tmp_path / "credentials"
    config.write_text("")
    credentials.write_text("")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_PROFILE", "inherited-profile-must-not-be-used")
    monkeypatch.setenv("AWS_DEFAULT_PROFILE", "inherited-profile-must-not-be-used")


@pytest.fixture
def local_invoke(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DATA_PATH", raising=False)
    monkeypatch.delenv("AWS_CA_BUNDLE", raising=False)

    def connect(sock, address):
        assert address[0] == "127.0.0.1"
        return _CONNECT(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        assert host == "127.0.0.1"
        return _GETADDRINFO(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    closed = threading.Event()
    sent = threading.Event()
    received = []
    body = json.dumps({"version": 1, "ok": True, "result": {"tools": []}}).encode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def handle(self):
            try:
                super().handle()
            except ConnectionResetError:
                pass
            finally:
                closed.set()

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            received.append(self.client_address)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            sent.set()
            self.connection.settimeout(2)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Session().create_client(  # nosec B106
        "lambda",
        region_name="cn-north-1",
        endpoint_url=f"http://127.0.0.1:{server.server_port}",
        aws_access_key_id="offline-test",
        aws_secret_access_key="offline-test",
        config=Config(
            connect_timeout=0.5,
            read_timeout=1,
            max_pool_connections=1,
            retries={"total_max_attempts": 1, "mode": "standard"},
            proxies={},
        ),
    )
    try:
        yield client, sent, closed, received
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("boundary", ["before_parse", "http_send", "invoke_handoff"])
def test_alarm_inside_invoke_closes_real_unread_body_before_return(local_invoke, boundary):
    client, sent, closed, received = local_invoke
    backend = facade.LambdaBackend(
        "arn:aws-cn:lambda:cn-north-1:123456789012:function:example:live", client
    )
    raw_bodies = []
    fired = []

    def interrupt_parse(response_dict, **kwargs):
        raw_bodies.append(response_dict["body"]._raw_stream)
        signal.raise_signal(signal.SIGALRM)

    previous_trace = sys.gettrace()

    def trace(frame, event, arg):
        if (
            boundary == "invoke_handoff"
            and not fired
            and event == "line"
            and frame.f_globals.get("__name__") == "semantic_service.facade"
            and frame.f_code.co_name == "_invoke"
            and "response" in frame.f_locals
        ):
            fired.append(True)
            raw_bodies.append(frame.f_locals["response"]["Payload"]._raw_stream)
            signal.raise_signal(signal.SIGALRM)
        if (
            boundary == "http_send"
            and not fired
            and event == "line"
            and frame.f_globals.get("__name__") == "botocore.httpsession"
            and frame.f_code.co_name == "send"
            and "http_response" in frame.f_locals
        ):
            fired.append(True)
            raw_bodies.append(frame.f_locals["http_response"].raw)
            signal.raise_signal(signal.SIGALRM)
        return trace

    if boundary == "before_parse":
        client.meta.events.register("before-parse.lambda.Invoke", interrupt_parse)
    else:
        sys.settrace(trace)
    try:
        with pytest.raises(facade.DeadlineExpired):
            with facade.enforce_deadline(time.monotonic() + 2):
                backend({"offline": True}, time.monotonic() + 1)
        assert sent.is_set() and len(received) == len(raw_bodies) == 1
        # Keep the actual unread response strongly referenced: GC cannot make
        # this pass. client.close() alone does not own this checked-out stream.
        assert raw_bodies[0].closed is True
        assert closed.wait(0.5), "server must observe EOF/reset of the live connection"
        pool = client._endpoint.http_session._manager.connection_from_url(client.meta.endpoint_url)
        assert pool.pool.qsize() == pool.pool.maxsize, "closed lease must return to pool"
    finally:
        sys.settrace(previous_trace)
        for raw in raw_bodies:
            raw.close()
            raw.release_conn()


def test_normal_invoke_preserves_real_http_connection_reuse(local_invoke):
    client, _, _, received = local_invoke
    backend = facade.LambdaBackend(
        "arn:aws-cn:lambda:cn-north-1:123456789012:function:example:live", client
    )
    with facade.enforce_deadline(time.monotonic() + 2):
        assert backend({"offline": True}, time.monotonic() + 1) == {
            "version": 1,
            "ok": True,
            "result": {"tools": []},
        }
        assert backend({"offline": True}, time.monotonic() + 1)["ok"] is True
    assert len(received) == 2 and received[0] == received[1]


def test_repeated_abort_keeps_one_restored_pool_slot_and_no_live_bodies(local_invoke):
    client, _, _, received = local_invoke
    backend = facade.LambdaBackend(
        "arn:aws-cn:lambda:cn-north-1:123456789012:function:example:live", client
    )
    raw_bodies = []

    def interrupt_parse(response_dict, **kwargs):
        raw_bodies.append(response_dict["body"]._raw_stream)
        signal.raise_signal(signal.SIGALRM)

    client.meta.events.register("before-parse.lambda.Invoke", interrupt_parse)
    try:
        for _ in range(20):
            with pytest.raises(facade.DeadlineExpired):
                with facade.enforce_deadline(time.monotonic() + 2):
                    backend({"offline": True}, time.monotonic() + 1)
            pool = client._endpoint.http_session._manager.connection_from_url(
                client.meta.endpoint_url
            )
            assert pool.pool.qsize() == pool.pool.maxsize == 1
        assert len(received) == len(raw_bodies) == 20
        assert all(raw.closed for raw in raw_bodies)
    finally:
        for raw in raw_bodies:
            raw.close()
            raw.release_conn()


def test_only_one_invoke_can_be_owned_at_a_time(local_invoke):
    client, _, _, received = local_invoke
    backend = facade.LambdaBackend(
        "arn:aws-cn:lambda:cn-north-1:123456789012:function:example:live", client
    )
    with backend._ownership.hold():
        with pytest.raises(RuntimeError, match="concurrent fallback unavailable"):
            backend({"offline": True}, time.monotonic() + 1)
    assert received == []


def test_signal_during_sdk_error_cleanup_does_not_strand_pool_capacity(local_invoke, monkeypatch):
    client, _, _, received = local_invoke
    backend = facade.LambdaBackend(
        "arn:aws-cn:lambda:cn-north-1:123456789012:function:example:live", client
    )
    source, first = inspect.getsourcelines(HTTPConnectionPool.urlopen)
    line_number = first + next(
        index for index, line in enumerate(source) if line.strip() == "self._put_conn(conn)"
    )
    old_pool = client._endpoint.http_session._manager.connection_from_url(client.meta.endpoint_url)
    fired = []

    def fail(*args, **kwargs):
        raise OSError("offline injected transport failure")

    def trace(frame, event, arg):
        if (
            not fired
            and event == "line"
            and frame.f_globals.get("__name__") == "urllib3.connectionpool"
            and frame.f_code.co_name == "urlopen"
            and frame.f_lineno == line_number
        ):
            fired.append(True)
            signal.raise_signal(signal.SIGALRM)
        return trace

    previous = sys.gettrace()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(HTTPConnectionPool, "_make_request", fail)
            sys.settrace(trace)
            with pytest.raises(facade.DeadlineExpired):
                with facade.enforce_deadline(time.monotonic() + 2):
                    backend({"offline": True}, time.monotonic() + 1)
    finally:
        sys.settrace(previous)
    assert fired and received == []
    fresh_pool = client._endpoint.http_session._manager.connection_from_url(
        client.meta.endpoint_url
    )
    assert fresh_pool.pool.qsize() == fresh_pool.pool.maxsize == 1
    assert fresh_pool is not old_pool, "the interrupted client-owned pool must not be reused"
    with facade.enforce_deadline(time.monotonic() + 2):
        assert backend({"offline": True}, time.monotonic() + 1)["ok"] is True
    assert len(received) == 1


def test_completed_fallback_survives_alarm_queued_during_owned_cleanup(local_invoke, monkeypatch):
    from semantic_service import invoke_ownership
    from semantic_service.transport import RouteIdentity

    client, _, _, _ = local_invoke
    backend = facade.LambdaBackend(
        "arn:aws-cn:lambda:cn-north-1:123456789012:function:example:live", client
    )
    close = invoke_ownership._Resources.close
    cleanup = []

    def alarm(resources):
        cleanup.append(True)
        signal.raise_signal(signal.SIGALRM)
        close(resources)

    monkeypatch.setattr(invoke_ownership._Resources, "close", alarm)
    identity = RouteIdentity("gateway", "target", "Search___search_tools")
    context = identity.context("fixture")
    context.get_remaining_time_in_millis = lambda: 15000
    records = []

    def primary(body, deadline):
        raise facade.TransportUnavailable()

    result = facade.Router(identity, primary, backend, emit=records.append)(
        {"query": "fixture", "_auth_context": "fixture"}, context
    )
    assert result == {"tools": []} and cleanup == [True]
    assert records[-1]["status"] == "ok" and records[-1]["backend"] == "fallback"
    assert backend._ownership._active is None
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def _unread_success_ownership_probe(client):
    backend = facade.LambdaBackend(
        "arn:aws-cn:lambda:cn-north-1:123456789012:function:example:live", client
    )
    pool = client._endpoint.http_session._manager.connection_from_url(client.meta.endpoint_url)
    with facade.enforce_deadline(time.monotonic() + 2):
        with backend._ownership.hold():
            response = client.invoke(
                FunctionName=backend.arn, InvocationType="RequestResponse", Payload=b"{}"
            )
            raw = response["Payload"]._raw_stream
            assert raw._pool is pool and pool.pool.qsize() == 0
        # This is the same pool that supplied the observed checked-out response,
        # not a fresh pool prefilled with None placeholders after an abort.
        assert (
            client._endpoint.http_session._manager.connection_from_url(client.meta.endpoint_url)
            is pool
        )
        assert raw.closed
        assert raw._connection is None and pool.pool.qsize() == 1, "borrowed lease not returned"


def test_unread_success_returns_the_observed_lease_to_the_same_pool(local_invoke):
    client, _, closed, received = local_invoke
    _unread_success_ownership_probe(client)
    assert len(received) == 1 and closed.wait(0.5)


def test_release_conn_negative_control_is_detected(local_invoke, monkeypatch):
    client, _, _, _ = local_invoke
    monkeypatch.setattr(HTTPResponse, "release_conn", lambda self: None)
    with pytest.raises(AssertionError, match="borrowed lease"):
        _unread_success_ownership_probe(client)


def _ownership_handoff_signal_probe(client):
    backend = facade.LambdaBackend(
        "arn:aws-cn:lambda:cn-north-1:123456789012:function:example:live", client
    )
    real_lock = threading.Lock()

    class InterruptingLock:
        def acquire(self, **kwargs):
            acquired = real_lock.acquire(**kwargs)
            if acquired:
                # The real lock is held, but hold() has not yet recorded the
                # boolean result. An unmasked signal strands ownership here.
                signal.raise_signal(signal.SIGALRM)
            return acquired

        def release(self):
            real_lock.release()

    backend._ownership._lock = InterruptingLock()
    try:
        with pytest.raises(facade.DeadlineExpired):
            with facade.enforce_deadline(time.monotonic() + 2):
                backend({"offline": True}, time.monotonic() + 1)
        available = real_lock.acquire(blocking=False)
        assert available, "ownership handoff stranded the real mutex"
        assert backend._ownership._active is None
    finally:
        if real_lock.locked():
            real_lock.release()


def test_initial_alarm_mask_protects_mutex_ownership_handoff(local_invoke):
    client, _, _, received = local_invoke
    _ownership_handoff_signal_probe(client)
    assert not received


def test_initial_sig_block_negative_control_is_detected(local_invoke, monkeypatch):
    client, _, _, _ = local_invoke
    original = signal.pthread_sigmask
    skipped = []

    def omit_first_block(how, signals):
        if how == signal.SIG_BLOCK and signals == {signal.SIGALRM} and not skipped:
            skipped.append(True)
            return original(how, set())
        return original(how, signals)

    monkeypatch.setattr(signal, "pthread_sigmask", omit_first_block)
    with pytest.raises(AssertionError, match="stranded the real mutex"):
        _ownership_handoff_signal_probe(client)

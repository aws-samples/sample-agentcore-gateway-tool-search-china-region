"""真实 httpcore 事件链和固定字段诊断；全部使用内存网络后端。"""

from __future__ import annotations

import gzip
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict
from threading import Barrier

import httpcore
import httpx
import pytest

from semantic_core import _embedding_telemetry as telemetry
from semantic_core.openai_compatible import (
    EmbeddingConfig,
    OpenAICompatibleConfig,
    OpenAIEmbeddingProvider,
)
from semantic_core.providers import ErrorCode, ModelProfile, ProviderError, RequestContext
from semantic_service.models import ModelSettings, RotatingModelProviders
from semantic_service.secrets import SecretsCache

PROFILE = ModelProfile("timing-fixture", "v1", 2)
BODY = b'{"data":[{"index":0,"embedding":[1,0]}]}'
HEADERS = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
    + str(len(BODY)).encode()
    + b"\r\n\r\n"
)
MARKER = "sensitive-timing-marker"


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ScriptStream(httpcore.MockStream):
    def __init__(self, clock, reads, *, tls_delay=0.03, tls_error=None):
        super().__init__([])
        self.clock, self.reads = clock, list(reads)
        self.tls_delay, self.tls_error = tls_delay, tls_error

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.clock.advance(self.tls_delay)
        if self.tls_error:
            raise self.tls_error
        return self

    def read(self, max_bytes, timeout=None):
        if not self.reads:
            return b""
        delay, value = self.reads.pop(0)
        self.clock.advance(delay)
        if isinstance(value, Exception):
            raise value
        return value


class ScriptBackend(httpcore.MockBackend):
    def __init__(self, clock, connections, *, tcp_delay=0.02, tcp_error=None):
        super().__init__([])
        self.clock, self.connections = clock, list(connections)
        self.tcp_delay, self.tcp_error = tcp_delay, tcp_error
        self.connects = 0

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connects += 1
        self.clock.advance(self.tcp_delay)
        if self.tcp_error:
            raise self.tcp_error
        return self.connections.pop(0)


def client_for(backend):
    transport = httpx.HTTPTransport(trust_env=False)
    # Test-only replacement: preserve HTTPX's real adapter and httpcore event path.
    transport._pool = httpcore.ConnectionPool(network_backend=backend, retries=0)
    return httpx.Client(transport=transport, trust_env=False)


def provider(client):
    return OpenAIEmbeddingProvider(
        client,
        OpenAICompatibleConfig(
            f"https://{MARKER}.invalid",
            (f"{MARKER}.invalid",),
            timeout_seconds=1,
            auth_header_name="Authorization",
        ),
        EmbeddingConfig(PROFILE, "/embeddings", 10, 8192),
        credential=MARKER,
    )


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr("semantic_core._http.time.monotonic", value)
    return value


def invoke(embedding, clock, *, seconds=1):
    deadline = clock() + seconds
    with telemetry.collect_embedding(deadline + 0.5, deadline, seconds) as trace:
        result = embedding.embed([MARKER], "query", RequestContext("timing-request", deadline))
    return result, trace.snapshot()


def test_real_httpcore_headers_are_complete_headers_not_wire_first_byte(clock):
    # First wire bytes arrive 40ms before the rest of the response headers.
    stream = ScriptStream(
        clock,
        [(0.01, HEADERS[:20]), (0.04, HEADERS[20:]), (0.06, BODY)],
    )
    backend = ScriptBackend(clock, [stream])
    with client_for(backend) as client:
        result, summary = invoke(provider(client), clock)
    attempt = summary.attempts[0]
    assert result.vectors.shape == (1, 2)
    assert summary.search_remaining_ms == pytest.approx(1500)
    assert summary.stage_budget_ms == pytest.approx(1000)
    assert summary.configured_timeout_ms == pytest.approx(1000)
    assert attempt.attempt == 1
    assert attempt.effective_timeout_ms == pytest.approx(1000)
    assert attempt.tcp_ms == pytest.approx(20)
    assert attempt.tls_ms == pytest.approx(30)
    assert attempt.response_headers_ms == pytest.approx(100)
    assert attempt.first_body_chunk_ms == pytest.approx(160)
    assert attempt.body_ms == pytest.approx(60)
    assert attempt.total_ms == pytest.approx(160)
    assert attempt.http_status == 200
    assert attempt.body_complete is True
    assert attempt.connection_state == "new_connect_observed"
    assert attempt.diagnostics_failed is False
    assert summary.diagnostics_failed is False
    assert "ttfb" not in json.dumps(asdict(summary)).lower()
    with pytest.raises(FrozenInstanceError):
        attempt.http_status = 500


def test_real_keepalive_has_no_new_connect_and_missing_trace_stays_unknown(clock):
    stream = ScriptStream(clock, [(0.04, HEADERS + BODY), (0.05, HEADERS + BODY)])
    backend = ScriptBackend(clock, [stream])
    with client_for(backend) as client:
        _, first = invoke(provider(client), clock)
        _, second = invoke(provider(client), clock)
    assert backend.connects == 1
    assert first.attempts[0].connection_state == "new_connect_observed"
    assert second.attempts[0].connection_state == "existing_connection_inferred"
    assert second.attempts[0].tcp_ms is None
    assert second.attempts[0].tls_ms is None
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=BODY))
    ) as client:
        _, unsupported = invoke(provider(client), clock)
    assert unsupported.attempts[0].connection_state == "unknown"
    assert unsupported.attempts[0].tcp_ms is None
    assert unsupported.attempts[0].tls_ms is None


def test_first_body_chunk_is_after_decoding_not_first_raw_body_bytes(clock):
    encoded = gzip.compress(BODY, mtime=0)
    headers = (
        b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: "
        + str(len(encoded)).encode()
        + b"\r\n\r\n"
    )
    stream = ScriptStream(clock, [(0.01, headers), (0.01, encoded[:8]), (0.02, encoded[8:])])
    with client_for(ScriptBackend(clock, [stream])) as client:
        result, summary = invoke(provider(client), clock)
    assert result.vectors.shape == (1, 2)
    assert summary.attempts[0].response_headers_ms == pytest.approx(60)
    assert summary.attempts[0].first_body_chunk_ms == pytest.approx(90)
    assert summary.attempts[0].body_ms == pytest.approx(30)
    assert summary.attempts[0].body_complete is True


@pytest.mark.parametrize("failure_at", ["tcp", "tls", "headers", "body"])
def test_real_transport_failures_preserve_error_and_partial_phase_timing(clock, failure_at):
    error = (
        httpcore.ConnectTimeout(MARKER)
        if failure_at in {"tcp", "tls"}
        else httpcore.ReadTimeout(MARKER)
    )
    reads = [(0.04, HEADERS), (0.06, error)] if failure_at == "body" else [(0.04, error)]
    stream = ScriptStream(clock, reads, tls_error=error if failure_at == "tls" else None)
    backend = ScriptBackend(clock, [stream], tcp_error=error if failure_at == "tcp" else None)
    deadline = clock() + 1
    with client_for(backend) as client:
        with telemetry.collect_embedding(deadline + 0.5, deadline, 1) as trace:
            with pytest.raises(ProviderError) as raised:
                provider(client).embed(
                    [MARKER], "query", RequestContext("timing-request", deadline)
                )
    attempt = trace.snapshot().attempts[0]
    assert raised.value.code is ErrorCode.TIMEOUT
    assert raised.value.failure.kind == (
        "connect_timeout" if failure_at in {"tcp", "tls"} else "read_timeout"
    )
    assert attempt.http_status == (200 if failure_at == "body" else None)
    if failure_at == "body":
        assert attempt.response_headers_ms == pytest.approx(90)
    else:
        assert attempt.response_headers_ms is None
    assert attempt.first_body_chunk_ms is None
    assert attempt.body_complete is False
    assert attempt.tcp_ms == pytest.approx(20)
    if failure_at == "tcp":
        assert attempt.tls_ms is None
        assert attempt.body_ms is None
    else:
        assert attempt.tls_ms == pytest.approx(30)
    assert MARKER not in json.dumps(asdict(trace.snapshot()))


def test_late_200_body_is_observed_without_changing_deadline_classification(clock):
    stream = ScriptStream(clock, [(0.15, HEADERS), (0.1, BODY)], tls_delay=0.15)
    backend = ScriptBackend(clock, [stream], tcp_delay=0.15)
    deadline = clock() + 0.45
    with client_for(backend) as client:
        with telemetry.collect_embedding(deadline + 1, deadline, 0.45) as trace:
            with pytest.raises(ProviderError) as raised:
                provider(client).embed(
                    [MARKER], "query", RequestContext("timing-request", deadline)
                )
    attempt = trace.snapshot().attempts[0]
    assert raised.value.failure.kind == "deadline_exceeded"
    assert raised.value.failure.http_status == 200
    assert attempt.effective_timeout_ms == pytest.approx(450)
    assert attempt.response_headers_ms == pytest.approx(450)
    assert attempt.first_body_chunk_ms == pytest.approx(550)
    assert attempt.body_complete is False
    assert attempt.total_ms == pytest.approx(550)


class FakeSecrets:
    def __init__(self, values):
        self.values = iter(values)

    def get_secret_value(self, **kwargs):
        result = next(self.values)
        if isinstance(result, Exception):
            raise result
        return result


def secret(version):
    return {
        "VersionId": version,
        "SecretString": json.dumps({"base_url": f"https://{MARKER}.invalid", "api_key": MARKER}),
    }


@pytest.mark.parametrize("refresh_fails", [False, True])
def test_auth_rotation_has_separate_attempts_and_keeps_original_deadline(clock, refresh_fails):
    seen_timeouts = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def respond(request):
        seen_timeouts.append(request.extensions["timeout"]["read"])
        if len(seen_timeouts) == 1:
            clock.advance(0.3)
            return httpx.Response(401)
        return httpx.Response(200, content=BODY)

    cache = SecretsCache(
        FakeSecrets([secret("v1"), RuntimeError(MARKER) if refresh_fails else secret("v2")])
    )
    settings = ModelSettings.from_env(
        {"MODEL_ALLOWED_HOSTS": f"{MARKER}.invalid", "MODEL_EMBEDDING_DIMENSIONS": "2"}
    )
    deadline = clock() + 0.4
    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        embedding = RotatingModelProviders(cache, settings, client).embedding
        with telemetry.collect_embedding(deadline + 1, deadline, 0.45) as trace:
            if refresh_fails:
                with pytest.raises(ProviderError):
                    embedding.embed([MARKER], "query", RequestContext("timing-request", deadline))
            else:
                embedding.embed([MARKER], "query", RequestContext("timing-request", deadline))
    attempts = trace.snapshot().attempts
    assert len(attempts) == (1 if refresh_fails else 2)
    assert attempts[0].http_status == 401
    assert attempts[0].attempt == 1
    assert attempts[0].body_complete is False
    assert attempts[0].body_ms is None
    if not refresh_fails:
        assert attempts[1].attempt == 2
        assert attempts[1].http_status == 200
        assert attempts[1].effective_timeout_ms == pytest.approx(100)
        assert seen_timeouts == pytest.approx([0.4, 0.1])


def test_trace_ignores_sensitive_info_unknown_names_and_caps_attempt_records(clock, capsys):
    class Poison:
        def __repr__(self):
            raise AssertionError("raw trace info must not be formatted")

        def __getitem__(self, key):
            raise AssertionError("raw trace info must not be inspected")

    def respond(request):
        callback = request.extensions.get("trace")
        if callback is not None:
            callback(f"unknown.{MARKER}", Poison())
            callback("connection.connect_tcp.started", Poison())
            clock.advance(0.01)
            callback("connection.connect_tcp.complete", Poison())
        return httpx.Response(200, content=BODY, headers={"X-Private": MARKER})

    deadline = clock() + 1
    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        with telemetry.collect_embedding(deadline, deadline, 1) as trace:
            for _ in range(3):
                provider(client).embed(
                    [MARKER], "query", RequestContext("timing-request", deadline)
                )
    summary = trace.snapshot()
    assert len(summary.attempts) == 2
    assert summary.attempts_truncated is True
    assert summary.attempts[0].tcp_ms == pytest.approx(10)
    text = json.dumps(asdict(summary))
    assert MARKER not in text
    assert len(text) < 4096
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_diagnostic_callback_and_snapshot_failures_do_not_change_provider_result(
    clock, monkeypatch
):
    def broken(*args, **kwargs):
        raise RuntimeError(MARKER)

    stream = ScriptStream(clock, [(0.01, HEADERS + BODY)])
    backend = ScriptBackend(clock, [stream])
    with client_for(backend) as client:
        monkeypatch.setattr(telemetry.AttemptTrace, "_on_event", broken)
        result, summary = invoke(provider(client), clock)
    assert result.vectors.shape == (1, 2)
    assert summary.diagnostics_failed is True
    assert summary.attempts[0].diagnostics_failed is True
    assert summary.attempts[0].connection_state == "unknown"
    monkeypatch.setattr(telemetry.AttemptTrace, "snapshot", broken)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=BODY))
    ) as client:
        result, summary = invoke(provider(client), clock)
    assert result.vectors.shape == (1, 2)
    assert summary.diagnostics_failed is True
    assert summary.attempts == ()


@pytest.mark.parametrize("status", [200, 503])
def test_failed_diagnostic_clock_preserves_success_or_original_provider_error(
    clock, monkeypatch, status
):
    def broken_clock():
        raise RuntimeError(MARKER)

    monkeypatch.setattr(telemetry, "_now", broken_clock)
    deadline = clock() + 1
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, content=BODY))
    ) as client:
        with telemetry.collect_embedding(deadline, deadline, 1) as trace:
            if status == 200:
                assert provider(client).embed(
                    [MARKER], "query", RequestContext("clock-fixture", deadline)
                ).vectors.shape == (1, 2)
            else:
                with pytest.raises(ProviderError) as error:
                    provider(client).embed(
                        [MARKER], "query", RequestContext("clock-fixture", deadline)
                    )
                assert error.value.failure.kind == "http_server_error"
                assert error.value.failure.http_status == 503
    summary = trace.snapshot()
    assert summary.diagnostics_failed is True
    assert summary.search_remaining_ms is None
    assert summary.stage_budget_ms is None
    assert summary.attempts[0].total_ms is None
    assert summary.attempts[0].connection_state == "unknown"
    assert MARKER not in json.dumps(asdict(summary))


def test_no_embedding_scope_does_not_install_a_trace_callback(clock):
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def respond(request):
        assert "trace" not in request.extensions
        return httpx.Response(200, content=BODY)

    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        result = provider(client).embed(
            [MARKER], "query", RequestContext("without-scope", clock() + 1)
        )
    assert result.vectors.shape == (1, 2)


def test_parallel_collectors_and_nested_scope_do_not_share_attempts():
    barrier = Barrier(2)

    def one_request(status):
        # Callback is passed to the local mock transport below; it is not unused.
        # nosemgrep: useless-inner-function
        def respond(request):
            barrier.wait(timeout=5)
            return httpx.Response(status, content=BODY)

        import time

        deadline = time.monotonic() + 10
        with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
            with telemetry.collect_embedding(deadline, deadline, 1) as trace:
                try:
                    provider(client).embed([MARKER], "query", RequestContext("parallel", deadline))
                except ProviderError:
                    assert status == 503
        return trace.snapshot()

    with ThreadPoolExecutor(max_workers=2) as executor:
        summaries = list(executor.map(one_request, [200, 503]))
    assert [tuple(a.http_status for a in s.attempts) for s in summaries] == [(200,), (503,)]
    assert all(s.attempts[0].attempt == 1 for s in summaries)
    assert telemetry.start_http_attempt(1) is None
    with telemetry.collect_embedding(1000, 1000, 1) as outer:
        with telemetry.collect_embedding(1000, 1000, 1) as inner:
            telemetry.start_http_attempt(1)
        telemetry.start_http_attempt(1)
    assert len(inner.snapshot().attempts) == len(outer.snapshot().attempts) == 1

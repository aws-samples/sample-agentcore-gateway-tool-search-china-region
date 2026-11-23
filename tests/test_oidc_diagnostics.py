"""OIDC 依赖失败只输出固定分类；补偿 GET 计时，诊断不跨请求残留。"""

import json
import socket
import ssl
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from test_oidc_attempt_events import split_request_rows

from semantic_service import runtime
from semantic_service.auth import AuthenticationError, OIDCConfig, OIDCVerifier, ServiceError

NOW = 1_700_000_000
PRIVATE = "private-diagnostic-fixture"
ISSUER = f"https://{PRIVATE}.example.invalid"
KID = f"{PRIVATE}-kid"
REQUEST_ID = "01234567-89ab-cdef-0123-456789abcdef"


@pytest.fixture(scope="module")
def signing_fixture():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid=KID, alg="RS256", use="sig")
    token = jwt.encode(
        {
            "iss": ISSUER,
            "sub": PRIVATE,
            "aud": "gateway",
            "iat": NOW,
            "exp": NOW + 300,
            "token_use": "access",
        },
        private,
        algorithm="RS256",
        headers={"kid": KID},
    )
    return jwk, token


@pytest.fixture
def oidc_factory(signing_fixture):
    jwk, token = signing_fixture
    clients = []

    def create(endpoint=None, failure=None):
        config = OIDCConfig(
            ISSUER,
            f"{ISSUER}/jwks",
            "gateway",
            frozenset({f"{PRIVATE}.example.invalid"}),
            discovery_uri=f"{ISSUER}/discovery",
        )
        calls = []

        def transport(request):
            current = "discovery" if str(request.url) == config.discovery_uri else "jwks"
            calls.append(current)
            if current == endpoint:
                if isinstance(failure, Exception):
                    raise failure
                if callable(failure):
                    return failure(request)
                return failure
            if current == "discovery":
                return httpx.Response(
                    200, json={"issuer": config.issuer, "jwks_uri": config.jwks_uri}
                )
            return httpx.Response(200, json={"keys": [jwk]})

        client = httpx.Client(transport=httpx.MockTransport(transport), trust_env=False)
        clients.append(client)
        return OIDCVerifier(config, client, clock=lambda: NOW), token, calls

    yield create
    for client in clients:
        client.close()


def observe(monkeypatch, capsys, verifier, token, expected_error=ServiceError):
    def handler(event, context):
        verifier.verify(token)
        return {"tools": []}

    monkeypatch.setattr(runtime, "_request", lambda: handler)
    context = SimpleNamespace(aws_request_id=REQUEST_ID)
    if expected_error is None:
        assert runtime.request_handler({}, context) == {"tools": []}
        error = None
    else:
        with pytest.raises(expected_error) as caught:
            runtime.request_handler({}, context)
        error = caught.value
        assert type(error) is expected_error
        assert str(error) == (
            "unauthenticated" if expected_error is AuthenticationError else "internal failure"
        )
    captured = capsys.readouterr()
    output = captured.out
    all_output = output + captured.err
    assert PRIVATE not in all_output
    assert ISSUER not in all_output
    assert KID not in all_output
    assert token not in all_output
    rows = [json.loads(line) for line in output.splitlines()]
    _, row = split_request_rows(rows, REQUEST_ID)
    assert row["aws_request_id"] == REQUEST_ID
    assert row["operation"] == "request"
    return row, error


def assert_failure(row, error, endpoint, category, reason, status=None, cause=None):
    assert row["status"] == "internal failure"
    assert row["auth_failure"] == {
        "endpoint": endpoint,
        "category": category,
        "reason": reason,
        "http_status": status,
        "cause": cause,
    }
    assert error.oidc_failure is not None
    assert row["stage_timings"]["auth.oidc"]["errors"] == 1
    assert row["stage_timings"]["handler.work"]["errors"] == 1


@pytest.mark.parametrize("endpoint", ["discovery", "jwks"])
@pytest.mark.parametrize("status", [429, 503])
def test_http_status_is_distinct_without_response_body(
    monkeypatch, capsys, oidc_factory, endpoint, status
):
    verifier, token, calls = oidc_factory(
        endpoint, httpx.Response(status, text=f"{PRIVATE} {ISSUER} {KID}")
    )
    row, error = observe(monkeypatch, capsys, verifier, token)
    assert_failure(row, error, endpoint, "http", "status", status)
    attempts = 2 if status == 503 else 1
    assert row["stage_timings"][f"auth.{endpoint}"]["count"] == attempts
    assert row["stage_timings"][f"auth.{endpoint}"]["errors"] == attempts
    assert calls == (
        ["discovery"] * attempts if endpoint == "discovery" else ["discovery"] + ["jwks"] * attempts
    )


@pytest.mark.parametrize("endpoint", ["discovery", "jwks"])
@pytest.mark.parametrize(
    ("exception_type", "reason"),
    [
        (httpx.ConnectTimeout, "connect"),
        (httpx.ReadTimeout, "read"),
        (httpx.WriteTimeout, "write"),
        (httpx.PoolTimeout, "pool"),
        (httpx.TimeoutException, "unknown"),
    ],
)
def test_timeout_type_is_preserved_without_inferring_its_lower_level_cause(
    monkeypatch, capsys, oidc_factory, endpoint, exception_type, reason
):
    verifier, token, _ = oidc_factory(endpoint, exception_type(f"{PRIVATE} DNS TLS {ISSUER}"))
    row, error = observe(monkeypatch, capsys, verifier, token)
    assert_failure(row, error, endpoint, "timeout", reason)
    attempts = 2 if reason in {"connect", "read"} else 1
    assert row["stage_timings"][f"auth.{endpoint}"]["count"] == attempts
    assert row["stage_timings"][f"auth.{endpoint}"]["errors"] == attempts


@pytest.mark.parametrize("endpoint", ["discovery", "jwks"])
@pytest.mark.parametrize(
    ("cause_type", "cause"),
    [
        (socket.gaierror, "dns"),
        (ssl.SSLCertVerificationError, "tls_certificate"),
        (ssl.SSLError, "tls"),
        (ConnectionRefusedError, "connection_refused"),
        (ConnectionResetError, "connection_reset"),
        (OSError, None),
    ],
)
def test_network_cause_uses_only_concrete_types_in_the_explicit_cause_chain(
    monkeypatch, capsys, oidc_factory, endpoint, cause_type, cause
):
    lower = cause_type(1, f"{PRIVATE} {ISSUER}")
    intermediate = RuntimeError(f"{PRIVATE} transport wrapper")
    intermediate.__cause__ = lower
    failure = httpx.ConnectError(f"{PRIVATE} {ISSUER}")
    failure.__cause__ = intermediate
    verifier, token, _ = oidc_factory(endpoint, failure)
    row, error = observe(monkeypatch, capsys, verifier, token)
    assert_failure(row, error, endpoint, "network", "connect", cause=cause)


def test_network_message_and_suppressed_context_do_not_imply_dns_or_tls(
    monkeypatch, capsys, oidc_factory
):
    failure = httpx.ConnectError(f"{PRIVATE} DNS TLS certificate connection refused")
    failure.__context__ = socket.gaierror(1, PRIVATE)
    failure.__suppress_context__ = True
    verifier, token, _ = oidc_factory("discovery", failure)
    row, error = observe(monkeypatch, capsys, verifier, token)
    assert_failure(row, error, "discovery", "network", "connect")


@pytest.mark.parametrize(
    ("exception_type", "reason"),
    [
        (httpx.ReadError, "read"),
        (httpx.WriteError, "write"),
        (httpx.CloseError, "close"),
        (httpx.RemoteProtocolError, "protocol"),
        (httpx.LocalProtocolError, "protocol"),
        (httpx.NetworkError, "unknown"),
    ],
)
def test_other_network_types_are_bounded_fixed_codes(
    monkeypatch, capsys, oidc_factory, exception_type, reason
):
    verifier, token, _ = oidc_factory("jwks", exception_type(PRIVATE))
    row, error = observe(monkeypatch, capsys, verifier, token)
    assert_failure(row, error, "jwks", "network", reason)


@pytest.mark.parametrize("endpoint", ["discovery", "jwks"])
@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (f"not-json {PRIVATE}".encode(), "json_invalid"),
        (b"\xff", "json_encoding"),
        (b"x" * (256 * 1024 + 1), "body_too_large"),
        (b"[]", "not_object"),
    ],
)
def test_body_and_json_failures_are_distinct_and_remain_inside_fetch_timing(
    monkeypatch, capsys, oidc_factory, endpoint, content, reason
):
    verifier, token, _ = oidc_factory(endpoint, httpx.Response(200, content=content))
    row, error = observe(monkeypatch, capsys, verifier, token)
    assert_failure(row, error, endpoint, "document", reason, 200)
    assert row["stage_timings"][f"auth.{endpoint}"]["errors"] == 1


@pytest.mark.parametrize("endpoint", ["discovery", "jwks"])
def test_content_encoding_failure_is_distinct_from_json_encoding(
    monkeypatch, capsys, oidc_factory, endpoint
):
    class InvalidGzipBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"keys":[]}'

    verifier, token, _ = oidc_factory(
        endpoint,
        httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=InvalidGzipBody()),
    )
    row, error = observe(monkeypatch, capsys, verifier, token)
    assert_failure(row, error, endpoint, "document", "content_encoding", 200)
    assert row["stage_timings"][f"auth.{endpoint}"]["errors"] == 1


@pytest.mark.parametrize(
    ("failure", "category", "reason"),
    [
        (httpx.DecodingError(PRIVATE), "document", "content_encoding"),
        (UnicodeDecodeError("utf-8", b"\xff", 0, 1, PRIVATE), "document", "json_encoding"),
        (json.JSONDecodeError(PRIVATE, "", 0), "document", "json_invalid"),
        (RuntimeError(PRIVATE), "unexpected", "unknown"),
    ],
)
def test_non_transport_failures_do_not_report_a_typed_transport_cause(
    monkeypatch, capsys, oidc_factory, failure, category, reason
):
    failure.__cause__ = socket.gaierror(1, PRIVATE)
    verifier, token, _ = oidc_factory("jwks", failure)
    row, error = observe(monkeypatch, capsys, verifier, token)
    assert_failure(row, error, "jwks", category, reason, cause=None)


def test_read_timeout_after_headers_keeps_the_observed_http_status(
    monkeypatch, capsys, oidc_factory
):
    class InterruptedBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"keys":'
            raise httpx.ReadTimeout(PRIVATE)

    # 每次 GET 获得独立响应流；真实网络也不会复用已消费的 Response。
    verifier, token, _ = oidc_factory(
        "jwks", lambda request: httpx.Response(200, stream=InterruptedBody())
    )
    row, error = observe(monkeypatch, capsys, verifier, token)
    assert_failure(row, error, "jwks", "timeout", "read", 200)
    assert row["stage_timings"]["auth.jwks"]["count"] == 2
    assert row["stage_timings"]["auth.jwks"]["errors"] == 2


def test_discovery_mismatch_keeps_the_existing_fetch_timer_boundary(
    monkeypatch, capsys, oidc_factory
):
    verifier, token, calls = oidc_factory(
        "discovery",
        httpx.Response(200, json={"issuer": PRIVATE, "jwks_uri": f"{ISSUER}/jwks"}),
    )
    row, error = observe(monkeypatch, capsys, verifier, token)
    assert_failure(row, error, "discovery", "document", "discovery_mismatch", 200)
    assert calls == ["discovery"]
    assert row["stage_timings"]["auth.discovery"]["errors"] == 0
    assert "auth.jwks" not in row["stage_timings"]


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ({}, "jwks_keys"),
        ({"keys": PRIVATE}, "jwks_keys"),
        ({"keys": []}, "jwks_keys"),
        ({"keys": [{}] * 101}, "jwks_keys"),
        ({"keys": [PRIVATE]}, "jwk_entry"),
        ({"keys": [{}]}, "jwk_kid"),
        ({"keys": [{"kid": ""}]}, "jwk_kid"),
        ({"keys": [{"kid": "x" * 257}]}, "jwk_kid"),
        ({"keys": [{"kid": KID}, {"kid": KID}]}, "duplicate_kid"),
    ],
)
def test_jwks_structure_failure_is_correlated_without_changing_timer_semantics(
    monkeypatch, capsys, oidc_factory, document, reason
):
    verifier, token, calls = oidc_factory("jwks", httpx.Response(200, json=document))
    row, error = observe(monkeypatch, capsys, verifier, token)
    assert_failure(row, error, "jwks", "document", reason, 200)
    assert calls == ["discovery", "jwks"]
    assert row["stage_timings"]["auth.jwks"]["errors"] == 0


def test_jwk_material_failure_has_no_claim_or_key_values(monkeypatch, capsys, oidc_factory):
    verifier, token, _ = oidc_factory(
        "jwks", httpx.Response(200, json={"keys": [{"kid": KID, "kty": "RSA"}]})
    )
    row, error = observe(monkeypatch, capsys, verifier, token)
    assert_failure(row, error, "jwk", "document", "jwk_material")
    assert row["stage_timings"]["auth.jwks"]["errors"] == 0


def test_unexpected_dependency_error_is_fixed_and_contains_no_exception_name(
    monkeypatch, capsys, oidc_factory
):
    class PrivateDiagnosticFailure(RuntimeError):
        pass

    verifier, token, _ = oidc_factory("discovery", PrivateDiagnosticFailure(PRIVATE))
    row, error = observe(monkeypatch, capsys, verifier, token)
    assert_failure(row, error, "discovery", "unexpected", "unknown")
    assert "PrivateDiagnosticFailure" not in json.dumps(row)


def test_success_then_cache_hit_has_no_failure_fields(monkeypatch, capsys, oidc_factory):
    verifier, token, calls = oidc_factory()
    for _ in range(2):
        row, error = observe(monkeypatch, capsys, verifier, token, expected_error=None)
        assert row["status"] == "ok"
        assert "auth_failure" not in row
        assert error is None
    assert calls == ["discovery", "jwks"]
    assert "auth.discovery" not in row["stage_timings"]
    assert "auth.jwks" not in row["stage_timings"]


def test_recovery_then_cache_hit_and_invalid_token_do_not_reuse_diagnostics(
    monkeypatch, capsys, oidc_factory
):
    attempts = 0

    def once(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text=PRIVATE)
        return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"})

    verifier, token, calls = oidc_factory("discovery", once)
    row, error = observe(monkeypatch, capsys, verifier, token, expected_error=None)
    assert "auth_failure" not in row
    assert row["stage_timings"]["auth.discovery"]["count"] == 2
    assert row["stage_timings"]["auth.discovery"]["errors"] == 1
    assert row["stage_timings"]["auth.oidc"]["errors"] == 0
    row, _ = observe(monkeypatch, capsys, verifier, token, expected_error=None)
    assert "auth_failure" not in row
    assert "auth.discovery" not in row["stage_timings"]
    row, error = observe(monkeypatch, capsys, verifier, PRIVATE, expected_error=AuthenticationError)
    assert row["status"] == "unauthenticated"
    assert "auth_failure" not in row
    assert error.oidc_failure is None
    assert calls == ["discovery", "discovery", "jwks"]


def test_terminal_failure_then_next_request_success_does_not_reuse_diagnostics(
    monkeypatch, capsys, oidc_factory
):
    attempts = 0

    def recover_next_request(request):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            return httpx.Response(503, text=PRIVATE)
        return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"})

    verifier, token, calls = oidc_factory("discovery", recover_next_request)
    row, error = observe(monkeypatch, capsys, verifier, token)
    assert_failure(row, error, "discovery", "http", "status", 503)
    assert row["stage_timings"]["auth.discovery"]["count"] == 2
    row, _ = observe(monkeypatch, capsys, verifier, token, expected_error=None)
    assert "auth_failure" not in row
    assert row["stage_timings"]["auth.discovery"]["count"] == 1
    assert row["stage_timings"]["auth.discovery"]["errors"] == 0
    assert calls == ["discovery", "discovery", "discovery", "jwks"]


def test_ordinary_service_failure_has_no_oidc_diagnostic(monkeypatch, capsys):
    def fail():
        raise ServiceError()

    monkeypatch.setattr(runtime, "_request", fail)
    with pytest.raises(ServiceError) as caught:
        runtime.request_handler({}, SimpleNamespace(aws_request_id=REQUEST_ID))
    assert caught.value.oidc_failure is None
    row = json.loads(capsys.readouterr().out)
    assert "auth_failure" not in row

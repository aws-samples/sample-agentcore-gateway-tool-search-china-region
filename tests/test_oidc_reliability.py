"""认证只共享一次补偿 GET，保留验证、缓存和密钥轮换边界。"""

import json
import ssl

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from semantic_core._timing import collect
from semantic_service import auth
from semantic_service.auth import AuthenticationError, OIDCConfig, OIDCVerifier, ServiceError

NOW = 1_700_000_000
ISSUER = "https://idp.example.invalid"


@pytest.fixture(scope="module")
def signing_key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid="known", alg="RS256", use="sig")
    return private, jwk


class OIDCFixture:
    def __init__(self, private, jwk):
        self.private = private
        self.keys = [dict(jwk)]
        self.now = float(NOW)
        self.config = OIDCConfig(
            ISSUER,
            ISSUER + "/jwks",
            "gateway",
            frozenset({"idp.example.invalid"}),
            discovery_uri=ISSUER + "/discovery",
        )
        self.steps = {"discovery": [], "jwks": []}
        self.calls = []
        self.delays = []
        self.jitter_bounds = []
        self.client = httpx.Client(transport=httpx.MockTransport(self.transport), trust_env=False)
        self.verifier = OIDCVerifier(self.config, self.client, clock=lambda: self.now)

    def token(self, kid="known", **changes):
        return jwt.encode(
            {
                "iss": ISSUER,
                "sub": "synthetic-principal",
                "aud": "gateway",
                "iat": NOW,
                "exp": NOW + 300,
                "token_use": "access",
                **changes,
            },
            self.private,
            algorithm="RS256",
            headers={"kid": kid},
        )

    def document(self, endpoint):
        if endpoint == "discovery":
            return {"issuer": ISSUER, "jwks_uri": self.config.jwks_uri}
        return {"keys": self.keys}

    def transport(self, request):
        endpoint = request.url.path.removeprefix("/")
        assert endpoint in self.steps
        self.calls.append((endpoint, dict(request.extensions["timeout"])))
        if self.steps[endpoint]:
            step = self.steps[endpoint].pop(0)
            if isinstance(step, Exception):
                raise step
            if callable(step):
                return step(request)
            return step
        return httpx.Response(200, json=self.document(endpoint))

    def jitter(self, lower, upper):
        self.jitter_bounds.append((lower, upper))
        return 0.05

    @property
    def endpoints(self):
        return [endpoint for endpoint, _ in self.calls]


@pytest.fixture
def fixture(monkeypatch, signing_key):
    result = OIDCFixture(*signing_key)
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda unused, request: result.transport(request)
    )
    monkeypatch.setattr(auth.time, "sleep", result.delays.append)
    # 红测阶段尚无 random import；不让该差异变成 collection/setup failure。
    import random

    monkeypatch.setattr(random, "uniform", result.jitter)
    yield result
    result.client.close()


def assert_attempt_timeouts(fixture, values):
    assert [timeouts for _, timeouts in fixture.calls] == [
        dict.fromkeys(("connect", "read", "write", "pool"), value) for value in values
    ]


@pytest.mark.parametrize("endpoint", ["discovery", "jwks"])
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_one_transient_http_failure_recovers_with_one_short_retry(fixture, endpoint, status):
    fixture.steps[endpoint] = [httpx.Response(status)]
    with collect() as timings:
        assert fixture.verifier.verify(fixture.token()).subject == "synthetic-principal"
    assert fixture.endpoints == (
        ["discovery", "discovery", "jwks"]
        if endpoint == "discovery"
        else ["discovery", "jwks", "jwks"]
    )
    assert fixture.delays == [0.05]
    assert fixture.jitter_bounds == [(0.025, 0.075)]
    assert_attempt_timeouts(
        fixture, [2.0, 0.5, 2.0] if endpoint == "discovery" else [2.0, 2.0, 0.5]
    )
    stages = timings.snapshot()
    assert stages[f"auth.{endpoint}"]["count"] == 2
    assert stages[f"auth.{endpoint}"]["errors"] == 1
    assert stages["auth.oidc"]["errors"] == 0


@pytest.mark.parametrize("endpoint", ["discovery", "jwks"])
@pytest.mark.parametrize("exception", [httpx.ConnectTimeout, httpx.ReadTimeout])
def test_one_transient_timeout_recovers(fixture, endpoint, exception):
    fixture.steps[endpoint] = [exception("synthetic failure")]
    assert fixture.verifier.verify(fixture.token()).subject == "synthetic-principal"
    assert fixture.endpoints.count(endpoint) == 2
    assert fixture.delays == [0.05]


@pytest.mark.parametrize("endpoint", ["discovery", "jwks"])
@pytest.mark.parametrize("failure", ["http", "timeout"])
def test_permanent_failure_ends_after_one_compensation_get(fixture, endpoint, failure):
    fixture.steps[endpoint] = [
        httpx.Response(503) if failure == "http" else httpx.ReadTimeout("synthetic")
        for _ in range(2)
    ]
    with collect() as timings, pytest.raises(ServiceError) as caught:
        fixture.verifier.verify(fixture.token())
    assert type(caught.value) is ServiceError
    assert caught.value.oidc_failure.endpoint == endpoint
    assert caught.value.oidc_failure.category == failure
    assert fixture.endpoints.count(endpoint) == 2
    assert fixture.delays == [0.05]
    assert timings.snapshot()[f"auth.{endpoint}"]["count"] == 2
    assert timings.snapshot()[f"auth.{endpoint}"]["errors"] == 2


def test_discovery_and_jwks_share_one_retry(fixture):
    fixture.steps["discovery"] = [httpx.Response(500)]
    fixture.steps["jwks"] = [httpx.ReadTimeout("synthetic"), httpx.Response(503)]
    with pytest.raises(ServiceError) as caught:
        fixture.verifier.verify(fixture.token())
    assert caught.value.oidc_failure.endpoint == "jwks"
    assert caught.value.oidc_failure.category == "timeout"
    assert fixture.endpoints == ["discovery", "discovery", "jwks"]
    assert len(fixture.steps["jwks"]) == 1
    assert fixture.delays == [0.05]
    assert_attempt_timeouts(fixture, [2.0, 0.5, 2.0])


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 429, 501, 505])
def test_other_http_statuses_do_not_retry(fixture, status):
    fixture.steps["jwks"] = [httpx.Response(status)]
    with pytest.raises(ServiceError) as caught:
        fixture.verifier.verify(fixture.token())
    assert caught.value.oidc_failure.http_status == status
    assert fixture.endpoints == ["discovery", "jwks"]
    assert fixture.delays == []


@pytest.mark.parametrize(
    "exception",
    [
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.TimeoutException,
        httpx.ConnectError,
        httpx.ReadError,
        httpx.WriteError,
        httpx.CloseError,
        httpx.RemoteProtocolError,
        httpx.LocalProtocolError,
        httpx.DecodingError,
        RuntimeError,
    ],
)
def test_other_failure_types_do_not_retry(fixture, exception):
    fixture.steps["jwks"] = [exception("synthetic failure")]
    with pytest.raises(ServiceError):
        fixture.verifier.verify(fixture.token())
    assert fixture.endpoints == ["discovery", "jwks"]
    assert fixture.delays == []


@pytest.mark.parametrize("exception", [httpx.ConnectTimeout, httpx.ReadTimeout])
@pytest.mark.parametrize("cause", [ssl.SSLError, ssl.SSLCertVerificationError])
def test_explicit_tls_cause_prevents_timeout_retry(fixture, exception, cause):
    failure = exception("synthetic failure")
    failure.__cause__ = cause(1, "synthetic TLS cause")
    fixture.steps["jwks"] = [failure]
    with pytest.raises(ServiceError) as caught:
        fixture.verifier.verify(fixture.token())
    assert caught.value.oidc_failure.cause in {"tls", "tls_certificate"}
    assert fixture.endpoints == ["discovery", "jwks"]
    assert fixture.delays == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"{broken"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"keys": []}),
        httpx.Response(200, json={"keys": [{"kid": "same"}, {"kid": "same"}]}),
    ],
)
def test_document_failure_does_not_retry(fixture, response):
    fixture.steps["jwks"] = [response]
    with pytest.raises(ServiceError) as caught:
        fixture.verifier.verify(fixture.token())
    assert caught.value.oidc_failure.category == "document"
    assert fixture.endpoints == ["discovery", "jwks"]
    assert fixture.delays == []


def test_discovery_is_reused_only_after_exact_validation(fixture):
    fixture.steps["discovery"] = [
        httpx.Response(200, json={"issuer": "wrong", "jwks_uri": fixture.config.jwks_uri})
    ]
    with pytest.raises(ServiceError) as caught:
        fixture.verifier.verify(fixture.token())
    assert caught.value.oidc_failure.reason == "discovery_mismatch"
    assert fixture.endpoints == ["discovery"]
    assert fixture.delays == []
    fixture.verifier.verify(fixture.token())
    assert fixture.endpoints == ["discovery", "discovery", "jwks"]


def test_unknown_kid_keeps_second_jwks_refresh_cooldown_and_rotation(fixture):
    with pytest.raises(AuthenticationError):
        fixture.verifier.verify(fixture.token("missing"))
    assert fixture.endpoints == ["discovery", "jwks", "jwks"]
    assert fixture.verifier._last_unknown == NOW
    with pytest.raises(AuthenticationError):
        fixture.verifier.verify(fixture.token("missing-again"))
    assert fixture.endpoints == ["discovery", "jwks", "jwks"]
    fixture.keys.append(fixture.keys[0] | {"kid": "rotated"})
    fixture.now += 31
    assert fixture.verifier.verify(fixture.token("rotated")).subject == "synthetic-principal"
    assert fixture.endpoints == ["discovery", "jwks", "jwks", "discovery", "jwks"]
    assert fixture.delays == []


def test_unknown_kid_second_refresh_can_observe_rotation_in_same_request(fixture):
    first = httpx.Response(200, json=fixture.document("jwks"))
    rotated = fixture.keys[0] | {"kid": "rotated"}
    fixture.steps["jwks"] = [first, httpx.Response(200, json={"keys": [rotated]})]
    assert fixture.verifier.verify(fixture.token("rotated")).subject == "synthetic-principal"
    assert fixture.endpoints == ["discovery", "jwks", "jwks"]
    assert fixture.delays == []


def test_unknown_kid_second_refresh_can_use_the_remaining_retry(fixture):
    rotated = fixture.keys[0] | {"kid": "rotated"}
    fixture.steps["jwks"] = [
        httpx.Response(200, json=fixture.document("jwks")),
        httpx.Response(503),
        httpx.Response(200, json={"keys": [rotated]}),
    ]
    with collect() as timings:
        assert fixture.verifier.verify(fixture.token("rotated")).subject == "synthetic-principal"
    assert fixture.endpoints == ["discovery", "jwks", "jwks", "jwks"]
    assert fixture.delays == [0.05]
    assert_attempt_timeouts(fixture, [2.0, 2.0, 2.0, 0.5])
    assert timings.snapshot()["auth.jwks"]["count"] == 3
    assert timings.snapshot()["auth.jwks"]["errors"] == 1


def test_unknown_kid_refresh_does_not_reset_used_retry(fixture):
    fixture.steps["jwks"] = [
        httpx.Response(500),
        httpx.Response(200, json=fixture.document("jwks")),
        httpx.Response(503),
    ]
    with pytest.raises(ServiceError) as caught:
        fixture.verifier.verify(fixture.token("missing"))
    assert caught.value.oidc_failure.http_status == 503
    assert fixture.endpoints == ["discovery", "jwks", "jwks", "jwks"]
    assert fixture.delays == [0.05]
    assert_attempt_timeouts(fixture, [2.0, 2.0, 0.5, 2.0])


def test_failed_expired_refresh_never_uses_or_overwrites_old_cache(fixture):
    fixture.verifier.verify(fixture.token())
    before_keys, before_expiry = fixture.verifier._keys, fixture.verifier._expires
    fixture.now += 301
    fixture.steps["jwks"] = [httpx.Response(503), httpx.Response(503)]
    with pytest.raises(ServiceError):
        fixture.verifier.verify(fixture.token(iat=NOW + 301, exp=NOW + 601))
    assert fixture.verifier._keys is before_keys
    assert fixture.verifier._expires == before_expiry
    assert fixture.endpoints == ["discovery", "jwks", "discovery", "jwks", "jwks"]


def test_new_request_has_fresh_retry_and_discovery_state_then_warm_hit(fixture):
    fixture.steps["jwks"] = [httpx.Response(503), httpx.Response(503)]
    with pytest.raises(ServiceError):
        fixture.verifier.verify(fixture.token())
    fixture.steps["jwks"] = [httpx.ReadTimeout("synthetic")]
    assert fixture.verifier.verify(fixture.token()).subject == "synthetic-principal"
    assert fixture.endpoints == [
        "discovery",
        "jwks",
        "jwks",
        "discovery",
        "jwks",
        "jwks",
    ]
    assert fixture.delays == [0.05, 0.05]
    fixture.verifier.verify(fixture.token())
    assert len(fixture.calls) == 6
    assert fixture.verifier._expires == NOW + 300


def test_token_expiry_is_rechecked_after_retry_wait(fixture):
    fixture.now = NOW + 29.5

    def timeout_after_wait(request):
        fixture.now += 2
        raise httpx.ReadTimeout("synthetic")

    fixture.steps["jwks"] = [timeout_after_wait]
    with pytest.raises(AuthenticationError):
        fixture.verifier.verify(fixture.token(iat=NOW - 60, exp=NOW))
    assert fixture.endpoints == ["discovery", "jwks", "jwks"]
    assert fixture.delays == [0.05]

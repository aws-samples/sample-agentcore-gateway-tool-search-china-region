"""预取复用认证缓存与恢复边界，不提前消耗真实请求的状态。"""

import json
import math
from dataclasses import asdict

import httpx
import pytest
from test_oidc_reliability import (
    NOW,
    assert_attempt_timeouts,
    signing_key,  # noqa: F401
)
from test_oidc_reliability import fixture as oidc_fixture  # noqa: F401

from semantic_service.auth import AuthenticationError, ServiceError


@pytest.fixture
def fixture(oidc_fixture):  # noqa: F811
    return oidc_fixture


def test_prefetch_loads_discovery_and_jwks_for_first_verification(fixture):
    assert fixture.verifier._prefetch() is None
    assert fixture.endpoints == ["discovery", "jwks"]
    assert fixture.verifier._expires == NOW + 300
    assert fixture.verifier.verify(fixture.token()).subject == "synthetic-principal"
    assert fixture.endpoints == ["discovery", "jwks"]
    assert fixture.delays == []
    assert_attempt_timeouts(fixture, [2.0, 2.0])


@pytest.mark.parametrize("elapsed", [0, 299])
def test_repeated_prefetch_reuses_valid_cache_without_extending_ttl(fixture, elapsed):
    fixture.verifier._prefetch()
    fixture.now += elapsed
    fixture.verifier._prefetch()
    assert fixture.endpoints == ["discovery", "jwks"]
    assert fixture.verifier._expires == NOW + 300


@pytest.mark.parametrize("elapsed", [299, 300])
def test_prefetched_cache_expires_at_300_seconds(fixture, elapsed):
    fixture.verifier._prefetch()
    fixture.now += elapsed

    assert (
        fixture.verifier.verify(fixture.token(iat=NOW + elapsed, exp=NOW + elapsed + 300)).subject
        == "synthetic-principal"
    )
    assert fixture.endpoints == (
        ["discovery", "jwks"] if elapsed == 299 else ["discovery", "jwks"] * 2
    )
    assert fixture.verifier._expires == NOW + 300 + (elapsed if elapsed == 300 else 0)


@pytest.mark.parametrize("endpoint", ["discovery", "jwks"])
def test_failed_prefetch_leaves_no_cache_and_next_request_has_fresh_retry(fixture, endpoint):
    fixture.steps[endpoint] = [httpx.Response(503), httpx.Response(503)]
    with pytest.raises(ServiceError) as caught:
        fixture.verifier._prefetch()
    assert type(caught.value) is ServiceError
    assert caught.value.oidc_failure.endpoint == endpoint
    assert fixture.verifier._keys == {}
    assert fixture.verifier._expires == -math.inf
    assert fixture.verifier._last_unknown == -math.inf
    failed_endpoints = (
        ["discovery", "discovery"] if endpoint == "discovery" else ["discovery", "jwks", "jwks"]
    )
    assert fixture.endpoints == failed_endpoints

    fixture.now += 1
    fixture.steps[endpoint] = [httpx.ReadTimeout("synthetic prefetch recovery")]
    assert fixture.verifier.verify(fixture.token()).subject == "synthetic-principal"
    retry_endpoints = (
        ["discovery", "discovery", "jwks"]
        if endpoint == "discovery"
        else ["discovery", "jwks", "jwks"]
    )
    assert fixture.endpoints == failed_endpoints + retry_endpoints
    assert fixture.delays == [0.05, 0.05]
    assert fixture.verifier._expires == NOW + 301
    fixture.verifier.verify(fixture.token())
    assert fixture.endpoints == failed_endpoints + retry_endpoints


@pytest.mark.parametrize("endpoint", ["discovery", "jwks"])
def test_prefetch_recovers_once_with_existing_short_retry(fixture, endpoint):
    fixture.steps[endpoint] = [httpx.ReadTimeout("synthetic")]

    fixture.verifier._prefetch()

    expected = (
        ["discovery", "discovery", "jwks"]
        if endpoint == "discovery"
        else ["discovery", "jwks", "jwks"]
    )
    assert fixture.endpoints == expected
    assert fixture.delays == [0.05]
    assert fixture.jitter_bounds == [(0.025, 0.075)]
    assert_attempt_timeouts(
        fixture, [2.0, 0.5, 2.0] if endpoint == "discovery" else [2.0, 2.0, 0.5]
    )
    assert fixture.verifier.verify(fixture.token()).subject == "synthetic-principal"
    assert fixture.endpoints == expected


def test_prefetch_discovery_and_jwks_share_one_retry(fixture):
    fixture.steps["discovery"] = [httpx.Response(503)]
    fixture.steps["jwks"] = [httpx.ReadTimeout("synthetic"), httpx.Response(503)]

    with pytest.raises(ServiceError) as caught:
        fixture.verifier._prefetch()

    assert caught.value.oidc_failure.endpoint == "jwks"
    assert caught.value.oidc_failure.category == "timeout"
    assert fixture.endpoints == ["discovery", "discovery", "jwks"]
    assert len(fixture.steps["jwks"]) == 1
    assert fixture.delays == [0.05]
    assert fixture.verifier._keys == {}
    assert fixture.verifier._expires == -math.inf
    assert_attempt_timeouts(fixture, [2.0, 0.5, 2.0])


def test_prefetch_does_not_start_unknown_kid_cooldown(fixture):
    fixture.verifier._prefetch()
    assert fixture.verifier._last_unknown == -math.inf

    with pytest.raises(AuthenticationError):
        fixture.verifier.verify(fixture.token("missing"))
    assert fixture.endpoints == ["discovery", "jwks"] * 2
    assert fixture.verifier._last_unknown == NOW
    with pytest.raises(AuthenticationError):
        fixture.verifier.verify(fixture.token("missing-again"))
    assert fixture.endpoints == ["discovery", "jwks"] * 2

    fixture.keys.append(fixture.keys[0] | {"kid": "rotated"})
    fixture.now += 31
    assert fixture.verifier.verify(fixture.token("rotated")).subject == "synthetic-principal"
    assert fixture.endpoints == ["discovery", "jwks"] * 3
    assert fixture.verifier._last_unknown == NOW + 31
    assert fixture.delays == []


def test_prefetch_retry_does_not_consume_unknown_kid_rotation_retry(fixture):
    fixture.steps["jwks"] = [httpx.Response(503)]
    fixture.verifier._prefetch()

    fixture.keys.append(fixture.keys[0] | {"kid": "rotated"})
    fixture.steps["jwks"] = [httpx.ReadTimeout("synthetic")]
    assert fixture.verifier.verify(fixture.token("rotated")).subject == "synthetic-principal"
    assert fixture.endpoints == ["discovery", "jwks", "jwks"] * 2
    assert fixture.delays == [0.05, 0.05]
    assert fixture.verifier._last_unknown == NOW
    assert_attempt_timeouts(fixture, [2.0, 2.0, 0.5] * 2)


@pytest.mark.parametrize("endpoint", ["discovery", "jwks"])
@pytest.mark.parametrize(
    ("kind", "category", "reason"),
    [
        ("http", "http", "status"),
        ("timeout", "timeout", "read"),
        ("network", "network", "connect"),
        ("document", "document", "json_invalid"),
    ],
)
def test_prefetch_preserves_sanitized_failure_and_never_caches_it(
    fixture, endpoint, kind, category, reason
):
    private = "private-prefetch-fixture-token-and-url"
    if kind == "http":
        fixture.steps[endpoint] = [httpx.Response(403, text=private)]
    elif kind == "timeout":
        fixture.steps[endpoint] = [httpx.ReadTimeout(private), httpx.ReadTimeout(private)]
    elif kind == "network":
        fixture.steps[endpoint] = [httpx.ConnectError(private)]
    else:
        fixture.steps[endpoint] = [httpx.Response(200, text=f"{private} {{broken")]

    with pytest.raises(ServiceError) as caught:
        fixture.verifier._prefetch()

    assert type(caught.value) is ServiceError
    assert str(caught.value) == "internal failure"
    failure = caught.value.oidc_failure
    assert failure.endpoint == endpoint
    assert failure.category == category
    assert failure.reason == reason
    assert failure.http_status == (403 if kind == "http" else 200 if kind == "document" else None)
    assert private not in json.dumps(asdict(failure))
    assert fixture.verifier._keys == {}
    assert fixture.verifier._expires == -math.inf
    assert fixture.endpoints == (
        ["discovery"] if endpoint == "discovery" else ["discovery", "jwks"]
    ) + ([endpoint] if kind == "timeout" else [])
    assert fixture.delays == ([0.05] if kind == "timeout" else [])

    previous = list(fixture.endpoints)
    assert fixture.verifier.verify(fixture.token()).subject == "synthetic-principal"
    assert fixture.endpoints == previous + ["discovery", "jwks"]


def test_prefetch_rejects_discovery_mismatch_without_caching_it(fixture):
    fixture.steps["discovery"] = [
        httpx.Response(
            200,
            json={"issuer": "untrusted-issuer", "jwks_uri": fixture.config.jwks_uri},
        )
    ]

    with pytest.raises(ServiceError) as caught:
        fixture.verifier._prefetch()

    assert caught.value.oidc_failure.endpoint == "discovery"
    assert caught.value.oidc_failure.category == "document"
    assert caught.value.oidc_failure.reason == "discovery_mismatch"
    assert fixture.endpoints == ["discovery"]
    assert fixture.verifier._keys == {}
    assert fixture.verifier._expires == -math.inf
    assert fixture.delays == []

    assert fixture.verifier.verify(fixture.token()).subject == "synthetic-principal"
    assert fixture.endpoints == ["discovery", "discovery", "jwks"]

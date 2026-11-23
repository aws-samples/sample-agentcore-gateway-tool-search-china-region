"""Only usable configured signing public keys may enter an OIDC cache."""

import base64
import json
import math
import time
from dataclasses import replace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from test_oidc_reliability import fixture as oidc_fixture  # noqa: F401
from test_oidc_reliability import signing_key  # noqa: F401

from semantic_service import _request_prefetch
from semantic_service.auth import (
    AuthenticationError,
    ServiceError,
    collect_oidc_diagnostics,
)


@pytest.fixture
def fixture(oidc_fixture):  # noqa: F811
    return oidc_fixture


@pytest.mark.parametrize(
    "bad_key",
    [
        {"kid": "known"},
        {"kid": "known", "kty": "RSA"},
        {"kid": "known", "kty": "RSA", "n": "broken", "e": "AA"},
        {"kid": "known", "kty": "EC", "crv": "P-256", "x": "bad", "y": "bad"},
        {"kid": "known", "kty": "oct", "k": "c2VjcmV0", "alg": "HS256"},
    ],
)
@pytest.mark.parametrize("compensate", [False, True])
@pytest.mark.parametrize("entrypoint", ["prefetch", "request"])
def test_bad_material_never_enters_cache_and_next_request_can_reload(
    fixture, bad_key, compensate, entrypoint
):
    fixture.steps["jwks"] = ([httpx.Response(503)] if compensate else []) + [
        httpx.Response(200, json={"keys": [bad_key]})
    ]
    action = (
        fixture.verifier._prefetch
        if entrypoint == "prefetch"
        else (lambda: fixture.verifier.verify(fixture.token()))
    )
    with pytest.raises(ServiceError) as caught:
        action()
    assert caught.value.oidc_failure.endpoint == "jwk"
    assert caught.value.oidc_failure.reason == "jwk_material"
    assert fixture.verifier._keys == {}
    assert fixture.verifier._expires == -math.inf
    prior_calls = len(fixture.calls)
    assert fixture.verifier.verify(fixture.token()).subject == "synthetic-principal"
    assert fixture.endpoints[prior_calls:] == ["discovery", "jwks"]


def test_compensation_bad_material_is_failed_metadata_recovery_not_auth_recovery(fixture):
    fixture.steps["jwks"] = [
        httpx.Response(503),
        httpx.Response(200, json={"keys": [{"kid": "known"}]}),
    ]
    fields, _ = _request_prefetch._collect_prefetch(
        fixture.verifier, lambda _: None, deadline=time.monotonic() + 3
    )
    assert fields["status"] == "failed"
    assert fields["failure_kind"] == "dependency"
    assert fields["auth_failure"]["reason"] == "jwk_material"
    assert fields["metadata_recovered"] is True
    assert "auth_recovered" not in fields
    assert fixture.verifier._keys == {}


def test_mixed_jwks_retains_only_usable_configured_public_keys(fixture):
    curve = ec.generate_private_key(ec.SECP256R1())
    ec_key = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(curve.public_key()))
    ec_key.update(kid="ec", alg="ES256", use="sig")
    ed_key = json.loads(
        jwt.algorithms.OKPAlgorithm.to_jwk(ed25519.Ed25519PrivateKey.generate().public_key())
    )
    ed_key.update(kid="ed", alg="EdDSA")
    fixture.keys += [
        ec_key,
        ed_key,
        {"kid": "broken", "kty": "RSA"},
        fixture.keys[0] | {"kid": "encrypt", "use": "enc"},
        fixture.keys[0] | {"kid": "sign", "key_ops": ["sign"]},
        fixture.keys[0] | {"kid": "wrong-alg", "alg": "RS512"},
        {"kid": "secret", "kty": "oct", "k": "c2VjcmV0", "alg": "HS256"},
    ]
    fixture.verifier.config = replace(fixture.config, algorithms=("RS256", "ES256"))
    fixture.verifier._prefetch()
    assert set(fixture.verifier._keys) == {"known", "ec"}
    assert fixture.verifier.verify(fixture.token()).subject == "synthetic-principal"
    token = jwt.encode(
        {
            "iss": fixture.config.issuer,
            "aud": fixture.config.audience,
            "sub": "ec-principal",
            "iat": int(fixture.now),
            "exp": int(fixture.now) + 300,
            "token_use": "access",
        },
        curve,
        algorithm="ES256",
        headers={"kid": "ec"},
    )
    assert fixture.verifier.verify(token).subject == "ec-principal"


def test_public_cache_drops_remote_metadata_and_never_retains_private_material(fixture):
    fixture.keys[0].update(
        x5u="https://private.invalid/cert", arbitrary="private-remote-value", x5c=["private-cert"]
    )
    private_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(fixture.private))
    private_jwk.update(kid="private", alg="RS256")
    fixture.keys.append(private_jwk)
    fixture.verifier._prefetch()
    assert set(fixture.verifier._keys) == {"known"}
    assert set(fixture.verifier._keys["known"]) == {
        "kid",
        "kty",
        "n",
        "e",
        "alg",
        "use",
        "key_ops",
    }
    assert "private" not in json.dumps(fixture.verifier._keys)


def test_missing_alg_retains_compatible_algorithms_without_accepting_wrong_curve(fixture):
    fixture.keys[0].pop("alg")
    curve = ec.generate_private_key(ec.SECP384R1())
    wrong_curve = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(curve.public_key()))
    wrong_curve.update(kid="wrong-curve", alg="ES256")
    fixture.keys.append(wrong_curve)
    fixture.verifier.config = replace(fixture.config, algorithms=("RS256", "RS384", "ES256"))
    fixture.verifier._prefetch()
    assert set(fixture.verifier._keys) == {"known"}
    assert "alg" not in fixture.verifier._keys["known"]
    token = jwt.encode(
        {
            "iss": fixture.config.issuer,
            "aud": fixture.config.audience,
            "sub": "rsa-principal",
            "iat": int(fixture.now),
            "exp": int(fixture.now) + 300,
            "token_use": "access",
        },
        fixture.private,
        algorithm="RS384",
        headers={"kid": "known"},
    )
    assert fixture.verifier.verify(token).subject == "rsa-principal"


@pytest.mark.parametrize("kid", ["", " ", "x" * 257])
def test_invalid_key_identifier_is_rejected_even_beside_valid_material(fixture, kid):
    fixture.keys.append(fixture.keys[0] | {"kid": kid})
    with pytest.raises(ServiceError) as caught:
        fixture.verifier._prefetch()
    assert caught.value.oidc_failure.reason == "jwk_kid"
    assert fixture.verifier._keys == {}


@pytest.mark.parametrize("field", ["n", "e"])
def test_invalid_base64url_is_not_silently_normalized_into_a_usable_key(fixture, field):
    fixture.keys[0][field] += "!"
    with pytest.raises(ServiceError) as caught:
        fixture.verifier._prefetch()
    assert caught.value.oidc_failure.reason == "jwk_material"
    assert fixture.verifier._keys == {}


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"use": "SIG"}, "usage"),
        ({"use": "enc"}, "usage"),
        ({"key_ops": ["verify", "sign"]}, "key_ops"),
        ({"alg": "RS512"}, "algorithm"),
        ({"n": None}, "missing_material"),
        ({"e": "AA"}, "material"),
        ({"n": "padded"}, "encoding"),
    ],
)
def test_partial_jwks_discard_is_counted_without_disclosing_key_identity(fixture, change, reason):
    rejected = fixture.keys[0] | {"kid": "private-active-identity"} | change
    if change.get("n") == "padded":
        rejected["n"] = fixture.keys[0]["n"] + "=="
    fixture.keys.append(rejected)
    with collect_oidc_diagnostics() as diagnostics:
        assert fixture.verifier.verify(fixture.token()).subject == "synthetic-principal"
    assert diagnostics["jwks_validation"] == {
        "accepted": 1,
        "rejected": 1,
        "rejection_reasons": {reason: 1},
    }
    assert set(fixture.verifier._keys) == {"known"}
    serialized = json.dumps(diagnostics)
    assert "private-active-identity" not in serialized
    assert fixture.keys[0]["n"] not in serialized
    assert fixture.config.issuer not in serialized


def test_jwks_discard_diagnostics_accumulate_both_refreshes_without_persisting(fixture):
    fixture.steps["jwks"] = [
        httpx.Response(200, json={"keys": fixture.keys}),
        httpx.Response(200, json={"keys": [*fixture.keys, {"kid": "missing"}]}),
    ]
    with collect_oidc_diagnostics() as diagnostics, pytest.raises(AuthenticationError):
        fixture.verifier.verify(fixture.token("missing"))
    assert diagnostics["jwks_validation"] == {
        "accepted": 2,
        "rejected": 1,
        "rejection_reasons": {"missing_material": 1},
    }
    with collect_oidc_diagnostics() as following:
        fixture.verifier.verify(fixture.token())
    assert following == {}


def test_jwks_validation_distinguishes_missing_material_from_bad_encoding(fixture):
    fixture.keys = [
        {"kid": "missing"},
        fixture.keys[0] | {"kid": "bad-encoding", "n": fixture.keys[0]["n"] + "=="},
    ]
    with collect_oidc_diagnostics() as diagnostics, pytest.raises(ServiceError):
        fixture.verifier._prefetch()
    assert diagnostics["jwks_validation"] == {
        "accepted": 0,
        "rejected": 2,
        "rejection_reasons": {"missing_material": 1, "encoding": 1},
    }


def test_healthy_jwks_does_not_add_or_inherit_discard_diagnostics(fixture):
    with collect_oidc_diagnostics() as diagnostics:
        fixture.verifier.verify(fixture.token())
    assert diagnostics == {}


@pytest.mark.parametrize("claimed_algorithm", ["ES256", "EdDSA"])
def test_missing_alg_cross_family_token_is_authentication_failure(fixture, claimed_algorithm):
    fixture.keys[0].pop("alg")
    fixture.verifier.config = replace(
        fixture.config, algorithms=("RS256", "RS384", "ES256", "EdDSA")
    )
    fixture.verifier._prefetch()
    private = (
        ec.generate_private_key(ec.SECP256R1())
        if claimed_algorithm == "ES256"
        else ed25519.Ed25519PrivateKey.generate()
    )
    token = jwt.encode(
        {
            "iss": fixture.config.issuer,
            "aud": fixture.config.audience,
            "sub": "synthetic-principal",
            "iat": int(fixture.now),
            "exp": int(fixture.now) + 300,
            "token_use": "access",
        },
        private,
        algorithm=claimed_algorithm,
        headers={"kid": "known"},
    )
    with pytest.raises(AuthenticationError):
        fixture.verifier.verify(token)
    assert fixture.endpoints == ["discovery", "jwks"]
    assert fixture.verifier.verify(fixture.token()).subject == "synthetic-principal"


@pytest.mark.parametrize("algorithm", ["RS256", "ES256", "EdDSA"])
def test_private_material_reaches_private_guard_with_verify_permission(fixture, algorithm):
    private = (
        fixture.private
        if algorithm == "RS256"
        else ec.generate_private_key(ec.SECP256R1())
        if algorithm == "ES256"
        else ed25519.Ed25519PrivateKey.generate()
    )
    raw = json.loads(jwt.algorithms.get_default_algorithms()[algorithm].to_jwk(private))
    raw.update(kid="private-key", alg=algorithm, use="sig", key_ops=["verify"])
    assert "d" in raw and raw["key_ops"] == ["verify"]
    fixture.verifier.config = replace(fixture.config, algorithms=("RS256", "ES256", "EdDSA"))
    fixture.keys.append(raw)
    with collect_oidc_diagnostics() as diagnostics:
        fixture.verifier._prefetch()
    assert set(fixture.verifier._keys) == {"known"}
    assert diagnostics["jwks_validation"] == {
        "accepted": 1,
        "rejected": 1,
        "rejection_reasons": {"private_material": 1},
    }


def test_noncanonical_last_bits_reach_the_roundtrip_guard(fixture):
    canonical = fixture.keys[0]["n"]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    replacement = alphabet[alphabet.index(canonical[-1]) + 1]
    noncanonical = canonical[:-1] + replacement

    def padded(value):
        return value + "=" * (-len(value) % 4)

    assert noncanonical != canonical
    assert base64.b64decode(padded(noncanonical), altchars=b"-_", validate=True) == (
        base64.b64decode(padded(canonical), altchars=b"-_", validate=True)
    )
    fixture.keys[0]["n"] = noncanonical
    with collect_oidc_diagnostics() as diagnostics, pytest.raises(ServiceError):
        fixture.verifier._prefetch()
    assert fixture.verifier._keys == {}
    assert diagnostics["jwks_validation"] == {
        "accepted": 0,
        "rejected": 1,
        "rejection_reasons": {"encoding": 1},
    }

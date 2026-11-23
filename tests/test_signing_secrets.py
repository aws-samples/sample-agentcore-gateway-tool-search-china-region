"""Signing key stage overlap and unknown-kid refresh never broaden acceptance."""

import base64
import json

import pytest

from semantic_service.secrets import SecretError, SecretsCache, SigningSecretsKeys


class SecretsStub:
    def __init__(self, current, previous=None):
        self.current = current
        self.previous = previous
        self.calls = []

    def get_secret_value(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["VersionStage"] == "AWSPREVIOUS":
            if self.previous is None:
                raise RuntimeError("synthetic service exception")
            return self.previous
        return self.current


def key(version, activated=1000, material=b"s" * 32):
    return {
        "VersionId": version,
        "SecretString": json.dumps(
            {"key_b64": base64.b64encode(material).decode(), "activated_at": activated}
        ),
    }


def test_signing_current_and_previous_overlap_has_explicit_upper_bound():
    now = [1000.0]
    sdk = SecretsStub(key("new"), key("old", 500, b"o" * 32))
    keys = SigningSecretsKeys(SecretsCache(sdk), "signing", clock=lambda: now[0])
    assert keys.current() == ("new", b"s" * 32)
    assert keys.resolve("old") == b"o" * 32
    now[0] = 1390.1
    with pytest.raises(SecretError):
        keys.resolve("old")
    assert keys.resolve("new") == b"s" * 32


def test_unknown_kid_refreshes_once_then_backs_off_even_if_previous_missing():
    sdk = SecretsStub(key("current"))
    keys = SigningSecretsKeys(SecretsCache(sdk), "signing", clock=lambda: 1000.0)
    for _ in range(3):
        with pytest.raises(SecretError):
            keys.resolve("unknown")
    assert len(sdk.calls) <= 4


def test_unknown_kid_refresh_can_observe_new_current():
    sdk = SecretsStub(key("old", 500))
    keys = SigningSecretsKeys(SecretsCache(sdk), "signing", clock=lambda: 1000.0)
    assert keys.current()[0] == "old"
    sdk.previous = sdk.current
    sdk.current = key("new")
    assert keys.resolve("new") == b"s" * 32


def test_emergency_revoked_key_never_uses_previous_overlap():
    sdk = SecretsStub(key("new"), key("old", 500))
    keys = SigningSecretsKeys(
        SecretsCache(sdk), "signing", clock=lambda: 1000.0, revoked_kids=frozenset({"old"})
    )
    with pytest.raises(SecretError):
        keys.resolve("old")
    assert sdk.calls == []


@pytest.mark.parametrize("invalid", [key("short", material=b"s"), key("future", 2000)])
def test_invalid_signing_material_is_rejected(invalid):
    keys = SigningSecretsKeys(SecretsCache(SecretsStub(invalid)), "signing", clock=lambda: 1000.0)
    with pytest.raises(SecretError):
        keys.current()


def test_overlap_recomputed_from_actual_cache_ttl():
    cache = SecretsCache(SecretsStub(key("new")), ttl_seconds=300)
    with pytest.raises(ValueError, match="overlap"):
        SigningSecretsKeys(cache, "signing", previous_window_seconds=389)

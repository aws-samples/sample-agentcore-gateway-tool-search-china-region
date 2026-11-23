"""Pinned deployment configuration is bounded, immutable, and namespace verified."""

from __future__ import annotations

import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from semantic_core.catalog import GatewayIdentity
from semantic_service import runtime
from semantic_service.deployment_config import (
    CONFIG_REFERENCE_FIELDS,
    ConfigurationError,
    ConfigurationReference,
    load_configuration,
)

GATEWAY = GatewayIdentity("111122223333", "cn-north-1", "gateway-fixture")


def document(operation="indexer", settings=None, gateway=None):
    return {
        "version": 1,
        "gateway": (gateway or GATEWAY).as_dict(),
        "operation": operation,
        "settings": settings or {"SEMSEARCH_REGISTRY_JSON": "[]"},
    }


def reference(data, operation="indexer"):
    digest = hashlib.sha256(data).hexdigest()
    return {
        "SEMSEARCH_CONFIG_BUCKET": "configuration-fixture",
        "SEMSEARCH_CONFIG_KEY": (
            f"111122223333/cn-north-1/gateway-fixture/configuration/{operation}/{digest}.json"
        ),
        "SEMSEARCH_CONFIG_VERSION_ID": "immutable-version-1",
        "SEMSEARCH_CONFIG_SHA256": digest,
        "SEMSEARCH_CONFIG_BYTES": str(len(data)),
    }


class S3:
    def __init__(self, data, *, version="immutable-version-1", size=None):
        self.data, self.version, self.size = data, version, size
        self.calls = []
        self.body = None

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        self.body = io.BytesIO(self.data)
        return {
            "Body": self.body,
            "VersionId": self.version,
            "ContentLength": len(self.data) if self.size is None else self.size,
        }


def read(data, *, operation="indexer", client=None):
    pointer = ConfigurationReference.from_env(reference(data, operation), GATEWAY, operation)
    assert pointer is not None
    return load_configuration(client or S3(data), pointer, GATEWAY, operation)


def test_load_500_tool_registry_without_environment_expansion():
    registry = [
        {
            "name": f"business___tool_{index}",
            "target_id": "business-target",
            "incarnation": "reviewed-v1",
            "role": "business",
        }
        for index in range(500)
    ]
    data = json.dumps(document(settings={"SEMSEARCH_REGISTRY_JSON": json.dumps(registry)})).encode()
    assert len(data) > 4096
    client = S3(data)
    settings = read(data, client=client)
    assert len(json.loads(settings["SEMSEARCH_REGISTRY_JSON"])) == 500
    assert client.calls == [
        {
            "Bucket": "configuration-fixture",
            "Key": reference(data)["SEMSEARCH_CONFIG_KEY"],
            "VersionId": "immutable-version-1",
        }
    ]
    assert client.body.closed
    with pytest.raises(TypeError):
        settings["SEMSEARCH_REGISTRY_JSON"] = "[]"


@pytest.mark.parametrize(
    "change",
    (
        {"SEMSEARCH_CONFIG_VERSION_ID": "null"},
        {"SEMSEARCH_CONFIG_VERSION_ID": ""},
        {"SEMSEARCH_CONFIG_BYTES": "1048577"},
        {"SEMSEARCH_CONFIG_BYTES": "0"},
        {"SEMSEARCH_CONFIG_SHA256": "not-a-hash"},
        {"SEMSEARCH_CONFIG_BUCKET": "wrong/bucket"},
        {"SEMSEARCH_CONFIG_KEY": "other/gateway/configuration/indexer/fixture.json"},
    ),
)
def test_reject_invalid_pinned_reference_before_s3(change):
    data = json.dumps(document()).encode()
    with pytest.raises(ConfigurationError):
        ConfigurationReference.from_env(reference(data) | change, GATEWAY, "indexer")


def test_reference_fallback_requires_all_fields_absent():
    assert ConfigurationReference.from_env({}, GATEWAY, "indexer") is None
    for field in CONFIG_REFERENCE_FIELDS:
        with pytest.raises(ConfigurationError):
            ConfigurationReference.from_env({field: "fixture"}, GATEWAY, "indexer")


@pytest.mark.parametrize("change", ({"version": "other"}, {"size": 0}, {"size": True}))
def test_reject_version_or_object_size_mismatch(change):
    data = json.dumps(document()).encode()
    with pytest.raises(ConfigurationError):
        read(data, client=S3(data, **change))


def test_reject_changed_bytes_even_at_expected_size():
    data = json.dumps(document()).encode()
    damaged = data.replace(b"indexer", b"indexez")
    assert len(damaged) == len(data)
    with pytest.raises(ConfigurationError):
        read(data, client=S3(damaged))


@pytest.mark.parametrize(
    "payload",
    (
        {"version": 2},
        {"version": True},
        {"operation": "search"},
        {"gateway": GatewayIdentity("111122223333", "cn-north-1", "other-gateway").as_dict()},
        {"unrecognized": True},
        {"settings": {"SEMSEARCH_ACCOUNT": "999900001111"}},
        {"settings": {"SEMSEARCH_TABLE": "other-table"}},
        {"settings": {"SEMSEARCH_PROTECTED_TABLE": "other-table"}},
        {"settings": {"SEMSEARCH_SIGNING_SECRET_ID": "other-secret"}},
        {"settings": {"MODEL_SECRET_ID": "other-secret"}},
        {"settings": {"MODEL_API_KEY": "secret-fixture"}},
        {"settings": {"UNKNOWN_OPTION": "true"}},
        {"settings": {"SEMSEARCH_REGISTRY_JSON": []}},
    ),
)
def test_reject_untrusted_document_shape_namespace_and_settings(payload):
    with pytest.raises(ConfigurationError):
        read(json.dumps(document() | payload).encode())


@pytest.mark.parametrize(
    "data",
    (
        b'{"version":1,"version":1}',
        b'{"version":NaN}',
        b"\xff",
        b"[]",
    ),
)
def test_reject_duplicate_keys_nonfinite_values_and_nonobject_json(data):
    with pytest.raises(ConfigurationError):
        read(data)


def test_runtime_cache_does_not_mutate_process_environment(monkeypatch):
    data = json.dumps(document(settings={"SEMSEARCH_REGISTRY_JSON": "[]"})).encode()
    for key, value in reference(data).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SEMSEARCH_REGISTRY_JSON", "legacy-setting")
    monkeypatch.setattr(runtime, "_settings", lambda: SimpleNamespace(gateway=GATEWAY))
    client = S3(data)
    monkeypatch.setattr(runtime, "_aws", lambda _: client)
    runtime._loaded_configuration.cache_clear()
    try:
        first = runtime._configuration("indexer")
        second = runtime._configuration("indexer")
        assert first["SEMSEARCH_REGISTRY_JSON"] == second["SEMSEARCH_REGISTRY_JSON"] == "[]"
        assert runtime.os.environ["SEMSEARCH_REGISTRY_JSON"] == "legacy-setting"
        assert len(client.calls) == 1
    finally:
        runtime._loaded_configuration.cache_clear()


def test_runtime_initializes_500_tools_from_pinned_config(monkeypatch):
    registry = [
        {
            "name": "discovery___search_tools",
            "target_id": "target-fixture",
            "incarnation": "reviewed-v1",
            "role": "discovery",
        }
    ] + [
        {
            "name": f"business___tool_{index}",
            "target_id": "business-target",
            "incarnation": "reviewed-v1",
            "role": "business",
        }
        for index in range(500)
    ]
    data = json.dumps(document(settings={"SEMSEARCH_REGISTRY_JSON": json.dumps(registry)})).encode()
    for key, value in reference(data).items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SEMSEARCH_REGISTRY_JSON", raising=False)
    monkeypatch.setenv("SEMSEARCH_SEARCH_INCARNATION", "reviewed-v1")
    monkeypatch.setattr(
        runtime,
        "_settings",
        lambda: SimpleNamespace(
            gateway=GATEWAY,
            search_tool_name="discovery___search_tools",
            search_target_id="target-fixture",
        ),
    )
    client = S3(data)
    monkeypatch.setattr(runtime, "_aws", lambda _: client)
    runtime._registry.cache_clear()
    runtime._loaded_configuration.cache_clear()
    try:
        assert len(runtime._registry().tools) == 501
        assert len(client.calls) == 1
        assert "SEMSEARCH_REGISTRY_JSON" not in runtime.os.environ
    finally:
        runtime._registry.cache_clear()
        runtime._loaded_configuration.cache_clear()


def test_disabled_enhancer_rejects_partial_configuration(monkeypatch):
    for field in CONFIG_REFERENCE_FIELDS:
        monkeypatch.delenv(field, raising=False)
    monkeypatch.setenv("SEMSEARCH_CONFIG_BUCKET", "configuration-fixture")
    monkeypatch.setenv("SEMSEARCH_MODELS_ENABLED", "false")
    monkeypatch.setattr(runtime, "_settings", lambda: SimpleNamespace(gateway=GATEWAY))
    monkeypatch.setattr(runtime, "_aws", lambda _: pytest.fail("partial config must not call S3"))
    with pytest.raises(runtime.ServiceError, match="internal failure"):
        runtime.enhancer_handler({}, object())


def test_expired_ticks_do_not_read_deployment_configuration(monkeypatch):
    monkeypatch.setattr(
        runtime, "_configuration", lambda _: pytest.fail("expired tick initialized configuration")
    )
    for handler in (runtime.indexer_handler, runtime.enhancer_handler):
        assert handler({"time": "2000-01-01T00:00:00Z"}, object()) == {"status": "expired"}

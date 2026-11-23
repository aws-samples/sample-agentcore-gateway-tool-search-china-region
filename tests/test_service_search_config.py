"""Shared service search settings preserve the core and public wire contracts."""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from infra.config import DeploymentConfig
from infra.stack import documents

from semantic_core.catalog import GatewayIdentity
from semantic_core.search import SearchConfig
from semantic_service import config, runtime
from semantic_service.deployment_config import (
    CONFIG_REFERENCE_FIELDS,
    ConfigurationError,
    parse_configuration,
)
from semantic_service.models import ModelSettings

GATEWAY = GatewayIdentity("111122223333", "cn-north-1", "gateway-fixture")
SEARCH_FIELDS = (
    "SEMSEARCH_FUSION_STRATEGY",
    "SEMSEARCH_MIN_VECTOR_SIMILARITY",
    "SEMSEARCH_MIN_VECTOR_MARGIN",
)


def document(settings):
    return json.dumps(
        {
            "version": 1,
            "gateway": GATEWAY.as_dict(),
            "operation": "search",
            "settings": settings,
        }
    ).encode()


def test_legacy_service_search_defaults_do_not_change_core_defaults():
    settings = parse_configuration(document({"SEMSEARCH_TOP_K": "7"}), GATEWAY, "search")
    parsed = config.parse_search_config(settings)
    assert parsed == SearchConfig(top_k=7, fusion_strategy="vector_bm25_fallback")
    assert parsed.rerank_enabled is False
    assert parsed.min_vector_similarity is parsed.min_vector_margin is None
    assert parsed.bm25_fallback_similarity == 0.5
    assert parsed.embedding_timeout == 1.0
    assert parsed.rerank_timeout == 0.45
    assert SearchConfig().fusion_strategy == "rrf"
    assert config.parse_search_config({}).top_k == 5


@pytest.mark.parametrize("mode", ["rrf", "vector", "vector_bm25_fallback"])
@pytest.mark.parametrize(("similarity", "margin"), [("-1", "0"), ("1", "2"), ("0.6", "0.1")])
def test_search_configuration_accepts_all_modes_and_finite_threshold_boundaries(
    mode, similarity, margin
):
    values = {
        "SEMSEARCH_FUSION_STRATEGY": mode,
        "SEMSEARCH_MIN_VECTOR_SIMILARITY": similarity,
        "SEMSEARCH_MIN_VECTOR_MARGIN": margin,
        "MODEL_RERANK_ENABLED": "true",
    }
    settings = parse_configuration(document(values), GATEWAY, "search")
    assert dict(settings) == values
    parsed = config.parse_search_config(settings)
    assert parsed.fusion_strategy == mode
    assert parsed.min_vector_similarity == float(similarity)
    assert parsed.min_vector_margin == float(margin)
    assert parsed.rerank_enabled is True
    assert parsed.bm25_fallback_similarity == 0.5


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SEMSEARCH_FUSION_STRATEGY", "bm25"),
        ("SEMSEARCH_FUSION_STRATEGY", "RRF"),
        ("SEMSEARCH_FUSION_STRATEGY", ""),
        ("SEMSEARCH_FUSION_STRATEGY", 1),
        ("SEMSEARCH_MIN_VECTOR_SIMILARITY", "-1.0001"),
        ("SEMSEARCH_MIN_VECTOR_SIMILARITY", "1.0001"),
        ("SEMSEARCH_MIN_VECTOR_MARGIN", "-0.0001"),
        ("SEMSEARCH_MIN_VECTOR_MARGIN", "2.0001"),
        *[
            (field, value)
            for field in ("SEMSEARCH_MIN_VECTOR_SIMILARITY", "SEMSEARCH_MIN_VECTOR_MARGIN")
            for value in ("NaN", "inf", "-inf", "1e309", "", "none", "true", True, 0.5)
        ],
        ("MODEL_RERANK_ENABLED", "yes"),
        ("MODEL_RERANK_ENABLED", "1"),
        ("MODEL_RERANK_ENABLED", ""),
        ("MODEL_RERANK_ENABLED", True),
        ("SEMSEARCH_TOP_K", "0"),
        ("SEMSEARCH_TOP_K", "21"),
        ("SEMSEARCH_TOP_K", "1.5"),
        ("SEMSEARCH_TOP_K", "nan"),
        ("SEMSEARCH_TOP_K", True),
    ],
)
def test_search_configuration_rejects_invalid_values_with_the_field_name(field, value):
    with pytest.raises(ValueError, match=field):
        config.parse_search_config({field: value})


@pytest.mark.parametrize("field", SEARCH_FIELDS)
@pytest.mark.parametrize("value", [None, True, 0.5])
def test_version_one_search_settings_remain_string_valued(field, value):
    with pytest.raises(ConfigurationError):
        parse_configuration(document({field: value}), GATEWAY, "search")


@pytest.mark.parametrize("rerank", ["true", "false", "TRUE", "FALSE"])
def test_service_and_model_settings_agree_on_existing_rerank_boolean(rerank):
    values = {"MODEL_ALLOWED_HOSTS": "model.example.invalid", "MODEL_RERANK_ENABLED": rerank}
    parsed = config.parse_search_config(values)
    models = ModelSettings.from_env(values)
    assert parsed.rerank_enabled is (models.rerank is not None)
    assert parsed.rerank_enabled is (rerank.lower() == "true")


def test_pinned_settings_override_environment_without_materializing_missing_threshold(
    monkeypatch,
):
    data = document(
        {
            "SEMSEARCH_FUSION_STRATEGY": "vector",
            "SEMSEARCH_MIN_VECTOR_SIMILARITY": "0.6",
            "MODEL_RERANK_ENABLED": "true",
        }
    )
    digest = hashlib.sha256(data).hexdigest()
    for field in CONFIG_REFERENCE_FIELDS | set(SEARCH_FIELDS) | {"MODEL_RERANK_ENABLED"}:
        monkeypatch.delenv(field, raising=False)
    values = {
        "SEMSEARCH_CONFIG_BUCKET": "configuration-fixture",
        "SEMSEARCH_CONFIG_KEY": (
            f"111122223333/cn-north-1/gateway-fixture/configuration/search/{digest}.json"
        ),
        "SEMSEARCH_CONFIG_VERSION_ID": "version-fixture",
        "SEMSEARCH_CONFIG_SHA256": digest,
        "SEMSEARCH_CONFIG_BYTES": str(len(data)),
        "SEMSEARCH_FUSION_STRATEGY": "rrf",
        "SEMSEARCH_MIN_VECTOR_SIMILARITY": "-0.5",
        "MODEL_RERANK_ENABLED": "false",
    }
    for field, value in values.items():
        monkeypatch.setenv(field, value)
    calls = []

    class S3:
        def get_object(self, **kwargs):
            calls.append(kwargs)
            return {
                "VersionId": "version-fixture",
                "ContentLength": len(data),
                "Body": io.BytesIO(data),
            }

    monkeypatch.setattr(runtime, "_settings", lambda: SimpleNamespace(gateway=GATEWAY))
    monkeypatch.setattr(runtime, "_aws", lambda _: S3())
    runtime._loaded_configuration.cache_clear()
    try:
        first = config.parse_search_config(runtime._configuration("search"))
        second = config.parse_search_config(runtime._configuration("search"))
    finally:
        runtime._loaded_configuration.cache_clear()
    assert first == second
    assert first.fusion_strategy == "vector"
    assert first.min_vector_similarity == 0.6
    assert first.min_vector_margin is None
    assert first.rerank_enabled is True
    assert runtime.os.environ["MODEL_RERANK_ENABLED"] == "false"
    assert len(calls) == 1


def test_cdk_emits_explicit_defaults_without_optional_thresholds():
    deployment = DeploymentConfig.from_file(Path("infra/config.example.json"))
    deployment = replace(deployment, service_settings={}, model_settings={})
    settings = json.loads(documents(deployment)["search"])["settings"]
    assert settings["SEMSEARCH_FUSION_STRATEGY"] == "vector_bm25_fallback"
    assert settings["MODEL_RERANK_ENABLED"] == "false"
    assert settings["SEMSEARCH_MODELS_ENABLED"] == "false"
    assert "SEMSEARCH_MIN_VECTOR_SIMILARITY" not in settings
    assert "SEMSEARCH_MIN_VECTOR_MARGIN" not in settings


@pytest.mark.parametrize("models_enabled", [True, False])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SEMSEARCH_FUSION_STRATEGY", "bm25"),
        ("SEMSEARCH_MIN_VECTOR_SIMILARITY", "nan"),
        ("SEMSEARCH_MIN_VECTOR_SIMILARITY", "1.1"),
        ("SEMSEARCH_MIN_VECTOR_MARGIN", "-0.1"),
        ("SEMSEARCH_MIN_VECTOR_MARGIN", "2.1"),
        ("SEMSEARCH_TOP_K", "21"),
        ("MODEL_RERANK_ENABLED", "yes"),
    ],
)
def test_cdk_validates_search_settings_even_with_models_disabled(models_enabled, field, value):
    deployment = DeploymentConfig.from_file(Path("infra/config.example.json"))
    category = "model_settings" if field == "MODEL_RERANK_ENABLED" else "service_settings"
    with pytest.raises(ValueError, match=field):
        replace(
            deployment,
            models_enabled=models_enabled,
            **{category: getattr(deployment, category) | {field: value}},
        )


@pytest.mark.parametrize("field", SEARCH_FIELDS + ("MODEL_RERANK_ENABLED",))
@pytest.mark.parametrize("value", ["", 0.5, True])
def test_cdk_configuration_file_errors_name_the_invalid_search_field(tmp_path, field, value):
    raw = json.loads(Path("infra/config.example.json").read_text())
    category = "model_settings" if field == "MODEL_RERANK_ENABLED" else "service_settings"
    raw[category][field] = value
    path = tmp_path / "invalid-config.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match=field):
        DeploymentConfig.from_file(path)


def test_cdk_emits_valid_custom_settings_to_search_only():
    deployment = DeploymentConfig.from_file(Path("infra/config.example.json"))
    search = {
        "SEMSEARCH_SEARCH_TIMEOUT_SECONDS": "2.8",
        "SEMSEARCH_FUSION_STRATEGY": "vector",
        "SEMSEARCH_MIN_VECTOR_SIMILARITY": "-1",
        "SEMSEARCH_MIN_VECTOR_MARGIN": "2",
        "SEMSEARCH_TOP_K": "20",
    }
    deployment = replace(
        deployment,
        service_settings=search,
        model_settings=deployment.model_settings | {"MODEL_RERANK_ENABLED": "true"},
    )
    payloads = {key: json.loads(value) for key, value in documents(deployment).items()}
    assert payloads["search"]["version"] == 1
    assert payloads["search"]["settings"] | search == payloads["search"]["settings"]
    assert payloads["search"]["settings"]["MODEL_RERANK_ENABLED"] == "true"
    parsed = config.parse_search_config(payloads["search"]["settings"])
    assert parsed == SearchConfig(
        top_k=20,
        fusion_strategy="vector",
        min_vector_similarity=-1,
        min_vector_margin=2,
        rerank_enabled=True,
    )
    for operation in ("request", "indexer", "enhancer"):
        assert not set(search).intersection(payloads[operation]["settings"])

"""Query embedding limits agree across service and model configuration boundaries."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from semantic_core.catalog import GatewayIdentity
from semantic_service.config import parse_search_config
from semantic_service.deployment_config import parse_configuration
from semantic_service.models import ModelSettings

FIELD = "MODEL_EMBEDDING_TIMEOUT_SECONDS"
GATEWAY = GatewayIdentity("123456789012", "cn-north-1", "gateway-bounds")


def settings(seconds=None):
    values = {
        "MODEL_ALLOWED_HOSTS": "bounds-model.invalid",
        "MODEL_EMBEDDING_DIMENSIONS": "2",
    }
    if seconds is not None:
        values[FIELD] = seconds
    return values


@pytest.mark.parametrize("seconds", ["0.000001", "0.45", "0.8", "1", "6"])
def test_core_and_model_accept_the_same_inclusive_upper_boundary(seconds):
    values = settings(seconds)
    assert parse_search_config(values).embedding_timeout == float(seconds)
    assert ModelSettings.from_env(values).embedding_timeout_seconds == float(seconds)


def test_omitted_embedding_budget_is_one_second_for_both_consumers():
    assert parse_search_config(settings()).embedding_timeout == 1.0
    assert ModelSettings.from_env(settings()).embedding_timeout_seconds == 1.0


@pytest.mark.parametrize("parser", [parse_search_config, ModelSettings.from_env])
@pytest.mark.parametrize("seconds", ["6.001", "800"])
@pytest.mark.parametrize("models_enabled", ["true", "false"])
def test_core_and_model_reject_oversized_budget_even_when_models_disabled(
    parser, seconds, models_enabled
):
    values = settings(seconds) | {"SEMSEARCH_MODELS_ENABLED": models_enabled}
    with pytest.raises(ValueError, match=FIELD):
        parser(values)


@pytest.mark.parametrize("parser", [parse_search_config, ModelSettings.from_env])
@pytest.mark.parametrize("invalid", [None, True, 0.8])
def test_core_and_model_reject_the_same_non_string_environment_values(parser, invalid):
    with pytest.raises(ValueError, match=FIELD):
        parser(settings() | {FIELD: invalid})


@pytest.mark.parametrize("seconds", [6.001, 800])
def test_direct_model_settings_construction_cannot_bypass_query_limit(seconds):
    model = ModelSettings.from_env(settings())
    with pytest.raises(ValueError, match=FIELD):
        replace(model, embedding_timeout_seconds=seconds)


@pytest.mark.parametrize("seconds", [0.8, 1, 6])
def test_direct_model_settings_accepts_query_limit_and_preserves_document_timeout(seconds):
    model = replace(ModelSettings.from_env(settings()), embedding_timeout_seconds=seconds)
    assert model.embedding_timeout_seconds == seconds
    assert model.document_timeout_seconds == 30
    assert model.chat_timeout_seconds == 30


@pytest.mark.parametrize("seconds", ["6.001", "800"])
@pytest.mark.parametrize("models_enabled", ["true", "false"])
def test_pinned_s3_settings_are_rejected_by_both_effective_configuration_consumers(
    seconds, models_enabled
):
    payload = json.dumps(
        {
            "version": 1,
            "gateway": GATEWAY.as_dict(),
            "operation": "search",
            "settings": {
                FIELD: seconds,
                "SEMSEARCH_MODELS_ENABLED": models_enabled,
            },
        }
    ).encode()
    # The document parser owns structure. Semantic consumers validate the
    # effective settings after the pinned document overrides environment values.
    pinned = parse_configuration(payload, GATEWAY, "search")
    effective = settings("0.45") | dict(pinned)
    for parser in (parse_search_config, ModelSettings.from_env):
        with pytest.raises(ValueError, match=FIELD):
            parser(effective)


@pytest.mark.parametrize("seconds", ["6.001", "800"])
@pytest.mark.parametrize("models_enabled", [True, False])
def test_deployment_config_rejects_oversized_embedding_with_models_on_or_off(
    seconds, models_enabled
):
    from infra.config import DeploymentConfig

    deployment = DeploymentConfig.from_file(Path("infra/config.example.json"))
    with pytest.raises(ValueError, match=FIELD):
        replace(
            deployment,
            models_enabled=models_enabled,
            model_settings=deployment.model_settings | {FIELD: seconds},
        )

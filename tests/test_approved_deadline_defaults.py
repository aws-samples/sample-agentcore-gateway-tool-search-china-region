"""The approved defaults reach direct, environment and pinned-config entry points."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from infra.config import DeploymentConfig
from infra.stack import documents
from service.test_identity import context, event
from test_embedding_budget import budget_service, model_values, read_timeout

from semantic_core._http import HTTPConfig
from semantic_core.bailian import BailianConfig
from semantic_core.catalog import GatewayIdentity
from semantic_core.openai_compatible import OpenAICompatibleConfig
from semantic_core.search import SearchConfig
from semantic_service.config import (
    parse_embedding_timeout_seconds,
    parse_search_config,
    parse_search_request_seconds,
)
from semantic_service.deployment_config import parse_configuration
from semantic_service.models import ModelSettings


@pytest.mark.parametrize("factory", [HTTPConfig, OpenAICompatibleConfig, BailianConfig])
def test_default_transport_allows_one_second(factory):
    config = factory(
        base_url="https://budget-model.invalid", allowed_hosts=("budget-model.invalid",)
    )
    assert config.timeout_seconds == 1.0
    assert replace(config, timeout_seconds=0.45).timeout_seconds == 0.45


def test_core_and_service_defaults_agree_without_environment_fields():
    settings = ModelSettings.from_env(model_values(None))
    assert SearchConfig().embedding_timeout == 1.0
    assert parse_search_config({}).embedding_timeout == 1.0
    assert parse_embedding_timeout_seconds({}) == 1.0
    assert settings.embedding_timeout_seconds == 1.0
    direct = ModelSettings(settings.allowed_hosts, settings.embedding, settings.chat)
    assert direct.embedding_timeout_seconds == 1.0
    assert parse_search_request_seconds({}) == 3.0


def test_defaults_do_not_change_other_service_stage_budgets():
    settings = ModelSettings.from_env(model_values(None))
    assert SearchConfig().rerank_timeout == 0.45
    assert settings.rerank_timeout_seconds == 0.45
    assert settings.document_timeout_seconds == 30.0
    assert settings.chat_timeout_seconds == 30.0


@pytest.mark.parametrize("region", ["cn-north-1", "cn-northwest-1"])
def test_cdk_generated_pinned_config_uses_new_defaults(region):
    deployment = replace(
        DeploymentConfig.from_file(Path("infra/config.example.json")), region=region
    )
    payload = documents(deployment)["search"]
    parsed = parse_configuration(
        payload,
        GatewayIdentity(deployment.account, region, deployment.gateway_id),
        "search",
    )
    assert json.loads(payload)["version"] == 1
    assert parse_search_config(parsed).embedding_timeout == 1.0
    assert ModelSettings.from_env(parsed).embedding_timeout_seconds == 1.0
    assert parse_search_request_seconds(parsed) == 3.0


def test_cdk_explicit_previous_budgets_still_override_defaults():
    deployment = DeploymentConfig.from_file(Path("infra/config.example.json"))
    deployment = replace(
        deployment,
        model_settings=deployment.model_settings | {"MODEL_EMBEDDING_TIMEOUT_SECONDS": "0.45"},
        service_settings=deployment.service_settings | {"SEMSEARCH_SEARCH_TIMEOUT_SECONDS": "1.5"},
    )
    settings = json.loads(documents(deployment)["search"])["settings"]
    assert parse_search_config(settings).embedding_timeout == 0.45
    assert parse_search_request_seconds(settings) == 1.5


def test_omitted_defaults_allow_cold_preparation_and_a_longer_model_response(monkeypatch):
    with budget_service(
        monkeypatch,
        embedding_seconds=None,
        request_seconds=None,
        pre_seconds=1.2,
        response_seconds=(0.8,),
    ) as service:
        observed = []
        service.handler(event(), context(), diagnostics=observed.append)
        assert service.handler.request_seconds == 3.0
        assert read_timeout(service.requests[0]) == pytest.approx(1.0)
        assert service.handler.config.embedding_timeout == 1.0
        assert observed[0].degraded is False
        assert service.authorizer.calls == 5


@pytest.mark.parametrize("name", ["rerank-off.dev.json", "rerank-on.dev.json"])
def test_deployment_examples_use_the_approved_embedding_and_search_budgets(name):
    settings = json.loads((Path("examples") / name).read_text())
    assert settings["model_settings"]["MODEL_EMBEDDING_TIMEOUT_SECONDS"] == "1.0"
    assert settings["service_settings"]["SEMSEARCH_SEARCH_TIMEOUT_SECONDS"] == "3.0"
    assert settings["model_settings"]["MODEL_RERANK_TIMEOUT_SECONDS"] == "0.45"

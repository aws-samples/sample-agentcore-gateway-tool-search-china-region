"""Service examples freeze the approved defaults and a one-variable rerank trial."""

import json
from pathlib import Path

import pytest
from infra.config import DeploymentConfig

from semantic_core.search import SearchConfig
from semantic_service.models import ModelSettings

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    "relative", ["README.md", "docs/service.md", "docs/design.md", "docs/deploy.md"]
)
def test_current_docs_publish_service_defaults_and_optional_threshold_names(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    if relative == "README.md":
        assert "[服务配置](docs/service.md#配置与入口)" in text
        text = (ROOT / "docs/service.md").read_text(encoding="utf-8")
    assert "`SEMSEARCH_FUSION_STRATEGY=vector_bm25_fallback`" in text
    assert "SEMSEARCH_MIN_VECTOR_SIMILARITY" in text
    assert "SEMSEARCH_MIN_VECTOR_MARGIN" in text
    assert "`MODEL_RERANK_ENABLED=false`" in text
    assert "建议的后续服务配置" not in text
    assert "默认值切换等待用户确认" not in text


def test_trial_examples_change_only_rerank_enablement_and_preserve_gateway_contract():
    baseline = json.loads((ROOT / "infra/config.example.json").read_text(encoding="utf-8"))
    off = json.loads((ROOT / "examples/rerank-off.dev.json").read_text(encoding="utf-8"))
    on = json.loads((ROOT / "examples/rerank-on.dev.json").read_text(encoding="utf-8"))
    assert off["model_settings"]["MODEL_RERANK_ENABLED"] == "false"
    assert on["model_settings"]["MODEL_RERANK_ENABLED"] == "true"
    on["model_settings"]["MODEL_RERANK_ENABLED"] = "false"
    assert off == on
    assert off["search_descriptor"] == baseline["search_descriptor"]
    assert off["registry"] == baseline["registry"]
    assert off["models_enabled"] is True
    assert off["schedules_enabled"] is False
    assert off["stage"] == "dev"
    assert off["service_settings"]["SEMSEARCH_FUSION_STRATEGY"] == "vector_bm25_fallback"
    assert "SEMSEARCH_MIN_VECTOR_SIMILARITY" not in off["service_settings"]
    assert "SEMSEARCH_MIN_VECTOR_MARGIN" not in off["service_settings"]


@pytest.mark.parametrize("enabled", [False, True])
def test_trial_examples_parse_offline_with_current_proxy_route_and_stage_budgets(enabled):
    arm = "on" if enabled else "off"
    config = DeploymentConfig.from_file(ROOT / f"examples/rerank-{arm}.dev.json")
    model = ModelSettings.from_env(config.model_settings)
    assert model.base_url == "https://model.example.invalid/v1"
    assert model.allowed_hosts == ("model.example.invalid",)
    assert model.embedding.path == "/embeddings"
    assert model.embedding_timeout_seconds == 1.0
    assert model.rerank_timeout_seconds == 0.45
    assert config.model_settings["MODEL_RERANK_MODEL"] == "qwen3-rerank"
    assert config.model_settings["MODEL_RERANK_PATH"] == "/rerank"
    assert config.model_settings["MODEL_RERANK_ENVELOPE"] == "flat"
    assert config.model_settings["MODEL_RERANK_SCORE_FIELD"] == "relevance_score"
    assert (model.rerank is not None) is enabled
    assert SearchConfig().bm25_fallback_similarity == 0.5
    assert SearchConfig().fusion_strategy == "rrf"

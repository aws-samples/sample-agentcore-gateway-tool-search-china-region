"""Keep operator documentation aligned with the opt-in retrieval contract."""

from pathlib import Path

import pytest

from semantic_core.search import SearchConfig

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("relative", ["README.md", "docs/design.md"])
def test_operator_docs_distinguish_defaults_from_optional_cosine_gates(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    if relative == "README.md":
        assert "[详细设计](docs/design.md)" in text
        text = (ROOT / "docs/design.md").read_text(encoding="utf-8")
    defaults = SearchConfig()
    for name in (
        "fusion_strategy",
        "min_vector_similarity",
        "min_vector_margin",
        "rerank_enabled",
    ):
        value = getattr(defaults, name)
        rendered = f'"{value}"' if isinstance(value, str) else str(value)
        assert f"`{name}={rendered}`" in text, name
    assert 'fusion_strategy="vector"' in text
    assert 'fusion_strategy="vector_bm25_fallback"' in text
    assert "min_vector_similarity" in text and "归一化余弦" in text
    assert "min_vector_margin" in text and "中位数" in text
    assert "跨请求 rerank 绝对分数阈值" in text


@pytest.mark.parametrize("relative", ["README.md", "docs/design.md"])
def test_operator_docs_state_that_embedding_outage_bypasses_all_confidence_gates(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    if relative == "README.md":
        assert "[详细设计](docs/design.md)" in text
        text = (ROOT / "docs/design.md").read_text(encoding="utf-8")
    assert "embedding 不可用时退化为授权 BM25，余弦与分差拒答门槛均失效" in text
    assert "单独告警" in text
    assert "rerank 失败保留实际基础排序" in text


@pytest.mark.parametrize("relative", ["README.md", "docs/design.md"])
def test_operator_docs_describe_trusted_mode_and_abstention_diagnostics(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    if relative == "README.md":
        assert "[详细设计](docs/design.md)" in text
        text = (ROOT / "docs/design.md").read_text(encoding="utf-8")
    assert "`observer(mode, abstained)`" in text
    assert "`ranking_mode` 必须与 `mode` 一致" in text
    assert "`abstained=True`" in text and "门槛拒答" in text
    assert "公共成功对象" in text


@pytest.mark.parametrize("relative", ["README.md", "reports/tuning/README.md"])
def test_operator_docs_explain_tuning_expired_credential_exit(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    if relative == "README.md":
        assert "[开发者指南](docs/developer-guide.md)" in text
        text = (ROOT / "docs/developer-guide.md").read_text(encoding="utf-8")
    assert "run_tuning.py" in text
    assert "`20`" in text
    for marker in ("ExpiredToken", "RequestExpired", "TokenRefreshRequired"):
        assert marker in text
    assert "CredentialsExpired" in text


def test_tuning_docs_label_reconstructed_latency_and_independent_knee_calls():
    text = (ROOT / "reports/tuning/README.md").read_text(encoding="utf-8")
    assert "reconstructed" in text
    assert "one measured query-embedding duration" in text
    assert "not an end-to-end latency measurement" in text
    assert "fresh query embedding for each search arm" in text


def test_tuning_docs_require_external_freeze_capture_only_for_future_runs():
    text = (ROOT / "reports/tuning/README.md").read_text(encoding="utf-8")
    for artifact in ("recommendation.json", "split-manifest.json", "freeze-boundary.json"):
        assert artifact in text
    assert "--freeze-evidence-ref" in text
    assert "not authenticated" in text
    assert "must not be retrofitted" in text
    assert "dev_finished_at" in text and "UTC" in text
    assert "0.05" in text and "vector_ungated" in text
    assert "future" in text

"""值班文档须覆盖 Indexer 实际输出的计数与对应指标。"""

from pathlib import Path

from semantic_service._indexer_telemetry import collect

ROOT = Path(__file__).parents[1]


def test_deploy_docs_list_every_indexer_counter_and_its_cloudwatch_metric() -> None:
    text = (ROOT / "docs/deploy.md").read_text(encoding="utf-8")
    with collect() as counters:
        for field in counters:
            metric = f"indexer-{field.replace('_', '-')}"
            assert f"| `{field}` | `{metric}` |" in text, field


def test_deploy_docs_distinguish_replay_outcomes_and_operator_actions() -> None:
    text = (ROOT / "docs/deploy.md").read_text(encoding="utf-8")
    for guidance in (
        "仅统计实际启动的第二次 Gateway 请求",
        "换令牌失败",
        "预算不足",
        "IdP",
        "不放宽预算",
        "下次部署",
        "不新增默认告警",
    ):
        assert guidance in text, guidance

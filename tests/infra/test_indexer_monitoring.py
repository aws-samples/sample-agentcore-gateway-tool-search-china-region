"""Indexer 汇总中的鉴权计数直接写入既有 CloudWatch 指标命名空间。"""

import pytest
from test_components import components  # noqa: F401


@pytest.mark.parametrize(
    "field",
    (
        "gateway_401_count",
        "gateway_403_count",
        "token_proactive_refresh_count",
        "gateway_replay_count",
        "gateway_replay_success_count",
        "gateway_replay_failure_count",
        "gateway_replay_skipped_count",
        "gateway_auth_refresh_failure_count",
    ),
)
def test_indexer_auth_counters_use_summary_values(components, field):  # noqa: F811
    indexer = next(
        resource["Properties"]
        for resource in components.values()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource["Properties"]["FunctionName"] == "semsearch-components-indexer"
    )
    metric_name = f"indexer-{field.replace('_', '-')}"
    filters = [
        resource["Properties"]
        for resource in components.values()
        if resource["Type"] == "AWS::Logs::MetricFilter"
        and resource["Properties"]["MetricTransformations"][0]["MetricName"] == metric_name
    ]
    assert len(filters) == 1
    # 字段存在即可匹配，因此零值也被记录；精确 transformation 排除默认值和维度。
    assert filters[0] == {
        "FilterName": f"semsearch-components-{metric_name}",
        "FilterPattern": f'{{ $.operation = "indexer" && $.{field} = * }}',
        "LogGroupName": indexer["LoggingConfig"]["LogGroup"],
        "MetricTransformations": [
            {
                "MetricName": metric_name,
                "MetricNamespace": "semsearch/semsearch-components",
                "MetricValue": f"$.{field}",
            }
        ],
    }

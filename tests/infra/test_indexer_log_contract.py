"""合成过滤器必须引用正式 Indexer 日志实际输出的字段。"""

import json
import re
from types import SimpleNamespace

from test_components import components  # noqa: F401

from semantic_service.runtime import indexer_handler


def test_indexer_filters_reference_real_summary_fields(components, capsys):  # noqa: F811
    assert indexer_handler({"time": "2000-01-01T00:00:00Z"}, SimpleNamespace()) == {
        "status": "expired"
    }
    row = json.loads(capsys.readouterr().out)
    indexer = next(
        resource["Properties"]
        for resource in components.values()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource["Properties"]["Handler"] == "semantic_service.runtime.indexer_handler"
    )
    extracted = []
    for resource in components.values():
        if resource["Type"] != "AWS::Logs::MetricFilter":
            continue
        properties = resource["Properties"]
        if properties["LogGroupName"] != indexer["LoggingConfig"]["LogGroup"]:
            continue
        transformation = properties["MetricTransformations"][0]
        value = transformation["MetricValue"]
        if not value.startswith("$."):
            continue
        match = re.fullmatch(r"\$\.([a-zA-Z_]\w*)", value)
        assert match is not None, value
        field = match[1]
        assert field in row, f"指标 {transformation['MetricName']} 引用了日志缺失字段 {field}"
        assert type(row[field]) is int and row[field] >= 0
        selectors = re.findall(r"\$\.([a-zA-Z_]\w*)", properties["FilterPattern"])
        assert field in selectors
        assert set(selectors) <= row.keys(), "过滤器模式引用了正式日志中不存在的字段"
        extracted.append(field)
    assert set(extracted) == {field for field in row if field.endswith("_count")}
    assert len(extracted) == len(set(extracted))

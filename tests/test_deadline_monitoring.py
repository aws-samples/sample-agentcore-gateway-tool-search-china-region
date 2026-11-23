"""The deadline fallback metric counts the real request-local log signal."""

import json
import re

import aws_cdk as cdk
import pytest
from aws_cdk import aws_logs as logs
from aws_cdk.assertions import Template
from infra.monitoring import add_monitoring
from service.test_identity import BUSINESS, context, event
from test_service_diagnostics import handler

from semantic_service import runtime
from semantic_service.auth import InputError, ServiceError

METRIC = "search-lambda-deadline-ignored"


@pytest.fixture(scope="module", params=("cn-north-1", "cn-northwest-1"))
def monitoring(request, tmp_path_factory):
    directory = tmp_path_factory.mktemp(f"deadline-monitoring-{request.param}")
    app = cdk.App(outdir=str(directory / "cdk.out"))
    stack = cdk.Stack(
        app, "Monitoring", env=cdk.Environment(account="111122223333", region=request.param)
    )
    operations = ("request", "search", "indexer", "enhancer")
    groups = {operation: logs.LogGroup(stack, operation) for operation in operations}
    add_monitoring(
        stack,
        prefix="monitoring-test",
        log_groups=groups,
        function_names={operation: f"test-{operation}" for operation in operations},
    )
    return Template.from_stack(stack), stack.resolve(groups["search"].log_group_name)


def boolean_filter_matches(pattern, row):
    """Interpret only the asserted fixed JSON boolean filter, not arbitrary syntax."""
    parsed = re.fullmatch(r'\{ \$\.operation = "([a-z]+)" && \$\.([a-z_]+) IS TRUE \}', pattern)
    assert parsed is not None, pattern
    operation, field = parsed.groups()
    return row.get("operation") == operation and row.get(field) is True


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("missing", False),
        ("valid", False),
        ("invalid", True),
        ("method_error", True),
        ("later_rejection", True),
        ("factory_error", False),
    ],
)
def test_deadline_metric_is_bound_to_actual_search_logs(
    monitoring, monkeypatch, capsys, scenario, expected
):
    search = handler()
    monkeypatch.setattr(runtime, "_search", lambda: search)
    ctx = context()
    arguments = event()
    expected_error = None

    def broken():
        raise RuntimeError("private-deadline-marker")

    if scenario == "valid":
        ctx.get_remaining_time_in_millis = lambda: 10000
    elif scenario in {"invalid", "later_rejection"}:
        ctx.get_remaining_time_in_millis = lambda: float("nan")
    elif scenario == "method_error":
        ctx.get_remaining_time_in_millis = broken
    elif scenario == "factory_error":
        monkeypatch.setattr(runtime, "_search", broken)
        expected_error = ServiceError
    if scenario == "later_rejection":
        arguments = {"query": ""}
        expected_error = InputError
    if expected_error is None:
        assert runtime.search_handler(arguments, ctx) == {"tools": [BUSINESS.descriptor]}
    else:
        with pytest.raises(expected_error):
            runtime.search_handler(arguments, ctx)
    captured = capsys.readouterr()
    assert "private-deadline-marker" not in captured.out + captured.err
    rows = [json.loads(line) for line in captured.out.splitlines() if line.startswith("{")]
    rows = [row for row in rows if row.get("operation") == "search"]
    assert len(rows) == 1
    row = rows[0]
    assert row["lambda_deadline_ignored"] is expected

    template, search_group = monitoring
    filters = [
        resource["Properties"]
        for resource in template.find_resources("AWS::Logs::MetricFilter").values()
        if resource["Properties"]["FilterName"] == f"monitoring-test-{METRIC}"
    ]
    assert len(filters) == 1
    metric_filter = filters[0]
    assert metric_filter["LogGroupName"] == search_group
    assert metric_filter["FilterPattern"] == (
        '{ $.operation = "search" && $.lambda_deadline_ignored IS TRUE }'
    )
    assert metric_filter["MetricTransformations"] == [
        {
            "MetricName": METRIC,
            "MetricNamespace": "semsearch/monitoring-test",
            "MetricValue": "1",
        }
    ]
    pattern = metric_filter["FilterPattern"]
    assert boolean_filter_matches(pattern, row) is expected
    assert not boolean_filter_matches(pattern, row | {"operation": "request"})
    assert not boolean_filter_matches(pattern, row | {"lambda_deadline_ignored": "true"})
    assert not boolean_filter_matches(pattern, row | {"lambda_deadline_ignored": 1})
    assert not boolean_filter_matches(
        pattern, {key: value for key, value in row.items() if key != "lambda_deadline_ignored"}
    )
    assert not any(
        resource["Properties"].get("MetricName") == METRIC
        for resource in template.find_resources("AWS::CloudWatch::Alarm").values()
    )

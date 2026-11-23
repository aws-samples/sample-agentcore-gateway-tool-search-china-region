"""Bind PC Init metrics and alarms to records emitted by the real REQUEST entry point."""

import httpx
import pytest
from test_oidc_monitoring import (
    monitoring,  # noqa: F401
    request_filter_matches,
)
from test_oidc_reliability import (
    fixture,  # noqa: F401
    signing_key,  # noqa: F401
)
from test_request_initialization import load_entrypoint  # noqa: F401
from test_request_prefetch import install_handler, records


@pytest.mark.parametrize(
    ("metric_name", "pattern", "threshold"),
    [
        (
            "request-init-auth-recovered",
            '{ $.operation = "request_init" && $.oidc_prefetch.auth_recovered IS TRUE }',
            2,
        ),
        (
            "request-init-oidc-prefetch-failed",
            '{ $.operation = "request_init" && $.oidc_prefetch.status = "failed" }',
            0,
        ),
    ],
)
def test_init_metrics_use_request_logs_and_the_existing_alarm_policy(
    monitoring,  # noqa: F811
    metric_name,
    pattern,
    threshold,
):
    template, request_group, action = monitoring
    filters = [
        resource["Properties"]
        for resource in template.find_resources("AWS::Logs::MetricFilter").values()
        if resource["Properties"]["MetricTransformations"][0]["MetricName"] == metric_name
    ]
    assert len(filters) == 1
    assert filters[0] == {
        "FilterName": f"monitoring-test-{metric_name}",
        "FilterPattern": pattern,
        "LogGroupName": request_group,
        "MetricTransformations": [
            {
                "MetricName": metric_name,
                "MetricNamespace": "semsearch/monitoring-test",
                "MetricValue": "1",
            }
        ],
    }
    alarms = [
        resource["Properties"]
        for resource in template.find_resources("AWS::CloudWatch::Alarm").values()
        if resource["Properties"].get("MetricName") == metric_name
    ]
    assert len(alarms) == 1
    alarm = alarms[0]
    assert alarm["AlarmName"] == f"monitoring-test-{metric_name}"
    assert alarm["Namespace"] == "semsearch/monitoring-test"
    assert alarm["Statistic"] == "Sum"
    assert alarm["Period"] == 300
    assert alarm["Threshold"] == threshold
    assert alarm["ComparisonOperator"] == "GreaterThanThreshold"
    assert alarm["EvaluationPeriods"] == alarm["DatapointsToAlarm"] == 1
    assert alarm["TreatMissingData"] == "notBreaching"
    assert alarm["AlarmActions"] == [action]
    assert not alarm.get("Dimensions")


@pytest.mark.parametrize("endpoint", ["discovery", "jwks"])
@pytest.mark.parametrize(
    "scenario", ["clean", "recovered", "terminal", "structural", "partial_recovery", "partial_jwks"]
)
def test_real_pc_init_records_reach_metrics_without_counting_attempts_or_warm_requests(
    monitoring,  # noqa: F811
    monkeypatch,
    capfd,
    load_entrypoint,  # noqa: F811
    fixture,  # noqa: F811
    endpoint,
    scenario,
):
    install_handler(monkeypatch, fixture)
    if scenario == "recovered":
        fixture.steps[endpoint] = [httpx.Response(503)]
    elif scenario == "terminal":
        fixture.steps[endpoint] = [httpx.Response(503), httpx.Response(503)]
    elif scenario == "partial_recovery":
        fixture.steps["discovery"] = [httpx.Response(503)]
        fixture.steps["jwks"] = [httpx.Response(503)]
    elif scenario == "structural":
        fixture.steps["discovery"] = [AssertionError("synthetic private implementation failure")]
    elif scenario == "partial_jwks":
        fixture.keys.append({"kid": "private-active-key", "use": "enc"})
    entry = load_entrypoint("provisioned-concurrency")
    init_rows = records(capfd)
    init = init_rows[-1]
    assert init["operation"] == "request_init"
    assert init["status"] == "ready"
    failed = scenario in {"terminal", "structural", "partial_recovery", "partial_jwks"}
    assert init["oidc_prefetch"]["status"] == ("failed" if failed else "ready")
    if scenario == "partial_jwks":
        assert init["oidc_prefetch"]["jwks_validation"]["rejected"] == 1
        assert fixture.verifier._keys == {}
        fixture.keys.pop()
    assert entry.request_handler({}, object()) == {"accepted": True}
    invocation_rows = records(capfd)
    first = invocation_rows[-1]
    assert first["first_invocation"] is True
    assert "auth_recovered" not in first
    assert "synthetic private" not in str(init_rows)
    expected_counts = {
        "request-count": 1,
        "request-internal-errors": 0,
        "rate-limited": 0,
        "authentication-failed": 0,
        "request-auth-recovered": 0,
        "request-init-auth-recovered": int(scenario == "recovered"),
        "request-init-oidc-prefetch-failed": int(failed),
    }
    template, request_group, _ = monitoring
    filters = [
        resource["Properties"]
        for resource in template.find_resources("AWS::Logs::MetricFilter").values()
        if resource["Properties"]["LogGroupName"] == request_group
    ]
    assert len(filters) == len(expected_counts)
    all_rows = init_rows + invocation_rows
    events = [row for row in all_rows if row["operation"] == "oidc_fetch"]
    for metric_filter in filters:
        pattern = metric_filter["FilterPattern"]
        metric = metric_filter["MetricTransformations"][0]
        assert not any(request_filter_matches(pattern, row) for row in events)
        assert (
            sum(request_filter_matches(pattern, row) for row in all_rows)
            == expected_counts[metric["MetricName"]]
        )
        if metric["MetricName"].startswith("request-init-"):
            assert not any(request_filter_matches(pattern, row) for row in invocation_rows)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row.update(operation="request"),
        lambda row: row.update(operation="oidc_fetch"),
        lambda row: row.update(oidc_prefetch={}),
        lambda row: row.update(oidc_prefetch={"status": "ready"}, auth_recovered=True),
    ],
)
def test_recovery_filter_requires_the_actual_init_operation_and_nested_signal(
    monitoring,  # noqa: F811
    monkeypatch,
    capfd,
    load_entrypoint,  # noqa: F811
    fixture,  # noqa: F811
    mutate,
):
    install_handler(monkeypatch, fixture)
    fixture.steps["jwks"] = [httpx.Response(503)]
    load_entrypoint("provisioned-concurrency")
    init = records(capfd)[-1]
    template, _, _ = monitoring
    filters = [
        resource["Properties"]
        for resource in template.find_resources("AWS::Logs::MetricFilter").values()
        if resource["Properties"]["FilterName"] == "monitoring-test-request-init-auth-recovered"
    ]
    assert len(filters) == 1
    pattern = filters[0]["FilterPattern"]
    assert request_filter_matches(pattern, init)
    mutate(init)
    assert not request_filter_matches(pattern, init)

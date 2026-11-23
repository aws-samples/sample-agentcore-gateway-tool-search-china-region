"""真实补偿成功才能记恢复，之后调用方被拒也不能丢掉该信号。"""

import builtins
import json
from pathlib import Path
from types import SimpleNamespace

import aws_cdk as cdk
import httpx
import jwt
import pytest
from aws_cdk import aws_logs as logs
from aws_cdk.assertions import Template
from infra.monitoring import add_monitoring
from test_oidc_attempt_events import EVENT_FIELDS, split_request_rows
from test_oidc_reliability import (
    NOW,
    signing_key,  # noqa: F401
)
from test_oidc_reliability import fixture as oidc_fixture  # noqa: F401

from semantic_core._timing import stage
from semantic_service import auth, runtime
from semantic_service.auth import AuthenticationError, RateLimitError, ServiceError

REQUEST_ID = "01234567-89ab-cdef-0123-456789abcdef"


def sensitive_values(fixture, token):
    claims = jwt.decode(token, options={"verify_signature": False})
    return {
        "issuer": fixture.config.issuer,
        "kid": jwt.get_unverified_header(token)["kid"],
        "subject": claims["sub"],
        "token": token,
        "public_key_material": fixture.keys[0]["n"],
    }


def observe(
    monkeypatch,
    capsys,
    fixture,
    *,
    token=None,
    after=None,
    expected_error=None,
    captured_rows=None,
):
    token = token or fixture.token()

    def handler(event, context):
        fixture.verifier.verify(token)
        if after is not None:
            raise after
        return {"tools": []}

    monkeypatch.setattr(runtime, "_request", lambda: handler)
    context = SimpleNamespace(aws_request_id=REQUEST_ID)
    if expected_error is None:
        assert runtime.request_handler({}, context) == {"tools": []}
    else:
        with pytest.raises(expected_error) as caught:
            runtime.request_handler({}, context)
        assert type(caught.value) is expected_error
    captured = capsys.readouterr()
    output = captured.out
    for field, value in sensitive_values(fixture, token).items():
        assert value not in output + captured.err, f"sensitive field leaked: {field}"
    rows = [json.loads(line) for line in output.splitlines()]
    _, summary = split_request_rows(rows, REQUEST_ID)
    if captured_rows is not None:
        captured_rows.extend(rows)
    assert summary["aws_request_id"] == REQUEST_ID
    assert summary["operation"] == "request"
    return summary


@pytest.fixture(scope="module", params=("cn-north-1", "cn-northwest-1"))
def monitoring(request, tmp_path_factory):
    directory = tmp_path_factory.mktemp(f"monitoring-{request.param}")
    app = cdk.App(outdir=str(directory / "cdk.out"))
    stack = cdk.Stack(
        app, "Monitoring", env=cdk.Environment(account="111122223333", region=request.param)
    )
    operations = ("request", "search", "indexer", "enhancer")
    groups = {operation: logs.LogGroup(stack, operation) for operation in operations}
    action = f"arn:aws-cn:sns:{request.param}:111122223333:operator-alerts"
    add_monitoring(
        stack,
        prefix="monitoring-test",
        log_groups=groups,
        function_names={operation: f"test-{operation}" for operation in operations},
        alarm_actions=(action,),
    )
    return Template.from_stack(stack), stack.resolve(groups["request"].log_group_name), action


@pytest.mark.parametrize("endpoint", ("discovery", "jwks"))
@pytest.mark.parametrize(
    ("outcome", "error", "status", "recovered", "failure", "count", "errors"),
    [
        ("recovered", None, "ok", True, False, 2, 1),
        ("expired", AuthenticationError, "unauthenticated", True, False, 2, 1),
        ("limited", RateLimitError, "rate limited", True, False, 2, 1),
        ("downstream", ServiceError, "internal failure", True, False, 2, 1),
        ("interrupted", KeyboardInterrupt, "internal failure", False, False, 2, 1),
        ("terminal", ServiceError, "internal failure", False, True, 2, 2),
        ("clean", None, "ok", False, False, 1, 0),
        ("cached", None, "ok", False, False, None, None),
    ],
)
def test_metadata_recovery_survives_later_rejection_and_reaches_alarm(
    monitoring,
    oidc_fixture,  # noqa: F811
    monkeypatch,
    capsys,
    endpoint,
    outcome,
    error,
    status,
    recovered,
    failure,
    count,
    errors,
):
    if outcome in {"recovered", "expired", "limited", "downstream", "interrupted"}:
        oidc_fixture.steps[endpoint] = [httpx.Response(503)]
    elif outcome == "terminal":
        oidc_fixture.steps[endpoint] = [httpx.Response(503), httpx.Response(503)]
    elif outcome == "cached":
        oidc_fixture.verifier.verify(oidc_fixture.token())
    token = oidc_fixture.token(exp=NOW - 31) if outcome == "expired" else oidc_fixture.token()
    after = {
        "limited": RateLimitError(1),
        "downstream": RuntimeError("synthetic downstream failure"),
        "interrupted": KeyboardInterrupt(),
    }.get(outcome)
    row = observe(monkeypatch, capsys, oidc_fixture, token=token, after=after, expected_error=error)
    assert row["status"] == status
    assert ("auth_failure" in row) is failure
    if recovered:
        assert row.get("auth_recovered") is True
    else:
        assert "auth_recovered" not in row
    stage_key = f"auth.{endpoint}"
    if count is None:
        assert stage_key not in row["stage_timings"]
    else:
        assert row["stage_timings"][stage_key]["count"] == count
        assert row["stage_timings"][stage_key]["errors"] == errors

    template, request_group, action = monitoring
    filters = [
        item["Properties"]
        for item in template.find_resources("AWS::Logs::MetricFilter").values()
        if item["Properties"]["FilterPattern"]
        == '{ $.operation = "request" && $.auth_recovered IS TRUE }'
    ]
    assert len(filters) == 1
    metric_filter = filters[0]
    assert metric_filter["FilterName"] == "monitoring-test-request-auth-recovered"
    assert metric_filter["LogGroupName"] == request_group
    metric = metric_filter["MetricTransformations"]
    assert metric == [
        {
            "MetricName": "request-auth-recovered",
            "MetricNamespace": "semsearch/monitoring-test",
            "MetricValue": "1",
        }
    ]
    alarms = [
        item["Properties"]
        for item in template.find_resources("AWS::CloudWatch::Alarm").values()
        if item["Properties"].get("MetricName") == metric[0]["MetricName"]
        and item["Properties"].get("Namespace") == metric[0]["MetricNamespace"]
    ]
    assert len(alarms) == 1
    alarm = alarms[0]
    assert alarm["AlarmName"] == "monitoring-test-request-auth-recovered"
    assert alarm["Statistic"] == "Sum"
    assert alarm["Period"] == 300
    assert alarm["Threshold"] == 2
    assert alarm["ComparisonOperator"] == "GreaterThanThreshold"
    assert alarm["EvaluationPeriods"] == alarm["DatapointsToAlarm"] == 1
    assert alarm["TreatMissingData"] == "notBreaching"
    assert alarm["AlarmActions"] == [action]
    assert not alarm.get("Dimensions")
    if recovered or outcome == "interrupted":
        warm_row = observe(monkeypatch, capsys, oidc_fixture)
        assert warm_row["status"] == "ok"
        assert "auth_recovered" not in warm_row
        assert "auth_failure" not in warm_row
        assert stage_key not in warm_row["stage_timings"]


def test_terminal_jwks_failure_supersedes_prior_discovery_recovery(
    oidc_fixture,  # noqa: F811
    monkeypatch,
    capsys,
):
    oidc_fixture.steps["discovery"] = [httpx.Response(503)]
    oidc_fixture.steps["jwks"] = [httpx.Response(503)]
    row = observe(monkeypatch, capsys, oidc_fixture, expected_error=ServiceError)
    assert row["stage_timings"]["auth.discovery"]["errors"] == 1
    assert row["stage_timings"]["auth.discovery"]["count"] == 2
    assert row["auth_failure"]["endpoint"] == "jwks"
    assert "auth_recovered" not in row


@pytest.mark.parametrize("endpoint", ("discovery", "jwks"))
def test_recovered_http_with_invalid_document_is_terminal(
    oidc_fixture,  # noqa: F811
    monkeypatch,
    capsys,
    endpoint,
):
    oidc_fixture.steps[endpoint] = [httpx.Response(503), httpx.Response(200, json={})]
    row = observe(monkeypatch, capsys, oidc_fixture, expected_error=ServiceError)
    assert row["auth_failure"]["category"] == "document"
    assert "auth_recovered" not in row


def test_success_before_failure_is_not_a_recovery(oidc_fixture, monkeypatch, capsys):  # noqa: F811
    oidc_fixture.steps["jwks"] = [
        httpx.Response(200, json=oidc_fixture.document("jwks")),
        httpx.Response(503),
    ]

    def interrupt_retry(delay):
        raise RuntimeError("synthetic failure before compensation GET")

    monkeypatch.setattr(auth.time, "sleep", interrupt_retry)
    row = observe(
        monkeypatch,
        capsys,
        oidc_fixture,  # noqa: F811
        token=oidc_fixture.token("missing"),
        expected_error=ServiceError,
    )
    assert row["stage_timings"]["auth.jwks"]["count"] == 2
    assert row["stage_timings"]["auth.jwks"]["errors"] == 1
    assert "auth_failure" not in row
    assert "auth_recovered" not in row
    assert all(timeouts["read"] == 2.0 for _, timeouts in oidc_fixture.calls)


def test_unknown_kid_rejection_retains_completed_compensation(
    oidc_fixture,  # noqa: F811
    monkeypatch,
    capsys,
):
    oidc_fixture.steps["jwks"] = [
        httpx.Response(200, json=oidc_fixture.document("jwks")),
        httpx.Response(503),
    ]
    row = observe(
        monkeypatch,
        capsys,
        oidc_fixture,  # noqa: F811
        token=oidc_fixture.token("missing"),
        expected_error=AuthenticationError,
    )
    assert row["status"] == "unauthenticated"
    assert row["auth_recovered"] is True
    assert "auth_failure" not in row
    assert [endpoint for endpoint, _ in oidc_fixture.calls] == ["discovery", "jwks", "jwks", "jwks"]


@pytest.mark.parametrize(
    "failed_stage",
    (
        "auth.discovery",
        "auth.jwks",
        "auth.oidc",
        "auth.authorize",
        "auth.rate_limit",
        "auth.envelope",
        "handler.work",
    ),
)
def test_stage_counts_alone_are_not_evidence_of_compensation(capsys, failed_stage):
    def action():
        try:
            with stage(failed_stage):
                raise RuntimeError("synthetic failure")
        except RuntimeError:
            pass
        with stage(failed_stage):
            pass
        return {"tools": []}

    assert runtime._observed("request", action) == {"tools": []}
    row = json.loads(capsys.readouterr().out)
    assert row["stage_timings"][failed_stage]["count"] == 2
    assert row["stage_timings"][failed_stage]["errors"] == 1
    assert "auth_recovered" not in row


@pytest.mark.parametrize("error", (KeyboardInterrupt, SystemExit))
def test_failed_metadata_then_base_exception_is_not_recovery(capsys, error):
    def action():
        try:
            with stage("auth.jwks"):
                raise RuntimeError("synthetic failure")
        except RuntimeError:
            raise error() from None

    with pytest.raises(error):
        runtime._observed("request", action)
    row = json.loads(capsys.readouterr().out)
    assert row["status"] == "internal failure"
    assert "auth_recovered" not in row


@pytest.mark.parametrize("operation", ("search", "indexer", "enhancer"))
def test_other_operations_do_not_collect_or_leak_request_recovery(
    oidc_fixture,  # noqa: F811
    monkeypatch,
    capsys,
    operation,
):
    oidc_fixture.steps["jwks"] = [httpx.Response(503)]
    runtime._observed(operation, lambda: oidc_fixture.verifier.verify(oidc_fixture.token()))
    row = json.loads(capsys.readouterr().out)
    assert row["stage_timings"]["auth.jwks"]["errors"] == 1
    assert "auth_recovered" not in row
    warm_row = observe(monkeypatch, capsys, oidc_fixture)
    assert "auth_recovered" not in warm_row


@pytest.mark.parametrize("field", ("issuer", "kid", "subject", "token", "public_key_material"))
def test_privacy_guard_rejects_values_from_the_actual_fixture(
    oidc_fixture,  # noqa: F811
    monkeypatch,
    capsys,
    field,
):
    token = oidc_fixture.token()
    secret = sensitive_values(oidc_fixture, token)[field]

    def inject(value):
        row = json.loads(value)
        row["synthetic_leak"] = secret
        builtins.print(json.dumps(row))

    monkeypatch.setattr(runtime, "print", inject, raising=False)
    with pytest.raises(AssertionError, match=f"sensitive field leaked: {field}"):
        observe(monkeypatch, capsys, oidc_fixture, token=token)


def test_recovery_metric_is_listed_in_operator_and_field_references():
    deploy = Path("docs/deploy.md").read_text()
    inventory = deploy.split("## 告警与处置\n", 1)[1].split("默认30天日志", 1)[0]
    assert "request-auth-recovered" in inventory.split("默认告警包括", 1)[0]
    assert "request-auth-recovered" in inventory.split("默认告警包括", 1)[1]
    assert "| request-auth-recovered" in deploy.replace("`", "")
    service = Path("docs/service.md").read_text()
    fields = service.split("### 冷启动分段计时\n", 1)[1].split("没有执行的阶段", 1)[0]
    assert "| `auth_recovered` |" in fields


def request_filter_matches(pattern, row):
    """Interpret the REQUEST log group's exact filter forms; reject unsupported drift."""
    operation = (
        "request_init" if pattern.startswith('{ $.operation = "request_init"') else "request"
    )
    prefix = f'{{ $.operation = "{operation}" && '
    assert pattern.startswith(prefix) and pattern.endswith(" }")
    condition = pattern[len(prefix) : -2]
    conditions = {
        ("request", "$.duration_ms = *"): lambda: "duration_ms" in row,
        (
            "request",
            '($.status = "internal failure" || $.status = "index not ready")',
        ): lambda: row.get("status") in {"internal failure", "index not ready"},
        ("request", '$.status = "rate limited"'): lambda: row.get("status") == "rate limited",
        ("request", '$.status = "unauthenticated"'): lambda: row.get("status") == "unauthenticated",
        ("request", "$.auth_recovered IS TRUE"): lambda: row.get("auth_recovered") is True,
        ("request_init", "$.oidc_prefetch.auth_recovered IS TRUE"): lambda: row.get(
            "oidc_prefetch", {}
        ).get("auth_recovered")
        is True,
        ("request_init", '$.oidc_prefetch.status = "failed"'): lambda: row.get(
            "oidc_prefetch", {}
        ).get("status")
        == "failed",
    }
    key = (operation, condition)
    assert key in conditions, f"unsupported real filter: {pattern}"
    return row.get("operation") == operation and conditions[key]()


@pytest.mark.parametrize("scenario", ["clean", "recovered", "terminal", "later_rejection"])
def test_real_request_metric_filters_ignore_attempt_events_without_changing_counts(
    monitoring,
    oidc_fixture,  # noqa: F811
    monkeypatch,
    capsys,
    scenario,
):
    if scenario in {"recovered", "later_rejection"}:
        oidc_fixture.steps["jwks"] = [httpx.ReadTimeout("synthetic private failure")]
    elif scenario == "terminal":
        oidc_fixture.steps["jwks"] = [httpx.Response(503), httpx.Response(503)]
    expected_error = {
        "terminal": ServiceError,
        "later_rejection": AuthenticationError,
    }.get(scenario)
    token = oidc_fixture.token(exp=NOW - 31) if scenario == "later_rejection" else None
    rows = []
    summary = observe(
        monkeypatch,
        capsys,
        oidc_fixture,
        token=token,
        expected_error=expected_error,
        captured_rows=rows,
    )
    events = [row for row in rows if row["operation"] == "oidc_fetch"]
    assert len(events) == (1 if scenario == "clean" else 2)
    expected_counts = {
        "request-count": 1,
        "request-internal-errors": int(scenario == "terminal"),
        "rate-limited": 0,
        "authentication-failed": int(scenario == "later_rejection"),
        "request-auth-recovered": int(scenario in {"recovered", "later_rejection"}),
        "request-init-auth-recovered": 0,
        "request-init-oidc-prefetch-failed": 0,
    }
    template, request_group, _ = monitoring
    filters = [
        resource["Properties"]
        for resource in template.find_resources("AWS::Logs::MetricFilter").values()
        if resource["Properties"]["LogGroupName"] == request_group
    ]
    assert len(filters) == len(expected_counts)
    for metric_filter in filters:
        assert len(metric_filter["MetricTransformations"]) == 1
        metric = metric_filter["MetricTransformations"][0]
        assert metric["MetricValue"] == "1"
        assert not metric.get("Dimensions")
        pattern = metric_filter["FilterPattern"]
        assert not any(request_filter_matches(pattern, row) for row in events)
        all_rows_count = sum(request_filter_matches(pattern, row) for row in rows)
        summary_only_count = int(request_filter_matches(pattern, summary))
        assert all_rows_count == summary_only_count == expected_counts[metric["MetricName"]]


@pytest.mark.parametrize("mutation", ["unknown_operation", "unexpected_field", "missing_field"])
def test_compatible_log_parser_still_rejects_unknown_or_malformed_attempt_lines(
    oidc_fixture,  # noqa: F811
    monkeypatch,
    capsys,
    mutation,
):
    def inject(value):
        row = json.loads(value)
        if row["operation"] == "oidc_fetch":
            if mutation == "unknown_operation":
                row["operation"] = "unexpected_operation"
            elif mutation == "unexpected_field":
                row["unexpected_field"] = True
            else:
                del row["failure"]
        builtins.print(json.dumps(row))

    monkeypatch.setattr(runtime, "print", inject, raising=False)
    with pytest.raises(AssertionError):
        observe(monkeypatch, capsys, oidc_fixture)


def test_attempt_event_fields_and_operator_references_are_documented():
    service = Path("docs/service.md").read_text()
    event_fields = service.split("#### JWKS 逐次读取事件\n", 1)[1].split(
        "#### 冷路径认证恢复\n", 1
    )[0]
    for field in EVENT_FIELDS["request"]:
        assert f"| `{field}` |" in event_fields
    assert "`attempt_finished`" in event_fields
    assert "`interrupted`" in event_fields
    for filename in (
        "docs/deploy.md",
        "docs/operations-runbook.md",
        "docs/security-and-data-flow.md",
    ):
        assert "oidc_fetch" in Path(filename).read_text()

"""Regression cases from the final independent ECS production review."""

import json
from dataclasses import replace

from aws_cdk import aws_ecs as ecs
from infra.assets import validate_lambda_asset
from test_ecs_production import configuration, props, resources, synth


def test_backend_diagnostics_cannot_change_any_legacy_search_metric_source(tmp_path):
    config = configuration()
    legacy = synth(replace(config, ecs_search=None), tmp_path / "legacy")
    enabled = synth(config, tmp_path / "enabled")

    def sources(document):
        result = {}
        for row in props(document, "AWS::Logs::MetricFilter"):
            metric = row["MetricTransformations"][0]
            identity = (
                metric["MetricNamespace"],
                metric["MetricName"],
                json.dumps(metric.get("Dimensions", {}), sort_keys=True),
            )
            result.setdefault(identity, set()).add(json.dumps(row["LogGroupName"], sort_keys=True))
        return result

    before, after = sources(legacy), sources(enabled)
    for identity, groups in before.items():
        assert after[identity] == groups, identity
    search_group = next(
        logical_id
        for logical_id, resource in resources(legacy, "AWS::Logs::LogGroup").items()
        if resource["Properties"]["LogGroupName"] == f"{config.prefix}-search-logs"
    )
    original_search_series = {
        row["MetricTransformations"][0]["MetricName"]
        for row in props(legacy, "AWS::Logs::MetricFilter")
        if row["LogGroupName"] == {"Ref": search_group}
    }
    backend_series = {"ecs": set(), "fallback": set()}
    for row in props(enabled, "AWS::Logs::MetricFilter"):
        name = row.get("FilterName", "")
        for backend in backend_series:
            if (
                name.endswith("-" + backend)
                and row["MetricTransformations"][0]["MetricName"] in original_search_series
            ):
                metric = row["MetricTransformations"][0]
                assert metric["MetricNamespace"] == f"semsearch/{config.prefix}/{backend}"
                backend_series[backend].add(metric["MetricName"])
    assert (
        backend_series["ecs"]
        == backend_series["fallback"]
        == {"search-count", "search-internal-errors", "search-degraded", "vector-failed"}
    )
    assert backend_series["ecs"] == backend_series["fallback"]
    old_alarms = resources(legacy, "AWS::CloudWatch::Alarm")
    new_alarms = resources(enabled, "AWS::CloudWatch::Alarm")
    assert all(new_alarms[name] == value for name, value in old_alarms.items())


def test_task_uses_existing_private_subnets_without_a_public_ip(tmp_path):
    document = synth(configuration(), tmp_path)
    private_ids = {
        name
        for name, row in resources(document, "AWS::EC2::Subnet").items()
        if not row["Properties"]["MapPublicIpOnLaunch"]
    }
    network = props(document, "AWS::ECS::Service")[0]["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert network["AssignPublicIp"] == "DISABLED"
    assert {row["Ref"] for row in network["Subnets"]} == private_ids
    assert len(private_ids) == len(props(document, "AWS::EC2::NatGateway")) == 2


def test_initializer_has_only_chown_capability_and_chmods_before_chown(tmp_path):
    document = synth(configuration(), tmp_path)
    initializer = next(
        row
        for row in props(document, "AWS::ECS::TaskDefinition")[0]["ContainerDefinitions"]
        if row["Name"] == "initialize-tmp"
    )
    capabilities = initializer["LinuxParameters"]["Capabilities"]
    supported = {capability.value for capability in ecs.Capability}
    assert set(capabilities["Drop"]) == supported - {"ALL", "CHOWN"}
    assert not capabilities.get("Add")
    command = initializer["Command"][0]
    assert command.index("os.chmod(") < command.index("os.chown(")
    assert initializer["User"] == "0:0" and initializer["ReadonlyRootFilesystem"]


def test_one_private_client_tls_failure_is_observable_at_low_traffic(tmp_path):
    document = synth(configuration(), tmp_path)
    client = next(
        row
        for row in props(document, "AWS::CloudWatch::Alarm")
        if row.get("MetricName") == "ClientTLSNegotiationErrorCount"
    )
    assert client["Threshold"] == 0
    assert client["EvaluationPeriods"] == client["DatapointsToAlarm"] == 1
    assert client["Period"] == 60 and client["Statistic"] == "Sum"


def test_slot_emission_and_scaling_use_the_construct_cluster_name(tmp_path):
    document = synth(configuration(), tmp_path)
    cluster = next(iter(resources(document, "AWS::ECS::Cluster")))
    service = props(document, "AWS::ECS::Service")[0]
    search = next(
        row
        for row in props(document, "AWS::ECS::TaskDefinition")[0]["ContainerDefinitions"]
        if row["Name"] == "search"
    )
    environment = {row["Name"]: row["Value"] for row in search["Environment"]}
    assert environment["SEMSEARCH_ECS_CLUSTER_NAME"] == {"Ref": cluster}
    assert environment["SEMSEARCH_ECS_SERVICE_NAME"] == service["ServiceName"]
    slot = next(
        row
        for row in props(document, "AWS::CloudWatch::Alarm")
        if row.get("MetricName") == "SearchSlotUtilization"
    )
    dimensions = {row["Name"]: row["Value"] for row in slot["Dimensions"]}
    assert dimensions == {
        "ClusterName": environment["SEMSEARCH_ECS_CLUSTER_NAME"],
        "ServiceName": environment["SEMSEARCH_ECS_SERVICE_NAME"],
    }
    policy = next(
        row["TargetTrackingScalingPolicyConfiguration"]["CustomizedMetricSpecification"]
        for row in props(document, "AWS::ApplicationAutoScaling::ScalingPolicy")
        if "CustomizedMetricSpecification" in row["TargetTrackingScalingPolicyConfiguration"]
    )
    assert policy["Dimensions"] == slot["Dimensions"]


def test_new_lambda_deadlines_and_entrypoint_include_fallback_cold_start(tmp_path):
    document = synth(configuration(), tmp_path)
    functions = props(document, "AWS::Lambda::Function")
    facade = next(row for row in functions if row["FunctionName"].endswith("-facade"))
    fallback = next(row for row in functions if row["FunctionName"].endswith("-fallback"))
    assert facade["Timeout"] == 15
    assert fallback["Timeout"] == 11
    assert facade["Handler"] == "semantic_service.facade_lambda.handler"
    assert fallback["Handler"] == "semantic_service.fallback.handler"
    original = [row for row in functions if row not in (facade, fallback)]
    assert {row["Timeout"] for row in original} == {10, 45, 240}


def test_ecs_asset_requires_independent_lambda_init_entrypoint(tmp_path):
    import zipfile

    import pytest

    synth(configuration(), tmp_path)
    archive = tmp_path / "ecs.zip"
    replacement = tmp_path / "missing-entry.zip"
    with zipfile.ZipFile(archive) as original, zipfile.ZipFile(replacement, "w") as modified:
        for info in original.infolist():
            if info.filename != "semantic_service/facade_lambda.py":
                modified.writestr(info, original.read(info))
    with pytest.raises(ValueError, match="facade_lambda.py"):
        validate_lambda_asset(replacement, "ecs")

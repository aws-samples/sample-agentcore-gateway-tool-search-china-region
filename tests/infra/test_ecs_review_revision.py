"""Regression cases from the independent production readiness reviews."""

from dataclasses import replace
from pathlib import Path

import pytest
from test_ecs_production import configuration, props, resources, synth
from test_request_deployment import run_synth


def test_enabled_assets_require_explicit_legacy_path_and_reject_same_file(tmp_path):
    from infra.assets import shared_asset_from_context

    with pytest.raises(ValueError, match="asset.*explicit"):
        shared_asset_from_context(True, None, tmp_path / "ecs.zip")
    with pytest.raises(ValueError, match="different"):
        shared_asset_from_context(True, "same.zip", Path("./same.zip"))
    assert shared_asset_from_context(False, None, None) == Path(".artifacts/lambda.zip")


def test_stack_rejects_identical_legacy_and_ecs_asset(tmp_path):
    with pytest.raises(ValueError, match="different"):
        synth(configuration(), tmp_path, ecs_asset_path=tmp_path / "lambda.zip")


@pytest.mark.parametrize(
    "ecs,flags,match",
    [
        (True, ("--ecs-asset", "new.zip"), "--asset.*required"),
        (True, ("--asset", "old.zip"), "--ecs-asset.*required"),
        (True, ("--asset", "same.zip", "--ecs-asset", "./same.zip"), "different"),
        (False, ("--ecs-asset", "new.zip"), "requires ecs_search"),
        (True, ("--asset", " ", "--ecs-asset", "new.zip"), "2"),
    ],
)
def test_cli_rejects_unsafe_assets_before_child_process(monkeypatch, tmp_path, ecs, flags, match):
    with pytest.raises(SystemExit, match=match):
        run_synth(monkeypatch, tmp_path, flags=flags, ecs=ecs)


def test_ecs_requires_notification_destination():
    with pytest.raises(ValueError, match="alarm_topic_arns"):
        replace(configuration(), alarm_topic_arns=())


def test_comma_is_not_a_single_oauth_scope():
    config = configuration()
    with pytest.raises(ValueError, match="scope"):
        replace(config, ecs_search=replace(config.ecs_search, oauth_scope="search.invoke,read"))


def test_extended_fallback_environment_is_bounded_before_synthesis(tmp_path):
    config = configuration()
    name = "discovery" + "x" * 230 + "___search_tools"
    config = replace(
        config,
        gateway_id="g" * 100,
        search_target_id="t" * 100,
        search_incarnation="i" * 100,
        search_descriptor=config.search_descriptor | {"name": name},
        registry=[
            {"name": name, "target_id": "t" * 100, "incarnation": "i" * 100, "role": "discovery"}
        ],
        secrets={
            kind: replace(ref, name=(kind + "/" + "x" * 470)[:470])
            for kind, ref in config.secrets.items()
        },
    )
    # All four legacy environments fit (largest 4065 bytes); fallback exceeds 4096.
    synth(replace(config, ecs_search=None), tmp_path / "legacy")
    with pytest.raises(ValueError, match="fallback.*4 KiB"):
        synth(config, tmp_path / "enabled")


def test_facade_environment_is_bounded_before_synthesis(tmp_path):
    config = configuration()
    config = replace(
        config,
        oidc=config.oidc | {"OIDC_ALLOWED_HOSTS": ",".join(["identity.example.invalid"] * 180)},
    )
    synth(replace(config, ecs_search=None), tmp_path / "legacy")
    with pytest.raises(ValueError, match="facade.*4 KiB"):
        synth(config, tmp_path / "enabled")


def test_nlb_can_be_deleted_by_first_deployment_rollback(tmp_path):
    document = synth(configuration(), tmp_path)
    nlb = next(iter(resources(document, "AWS::ElasticLoadBalancingV2::LoadBalancer").values()))
    attrs = {row["Key"]: row["Value"] for row in nlb["Properties"]["LoadBalancerAttributes"]}
    assert (
        attrs.get("deletion_protection.enabled", "false") == "false"
        or nlb.get("DeletionPolicy") == "Retain"
    )


def test_new_alarms_have_actions_descriptions_and_single_security_event_detection(tmp_path):
    config = configuration()
    alarms = props(synth(config, tmp_path), "AWS::CloudWatch::Alarm")
    for alarm in alarms:
        assert alarm.get("AlarmActions") == list(config.alarm_topic_arns)
        assert alarm.get("AlarmDescription")
        if alarm["AlarmName"].endswith(
            ("ecs-auth-failed", "ecs-replay-failed", "ecs-backend-errors")
        ):
            assert alarm["EvaluationPeriods"] == alarm["DatapointsToAlarm"] == 1
    client = next(
        row for row in alarms if row.get("MetricName") == "ClientTLSNegotiationErrorCount"
    )
    target = next(
        row for row in alarms if row.get("MetricName") == "TargetTLSNegotiationErrorCount"
    )
    assert client["Threshold"] == target["Threshold"] == 0


def test_flow_slot_and_latency_metrics_cover_new_execution_path(tmp_path):
    config = configuration()
    document = synth(config, tmp_path)
    alarms = props(document, "AWS::CloudWatch::Alarm")
    names = {row.get("MetricName") for row in alarms}
    assert {
        "NewFlowCount",
        "ActiveFlowCount",
        "SearchSlotUtilization",
        "ecs-search-duration-ms",
    } <= names
    facade = next(row for row in alarms if row["AlarmName"].endswith("facade-execution-p95"))
    assert facade["MetricName"] == "Duration" and facade["ExtendedStatistic"] == "p95"
    assert facade["Dimensions"] == [{"Name": "FunctionName", "Value": config.prefix + "-facade"}]
    policies = props(document, "AWS::ApplicationAutoScaling::ScalingPolicy")
    custom = [
        row["TargetTrackingScalingPolicyConfiguration"]
        for row in policies
        if "CustomizedMetricSpecification"
        in row.get("TargetTrackingScalingPolicyConfiguration", {})
    ]
    assert len(custom) == 1
    metric = custom[0]["CustomizedMetricSpecification"]
    assert metric["MetricName"] == "SearchSlotUtilization"
    assert metric["Namespace"] == "semsearch/" + config.prefix
    assert metric["Statistic"] == "Average" and custom[0]["TargetValue"] == 60
    assert metric["Dimensions"] == [
        {
            "Name": "ClusterName",
            "Value": {"Ref": next(iter(resources(document, "AWS::ECS::Cluster")))},
        },
        {"Name": "ServiceName", "Value": config.prefix + "-search"},
    ]


def test_health_listener_is_reserved_and_search_capabilities_are_dropped(tmp_path):
    config = configuration()
    document = synth(config, tmp_path)
    target = props(document, "AWS::ElasticLoadBalancingV2::TargetGroup")[0]
    assert target["HealthCheckPort"] == "9443"
    health = [
        row
        for row in props(document, "AWS::EC2::SecurityGroupIngress")
        if row.get("FromPort") == 9443
    ]
    assert len(health) == 1 and "SourceSecurityGroupId" in health[0] and "CidrIp" not in health[0]
    task = props(document, "AWS::ECS::TaskDefinition")[0]
    search = next(row for row in task["ContainerDefinitions"] if row["Name"] == "search")
    assert search["LinuxParameters"]["Capabilities"]["Drop"] == ["ALL"]
    assert search["StartTimeout"] == 30
    assert search["HealthCheck"]["Command"] == [
        "CMD",
        "python",
        "-m",
        "semantic_service.ecs_health",
    ]
    env = {row["Name"]: row["Value"] for row in search["Environment"]}
    assert env["SEMSEARCH_ECS_HEALTH_PORT"] == "9443"
    assert env["SEMSEARCH_ECS_METRIC_NAMESPACE"] == "semsearch/" + config.prefix
    assert env["SEMSEARCH_ECS_CLUSTER_NAME"] == {
        "Ref": next(iter(resources(document, "AWS::ECS::Cluster")))
    }
    assert env["SEMSEARCH_ECS_SERVICE_NAME"] == config.prefix + "-search"
    healthcheck = search["HealthCheck"]
    dockerfile = Path("containers/search/Dockerfile").read_text()
    assert (
        f"HEALTHCHECK --interval={healthcheck['Interval']}s "
        f"--timeout={healthcheck['Timeout']}s --start-period={healthcheck['StartPeriod']}s "
        f"--retries={healthcheck['Retries']}"
    ) in dockerfile
    assert 'CMD ["python", "-m", "semantic_service.ecs_health"]' in dockerfile


def test_blank_asset_error_names_the_shared_argument_rule(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit):
        run_synth(monkeypatch, tmp_path, flags=("--asset", " "))
    error = capsys.readouterr().err
    assert "asset argument requires a nonempty ZIP path" in error
    assert "--request-asset requires" not in error


@pytest.mark.parametrize("region", ["cn-north-1", "cn-northwest-1"])
def test_private_entry_has_two_az_egress_and_only_facade_can_reach_nlb(region, tmp_path):
    config = configuration(region)
    document = synth(config, tmp_path)
    nlb = props(document, "AWS::ElasticLoadBalancingV2::LoadBalancer")[0]
    assert nlb["Scheme"] == "internal"
    subnets = resources(document, "AWS::EC2::Subnet")
    assert len(subnets) == 4
    public_ids = {key for key, row in subnets.items() if row["Properties"]["MapPublicIpOnLaunch"]}
    private_ids = set(subnets) - public_ids
    assert len(private_ids) == len(public_ids) == 2
    assert {row["Ref"] for row in nlb["Subnets"]} == private_ids
    nat = resources(document, "AWS::EC2::NatGateway")
    assert len(nat) == len(props(document, "AWS::EC2::EIP")) == 2
    assert {row["Properties"]["SubnetId"]["Ref"] for row in nat.values()} == public_ids
    associations = props(document, "AWS::EC2::SubnetRouteTableAssociation")
    routes = props(document, "AWS::EC2::Route")
    for private in private_ids:
        association = next(row for row in associations if row["SubnetId"] == {"Ref": private})
        route = next(
            row
            for row in routes
            if row["RouteTableId"] == association["RouteTableId"] and "NatGatewayId" in row
        )
        nat_subnet = nat[route["NatGatewayId"]["Ref"]]["Properties"]["SubnetId"]["Ref"]
        assert (
            subnets[private]["Properties"]["AvailabilityZone"]
            == subnets[nat_subnet]["Properties"]["AvailabilityZone"]
        )
    facade = next(
        row
        for row in props(document, "AWS::Lambda::Function")
        if row["Handler"] == "semantic_service.facade_lambda.handler"
    )
    assert {row["Ref"] for row in facade["VpcConfig"]["SubnetIds"]} == private_ids
    facade_group = facade["VpcConfig"]["SecurityGroupIds"][0]
    nlb_group = nlb["SecurityGroups"][0]
    groups = resources(document, "AWS::EC2::SecurityGroup")
    assert not groups[facade_group["Fn::GetAtt"][0]]["Properties"].get("SecurityGroupIngress")
    inbound = props(document, "AWS::EC2::SecurityGroupIngress")
    inbound += [
        dict(row, GroupId={"Fn::GetAtt": [key, "GroupId"]})
        for key, group in groups.items()
        for row in group["Properties"].get("SecurityGroupIngress", [])
    ]
    nlb_inbound = [row for row in inbound if row["GroupId"] == nlb_group]
    assert len(nlb_inbound) == 1
    assert nlb_inbound[0]["FromPort"] == nlb_inbound[0]["ToPort"] == 443
    assert nlb_inbound[0]["SourceSecurityGroupId"] == facade_group
    assert not any(row.get("CidrIp") == "0.0.0.0/0" for row in inbound)
    service = props(document, "AWS::ECS::Service")[0]
    assert {
        row["Ref"] for row in service["NetworkConfiguration"]["AwsvpcConfiguration"]["Subnets"]
    } == private_ids
    assert service["NetworkConfiguration"]["AwsvpcConfiguration"]["AssignPublicIp"] == "DISABLED"


def test_only_facade_gets_network_permission_and_function_code_is_explicitly_denied(tmp_path):
    from test_ecs_production import listify, statements

    config = configuration()
    document = synth(config, tmp_path)
    expected = {
        "ec2:CreateNetworkInterface",
        "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeSubnets",
        "ec2:DeleteNetworkInterface",
        "ec2:AssignPrivateIpAddresses",
        "ec2:UnassignPrivateIpAddresses",
    }
    for function in props(document, "AWS::Lambda::Function"):
        role = function["Role"]["Fn::GetAtt"][0]
        policy = statements(document, role)
        eni = [
            row
            for row in policy
            if any(action.startswith("ec2:") for action in listify(row["Action"]))
        ]
        if function["Handler"] != "semantic_service.facade_lambda.handler":
            assert "VpcConfig" not in function and eni == []
            continue
        assert "ManagedPolicyArns" not in document["Resources"][role]["Properties"]
        allow = next(row for row in eni if row["Effect"] == "Allow")
        deny = next(row for row in eni if row["Effect"] == "Deny")
        assert set(listify(allow["Action"])) == expected
        assert listify(allow["Resource"]) == ["*"]
        assert set(listify(deny["Action"])) == expected | {"ec2:DetachNetworkInterface"}
        assert listify(deny["Resource"]) == ["*"]
        source = deny["Condition"]["ArnEquals"]["lambda:SourceFunctionArn"]
        from test_stack import resolve

        assert (
            resolve(source, config)
            == f"arn:aws-cn:lambda:{config.region}:{config.account}:function:{config.prefix}-facade"
        )


def test_log_delivery_cannot_block_search_threads(tmp_path):
    document = synth(configuration(), tmp_path)
    for container in props(document, "AWS::ECS::TaskDefinition")[0]["ContainerDefinitions"]:
        options = container["LogConfiguration"]["Options"]
        assert options["mode"] == "non-blocking"
        assert options["max-buffer-size"] == "4194304b"


def test_worker_initialization_and_reclaim_failures_alarm_without_a_phase_field(tmp_path):
    document = synth(configuration(), tmp_path)
    filters = {
        row["MetricTransformations"][0]["MetricName"]: row["FilterPattern"]
        for row in props(document, "AWS::Logs::MetricFilter")
    }
    pattern = filters["ecs-worker-failed"]
    assert "$.phase" not in pattern
    for status in ("worker initialization failed", "worker reclaim failed", "worker unavailable"):
        assert status in pattern
    assert "worker reaped" not in pattern
    alarm = next(
        row
        for row in props(document, "AWS::CloudWatch::Alarm")
        if row.get("MetricName") == "ecs-worker-failed"
    )
    assert alarm["Threshold"] == 0
    assert alarm["EvaluationPeriods"] == alarm["DatapointsToAlarm"] == 1
    assert alarm["AlarmActions"] and alarm["AlarmDescription"]


def test_service_health_grace_covers_both_startup_pools_and_nlb_probe_window(tmp_path):
    document = synth(configuration(), tmp_path)
    task = props(document, "AWS::ECS::TaskDefinition")[0]
    search = next(row for row in task["ContainerDefinitions"] if row["Name"] == "search")
    assert search["HealthCheck"]["StartPeriod"] == 120
    target = props(document, "AWS::ElasticLoadBalancingV2::TargetGroup")[0]
    required = 120 + target["HealthCheckIntervalSeconds"] * (target["HealthyThresholdCount"] + 1)
    assert props(document, "AWS::ECS::Service")[0]["HealthCheckGracePeriodSeconds"] >= required

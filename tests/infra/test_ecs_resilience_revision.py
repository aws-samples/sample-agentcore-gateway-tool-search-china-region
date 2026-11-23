"""Network, cost and deployment dependency cases from the resilience review."""

from dataclasses import replace

import pytest
from test_ecs_production import configuration, props, resources, synth
from test_ecs_production import statements as role_statements

KEPT = {"search-count", "search-internal-errors", "search-degraded", "vector-failed"}


def dependencies(document, logical_id):
    result = set()

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("Ref"), str):
                result.add(value["Ref"])
            if isinstance(value.get("Fn::GetAtt"), list):
                result.add(value["Fn::GetAtt"][0])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    pending = [logical_id]
    visited = set()
    while pending:
        current = pending.pop()
        if current in visited or current not in document["Resources"]:
            continue
        visited.add(current)
        node = document["Resources"][current]
        explicit = node.get("DependsOn", [])
        result.update([explicit] if isinstance(explicit, str) else explicit)
        walk(node.get("Properties", {}))
        pending.extend(result - visited)
    return result


def test_only_actionable_backend_diagnostics_are_billed_and_all_have_alarm_consumers(tmp_path):
    config = configuration()
    document = synth(config, tmp_path / "new")
    baseline = synth(replace(config, ecs_search=None), tmp_path / "legacy")
    old_filters = resources(baseline, "AWS::Logs::MetricFilter")
    new_filters = resources(document, "AWS::Logs::MetricFilter")
    old_alarms = resources(baseline, "AWS::CloudWatch::Alarm")
    new_alarms = resources(document, "AWS::CloudWatch::Alarm")
    assert all(new_filters[name] == value for name, value in old_filters.items())
    assert all(new_alarms[name] == value for name, value in old_alarms.items())
    for backend in ("ecs", "fallback"):
        namespace = f"semsearch/{config.prefix}/{backend}"
        actual = {
            row["MetricTransformations"][0]["MetricName"]
            for row in props(document, "AWS::Logs::MetricFilter")
            if row["MetricTransformations"][0]["MetricNamespace"] == namespace
        }
        assert actual == KEPT
        consumed = set()
        for alarm in props(document, "AWS::CloudWatch::Alarm"):
            if alarm.get("Namespace") == namespace:
                consumed.add(alarm["MetricName"])
            for metric in alarm.get("Metrics", []):
                spec = metric.get("MetricStat", {}).get("Metric", {})
                if spec.get("Namespace") == namespace:
                    consumed.add(spec["MetricName"])
        assert consumed == KEPT


def test_private_task_waits_for_both_nat_routes_and_gateway_endpoints(tmp_path):
    document = synth(configuration(), tmp_path)
    service = next(iter(resources(document, "AWS::ECS::Service")))
    closure = dependencies(document, service)
    routes = {
        name
        for name, row in resources(document, "AWS::EC2::Route").items()
        if "NatGatewayId" in row["Properties"]
    }
    assert len(routes) == 2 and routes <= closure
    endpoints = resources(document, "AWS::EC2::VPCEndpoint")
    assert len(endpoints) == 2 and set(endpoints) <= closure
    private_tables = {
        row["Properties"]["RouteTableId"]["Ref"]
        for row in resources(document, "AWS::EC2::Route").values()
        if "NatGatewayId" in row["Properties"]
    }
    for endpoint in endpoints.values():
        assert endpoint["Properties"]["VpcEndpointType"] == "Gateway"
        assert {value["Ref"] for value in endpoint["Properties"]["RouteTableIds"]} == private_tables


def test_gateway_policies_keep_ecr_layer_access_and_exact_runtime_read_scopes(tmp_path):
    document = synth(configuration(), tmp_path)
    endpoints = props(document, "AWS::EC2::VPCEndpoint")
    assert len(endpoints) == 2
    policies = [row["PolicyDocument"]["Statement"] for row in endpoints]
    statements = [statement for policy in policies for statement in policy]
    layer = next(row for row in statements if "starport-layer-bucket" in str(row.get("Resource")))
    assert layer["Action"] == "s3:GetObject"
    assert layer["Principal"] == "*"
    assert "prod-" in str(layer["Resource"]) and "AWS::Region" in str(layer["Resource"])
    for row in statements:
        actions = row["Action"] if isinstance(row["Action"], list) else [row["Action"]]
        assert row["Effect"] == "Allow"
        assert row["Resource"] != "*"
        assert set(actions) <= {
            "s3:GetObject",
            "s3:GetObjectVersion",
            "dynamodb:GetItem",
            "dynamodb:PutItem",
        }
        if row is not layer:
            assert row["Principal"] == "*"
            assert "TaskRole" in str(row["Condition"]["StringEquals"]["aws:PrincipalArn"])
    assert sum("s3:VersionId" in str(row.get("Condition", {})) for row in statements) == 1
    replay = next(row for row in statements if row["Action"] == "dynamodb:PutItem")
    task_role = props(document, "AWS::ECS::TaskDefinition")[0]["TaskRoleArn"]
    assert replay["Condition"] == {"StringEquals": {"aws:PrincipalArn": task_role}}


@pytest.mark.parametrize("region", ["cn-north-1", "cn-northwest-1"])
def test_dynamodb_endpoint_limits_actions_tables_and_role_without_duplicate_key_conditions(
    tmp_path, region
):
    config = configuration(region)
    document = synth(config, tmp_path)
    task_role = props(document, "AWS::ECS::TaskDefinition")[0]["TaskRoleArn"]
    policies = role_statements(document, task_role["Fn::GetAtt"][0])
    dynamodb_permissions = [
        row for row in policies if row["Action"] in ("dynamodb:GetItem", "dynamodb:PutItem")
    ]
    tables = resources(document, "AWS::DynamoDB::Table")
    state = next(name for name in tables if name.startswith("StorageState"))
    protected = next(name for name in tables if name.startswith("StorageProtected"))
    replay = next(name for name in tables if name.startswith("ReplayTable"))
    assert dynamodb_permissions == [
        {
            "Action": "dynamodb:GetItem",
            "Effect": "Allow",
            "Resource": {"Fn::GetAtt": [state, "Arn"]},
            "Condition": {
                "ForAllValues:StringEquals": {
                    "dynamodb:LeadingKeys": [
                        {
                            "Fn::Join": [
                                "",
                                [
                                    {"Ref": "AWS::AccountId"},
                                    "/",
                                    {"Ref": "AWS::Region"},
                                    f"/{config.gateway_id}/CONTROL",
                                ],
                            ]
                        }
                    ]
                },
                "Null": {"dynamodb:LeadingKeys": "false"},
            },
        },
        {
            "Action": "dynamodb:GetItem",
            "Effect": "Allow",
            "Resource": {"Fn::GetAtt": [protected, "Arn"]},
        },
        {
            "Action": "dynamodb:PutItem",
            "Effect": "Allow",
            "Resource": {"Fn::GetAtt": [replay, "Arn"]},
            "Condition": {
                "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["ecs/*"]},
                "Null": {"dynamodb:LeadingKeys": "false"},
            },
        },
    ]
    endpoints = props(document, "AWS::EC2::VPCEndpoint")
    endpoint = next(row for row in endpoints if ".dynamodb" in str(row["ServiceName"]))
    assert endpoint["PolicyDocument"]["Statement"] == [
        {
            "Action": action,
            "Effect": "Allow",
            "Principal": "*",
            "Resource": {"Fn::GetAtt": [table, "Arn"]},
            "Condition": {"StringEquals": {"aws:PrincipalArn": task_role}},
        }
        for action, table in (
            ("dynamodb:GetItem", state),
            ("dynamodb:GetItem", protected),
            ("dynamodb:PutItem", replay),
        )
    ]
    # S3 retains its version condition as well as the exact task-role boundary.
    s3_endpoint = next(row for row in endpoints if ".s3" in str(row["ServiceName"]))
    s3_permissions = [row for row in policies if row["Action"] == "s3:GetObjectVersion"]
    s3_statements = s3_endpoint["PolicyDocument"]["Statement"]
    assert s3_statements[:-1] == [
        row
        | {
            "Principal": "*",
            "Condition": {
                **row.get("Condition", {}),
                "StringEquals": {
                    **row.get("Condition", {}).get("StringEquals", {}),
                    "aws:PrincipalArn": task_role,
                },
            },
        }
        for row in s3_permissions
    ]
    assert s3_statements[0]["Condition"]["StringEquals"]["s3:VersionId"] == {
        "Ref": "SearchConfigVersionId"
    }


def test_health_windows_cover_the_shared_application_startup_budget(tmp_path):
    from semantic_service.ecs_workers import APPLICATION_STARTUP_SECONDS

    document = synth(configuration(), tmp_path)
    container = props(document, "AWS::ECS::TaskDefinition")[0]["ContainerDefinitions"][0]
    service = props(document, "AWS::ECS::Service")[0]
    target = props(document, "AWS::ElasticLoadBalancingV2::TargetGroup")[0]
    assert container["HealthCheck"]["StartPeriod"] >= APPLICATION_STARTUP_SECONDS
    healthy_window = target["HealthCheckIntervalSeconds"] * target["HealthyThresholdCount"]
    assert service["HealthCheckGracePeriodSeconds"] >= (
        APPLICATION_STARTUP_SECONDS + healthy_window
    )

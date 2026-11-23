"""Component assertions that do not depend on unresolved deployment configuration."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template
from infra.functions import Functions
from infra.monitoring import add_monitoring
from infra.scheduling import add_schedules
from infra.storage import Storage

from semantic_service.auth import (
    AuthenticationError,
    IndexNotReadyError,
    RateLimitError,
    ServiceError,
)


@pytest.fixture(params=("cn-north-1", "cn-northwest-1"))
def components(request: pytest.FixtureRequest, tmp_path: Path) -> dict:
    archive = tmp_path / "lambda.zip"
    with zipfile.ZipFile(archive, "w") as package:
        for module in ("__init__", "runtime", "search", "indexer", "enhancer"):
            package.writestr(f"semantic_service/{module}.py", "# fixture\n")
        package.writestr("semantic_core/__init__.py", "# fixture\n")
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    stack = cdk.Stack(
        app,
        "semsearch-components",
        env=cdk.Environment(account="111122223333", region=request.param),
    )
    cdk.Tags.of(stack).add("project", "semsearch")
    storage = Storage(stack, "Storage", prefix="semsearch-components")
    operations = ("search", "request", "indexer", "enhancer")
    functions = Functions(
        stack,
        "Functions",
        prefix="semsearch-components",
        asset_path=archive,
        environments={operation: {} for operation in operations},
        permissions={
            operation: [
                iam.PolicyStatement(
                    actions=["dynamodb:GetItem"],
                    resources=[storage.table.table_arn],
                    conditions={
                        "ForAllValues:StringEquals": {
                            "dynamodb:LeadingKeys": [
                                f"111122223333/{request.param}/fixture/CONTROL"
                            ]
                        }
                    },
                )
            ]
            for operation in operations
        },
    )
    add_schedules(stack, prefix="semsearch-components", aliases=functions.aliases, enabled=False)
    add_monitoring(
        stack,
        prefix="semsearch-components",
        log_groups=functions.log_groups,
        function_names=functions.names,
    )
    return Template.from_stack(stack).to_json()["Resources"]


def _resources(components: dict, kind: str) -> dict:
    return {key: value for key, value in components.items() if value["Type"] == kind}


def _role_statements(components: dict, role_id: str) -> list[dict]:
    statements = [
        statement
        for policy in components[role_id]["Properties"].get("Policies", [])
        for statement in policy["PolicyDocument"]["Statement"]
    ]
    for policy in _resources(components, "AWS::IAM::Policy").values():
        properties = policy["Properties"]
        if {"Ref": role_id} in properties.get("Roles", []):
            statements.extend(properties["PolicyDocument"]["Statement"])
    return statements


def test_each_function_can_write_only_its_log_group(components: dict) -> None:
    functions = _resources(components, "AWS::Lambda::Function")
    assert len(functions) == 4
    for function in functions.values():
        properties = function["Properties"]
        role_id = properties["Role"]["Fn::GetAtt"][0]
        group_id = properties["LoggingConfig"]["LogGroup"]["Ref"]
        statements = _role_statements(components, role_id)
        log_statements = [
            statement for statement in statements if "logs:" in json.dumps(statement["Action"])
        ]
        assert len(log_statements) == 1
        assert log_statements[0] == {
            "Effect": "Allow",
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            # CloudFormation's LogGroup Arn already includes its stream wildcard.
            "Resource": {"Fn::GetAtt": [group_id, "Arn"]},
        }
    for policy in _resources(components, "AWS::IAM::Policy").values():
        assert policy["Properties"]["PolicyName"].startswith("semsearch-")


def test_deployed_memory_bounds_snapshot_restore_budget(components: dict) -> None:
    from semantic_service import storage as service_storage

    functions = _resources(components, "AWS::Lambda::Function")
    assert len(functions) == 4
    properties = [function["Properties"] for function in functions.values()]
    memory_mib = {
        item["FunctionName"].removeprefix("semsearch-components-"): item["MemorySize"]
        for item in properties
    }
    assert memory_mib == {"search": 2048, "request": 1769, "indexer": 1024, "enhancer": 1024}
    # REQUEST 不恢复快照，按角色排除；此集合不由内存大小排序决定。
    smallest_snapshot_worker_mib = min(
        memory_mib[operation] for operation in ("search", "indexer", "enhancer")
    )
    budget_bytes = smallest_snapshot_worker_mib * 1024 * 1024 // 4
    assert 0 < service_storage._SNAPSHOT_RESTORE_BUDGET <= budget_bytes


def test_scheduler_role_and_async_delivery_bind_the_same_alias(components: dict) -> None:
    groups = _resources(components, "AWS::Scheduler::ScheduleGroup")
    assert len(groups) == 1
    group_id = next(iter(groups))
    schedules = _resources(components, "AWS::Scheduler::Schedule")
    configurations = _resources(components, "AWS::Lambda::EventInvokeConfig")
    assert len(schedules) == len(configurations) == 2
    observed_aliases = set()
    for schedule in schedules.values():
        properties = schedule["Properties"]
        assert properties["State"] == "DISABLED"
        target = properties["Target"]
        alias_id = target["Arn"]["Ref"]
        observed_aliases.add(alias_id)
        alias = components[alias_id]["Properties"]
        assert alias["Name"] == "live"
        function_ref = alias["FunctionName"]
        matching = [
            item["Properties"]
            for item in configurations.values()
            if item["Properties"]["FunctionName"] == function_ref
        ]
        assert len(matching) == 1
        assert matching[0]["Qualifier"] == {
            "Fn::Select": [7, {"Fn::Split": [":", {"Ref": alias_id}]}]
        }
        assert matching[0]["MaximumEventAgeInSeconds"] == 60
        assert matching[0]["MaximumRetryAttempts"] == 0
        role_id = target["RoleArn"]["Fn::GetAtt"][0]
        role = components[role_id]["Properties"]
        trust = role["AssumeRolePolicyDocument"]["Statement"]
        assert trust == [
            {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "scheduler.amazonaws.com"},
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}},
                    "ArnEquals": {"aws:SourceArn": {"Fn::GetAtt": [group_id, "Arn"]}},
                },
            }
        ]
        assert _role_statements(components, role_id) == [
            {
                "Effect": "Allow",
                "Action": "lambda:InvokeFunction",
                "Resource": target["Arn"],
            }
        ]
    assert len(observed_aliases) == 2


def test_china_storage_and_log_contracts(components: dict) -> None:
    rendered = json.dumps(components)
    assert "arn:" + "aws:" not in rendered
    assert "lambda.amazonaws.com.cn" not in rendered
    assert "scheduler.amazonaws.com.cn" not in rendered
    buckets = _resources(components, "AWS::S3::Bucket")
    assert len(buckets) == 1
    bucket = next(iter(buckets.values()))
    assert bucket["DeletionPolicy"] == bucket["UpdateReplacePolicy"] == "Retain"
    assert bucket["Properties"]["VersioningConfiguration"] == {"Status": "Enabled"}
    assert bucket["Properties"]["BucketEncryption"] == {
        "ServerSideEncryptionConfiguration": [
            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
        ]
    }
    tables = _resources(components, "AWS::DynamoDB::Table")
    assert len(tables) == 2
    for table in tables.values():
        assert table["Properties"]["SSESpecification"] == {"SSEEnabled": True}
        assert table["Properties"]["DeletionProtectionEnabled"] is True
        assert table["Properties"]["PointInTimeRecoverySpecification"] == {
            "PointInTimeRecoveryEnabled": True
        }
        assert table["DeletionPolicy"] == table["UpdateReplacePolicy"] == "Retain"
    groups = _resources(components, "AWS::Logs::LogGroup")
    assert len(groups) == 4
    assert all(group["Properties"]["RetentionInDays"] == 30 for group in groups.values())
    filters = {
        item["Properties"]["MetricTransformations"][0]["MetricName"]: item["Properties"]
        for item in _resources(components, "AWS::Logs::MetricFilter").values()
    }
    assert filters["authentication-failed"]["FilterPattern"] == (
        f'{{ $.operation = "request" && $.status = "{AuthenticationError.message}" }}'
    )
    assert filters["rate-limited"]["FilterPattern"] == (
        f'{{ $.operation = "request" && $.status = "{RateLimitError.message}" }}'
    )
    for operation in ("search", "request", "indexer", "enhancer"):
        assert filters[f"{operation}-internal-errors"]["FilterPattern"] == (
            f'{{ $.operation = "{operation}" && ($.status = "{ServiceError.message}" '
            f'|| $.status = "{IndexNotReadyError.message}") }}'
        )
    native_error_alarms = [
        resource["Properties"]
        for resource in _resources(components, "AWS::CloudWatch::Alarm").values()
        if resource["Properties"].get("Namespace") == "AWS/Lambda"
        and resource["Properties"].get("MetricName") == "Errors"
    ]
    assert len(native_error_alarms) == 4
    function_names = {
        resource["Properties"]["FunctionName"]
        for resource in _resources(components, "AWS::Lambda::Function").values()
    }
    assert {alarm["Dimensions"][0]["Value"] for alarm in native_error_alarms} == function_names
    for alarm in native_error_alarms:
        assert len(alarm["Dimensions"]) == 1
        assert alarm["Dimensions"][0]["Name"] == "FunctionName"
        assert alarm["Statistic"] == "Sum"
        assert alarm["Period"] == 300
        assert alarm["Threshold"] == 0
        assert alarm["ComparisonOperator"] == "GreaterThanThreshold"

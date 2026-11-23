"""Offline deployment assertions: test the synthesized contract, never query AWS."""

import fnmatch
import hashlib
import inspect
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from infra.config import DeploymentConfig
from infra.stack import SemsearchStack, documents

from semantic_service import runtime
from semantic_service.deployment_config import CONFIGURATION_FIELDS
from semantic_service.secrets import SecretsCache


@pytest.fixture(params=["cn-north-1", "cn-northwest-1"])
def template(request, tmp_path):
    config = DeploymentConfig.from_file(Path("infra/config.example.json"), region=request.param)
    return synthesize(config, tmp_path)


def synthesize(config, directory, *, asset_path=None, request_asset_path=None):
    directory.mkdir(parents=True, exist_ok=True)
    archive = asset_path or directory / "lambda.zip"
    if asset_path is None:
        with zipfile.ZipFile(archive, "w") as package:
            for module in ("__init__", "runtime", "search", "indexer", "enhancer"):
                package.writestr(f"semantic_service/{module}.py", "# fixture\n")
            package.writestr("semantic_core/__init__.py", "# fixture\n")
    app = cdk.App(outdir=str(directory / "cdk.out"))
    stack = SemsearchStack(
        app,
        "semsearch-test",
        config=config,
        asset_path=archive,
        request_asset_path=request_asset_path,
        env=cdk.Environment(account=config.account, region=config.region),
    )
    return Template.from_stack(stack)


def resources(template, kind):
    return list(template.find_resources(kind).values())


def as_list(value):
    return value if isinstance(value, list) else [value]


def functions_by_operation(template):
    return {
        function["Properties"]["Handler"].rsplit(".", 1)[1].removesuffix("_handler"): function[
            "Properties"
        ]
        for function in resources(template, "AWS::Lambda::Function")
    }


def test_request_asset_changes_only_request_resources(template, tmp_path):
    """A REQUEST-only package cannot update Search, Indexer, Enhancer or their aliases."""
    config = replace(fixture_config(template), request_package="request")
    request_asset = tmp_path / "request.zip"
    with zipfile.ZipFile(request_asset, "w") as archive:
        for module in ("__init__", "runtime", "request_runtime"):
            archive.writestr(f"semantic_service/{module}.py", "# fixture\n")
        archive.writestr("semantic_core/__init__.py", "# fixture\n")
    after_template = synthesize(
        config,
        tmp_path / "request",
        asset_path=tmp_path / "lambda.zip",
        request_asset_path=request_asset,
    )
    assert functions_by_operation(after_template)["request"]["Handler"] == (
        "semantic_service.request_runtime.request_handler"
    )
    left = template.to_json()["Resources"]
    right = after_template.to_json()["Resources"]
    changed = {name for name in left.keys() | right.keys() if left.get(name) != right.get(name)}
    assert changed
    assert all("request" in name for name in changed), changed


@pytest.mark.parametrize("region", ["cn-north-1", "cn-northwest-1"])
def test_request_pc_waits_for_init_monitoring_without_changing_other_aliases(tmp_path, region):
    config = replace(
        DeploymentConfig.from_file(Path("infra/config.example.json"), region=region),
        request_package="request",
    )
    request_asset = tmp_path / "request.zip"
    with zipfile.ZipFile(request_asset, "w") as archive:
        for module in ("__init__", "runtime", "request_runtime"):
            archive.writestr(f"semantic_service/{module}.py", "# offline fixture\n")
        archive.writestr("semantic_core/__init__.py", "# offline fixture\n")
    disabled = synthesize(config, tmp_path / "disabled", request_asset_path=request_asset)
    enabled = synthesize(
        replace(config, request_provisioned_concurrency=1),
        tmp_path / "enabled",
        asset_path=tmp_path / "disabled" / "lambda.zip",
        request_asset_path=request_asset,
    )
    metric_names = {"request-init-auth-recovered", "request-init-oidc-prefetch-failed"}
    monitoring_ids = {
        logical_id
        for logical_id, resource in enabled.to_json()["Resources"].items()
        if (
            resource["Type"] == "AWS::Logs::MetricFilter"
            and resource["Properties"]["MetricTransformations"][0]["MetricName"] in metric_names
        )
        or (
            resource["Type"] == "AWS::CloudWatch::Alarm"
            and resource["Properties"].get("MetricName") in metric_names
        )
    }
    assert len(monitoring_ids) == 4

    def aliases_by_operation(template):
        operations = {
            logical_id: resource["Properties"]["Handler"].rsplit(".", 1)[1].removesuffix("_handler")
            for logical_id, resource in template.find_resources("AWS::Lambda::Function").items()
        }
        return {
            operations[resource["Properties"]["FunctionName"]["Ref"]]: resource
            for resource in template.find_resources("AWS::Lambda::Alias").values()
        }

    before, after = aliases_by_operation(disabled), aliases_by_operation(enabled)
    previous_dependencies = set(before["request"].get("DependsOn", []))
    assert previous_dependencies.isdisjoint(monitoring_ids)
    assert set(after["request"].get("DependsOn", [])) == previous_dependencies | monitoring_ids
    for operation in ("search", "indexer", "enhancer"):
        assert after[operation] == before[operation]


def test_request_pc_deployment_requires_exact_monitoring_dependencies():
    text = Path("docs/deploy.md").read_text()
    assert "DependsOn" in text
    assert "先创建监控，再分配 PC" in text
    for name in (
        "request-init-auth-recoveredFilter",
        "request-init-auth-recoveredAlarm",
        "request-init-oidc-prefetch-failedFilter",
        "request-init-oidc-prefetch-failedAlarm",
    ):
        assert name in text


def fixture_config(template):
    environment = functions_by_operation(template)["search"]["Environment"]["Variables"]
    region = environment["SEMSEARCH_CONFIG_KEY"].split("/")[1]
    return DeploymentConfig.from_file(Path("infra/config.example.json"), region=region)


def test_deployment_profile_pins_explicit_gateway_and_model_defaults():
    config = DeploymentConfig.from_file(Path("infra/config.example.json"))
    version = "2025-03-26"
    observed = {
        "MODEL_EMBEDDING_DIMENSIONS": "1024",
        "MODEL_RERANK_PATH": "/rerank",
        "MODEL_RERANK_ENVELOPE": "flat",
        "MODEL_RERANK_SCORE_FIELD": "relevance_score",
    }
    payloads = {name: json.loads(body)["settings"] for name, body in documents(config).items()}
    assert payloads["indexer"]["SEMSEARCH_MCP_VERSIONS"] == version == "2025-03-26"
    for operation in ("search", "enhancer"):
        settings = payloads[operation]
        for name in (
            "MODEL_EMBEDDING_DIMENSIONS",
            "MODEL_RERANK_PATH",
            "MODEL_RERANK_ENVELOPE",
            "MODEL_RERANK_SCORE_FIELD",
        ):
            assert settings[name] == observed[name]
        assert settings["MODEL_EMBEDDING_MAX_BATCH"] == "10"
        assert settings["MODEL_RERANK_ENABLED"] == "false"
        assert settings["SEMSEARCH_MODELS_ENABLED"] == "false"
    # A profile is explicit even while both model and rerank activation remain gated.
    assert config.models_enabled is False


def resolve(value, config, references=None):
    """Resolve only syntactic CloudFormation intrinsics; never contact AWS."""
    refs = {
        "AWS::Partition": "aws-cn",
        "AWS::AccountId": config.account,
        "AWS::Region": config.region,
        "AWS::URLSuffix": "amazonaws.com.cn",
        "ConfigurationBucketName": "semsearch-configuration-fixture",
        **(references or {}),
    }
    if isinstance(value, str):
        return value
    if "Ref" in value:
        return refs.get(value["Ref"], f"ref:{value['Ref']}")
    if "Fn::GetAtt" in value:
        return "attribute:" + ".".join(value["Fn::GetAtt"])
    if "Fn::Join" in value:
        separator, parts = value["Fn::Join"]
        return separator.join(resolve(part, config, refs) for part in parts)
    raise AssertionError(f"Unexpected unresolved permission or environment expression: {value}")


def iam_statements(template):
    """Include inline policies and separately attached policies, including imported roles."""
    document = template.to_json()
    role_names = {
        resource["Properties"]["RoleName"]: logical_id
        for logical_id, resource in document["Resources"].items()
        if resource["Type"] == "AWS::IAM::Role"
    }
    for logical_id, resource in document["Resources"].items():
        props = resource.get("Properties", {})
        if resource["Type"] == "AWS::IAM::Role":
            assert not props.get("ManagedPolicyArns"), "Managed policies bypass the exact ARN audit"
            for policy in props.get("Policies", []):
                for statement in as_list(policy["PolicyDocument"]["Statement"]):
                    yield logical_id, [logical_id], statement
        elif resource["Type"] == "AWS::IAM::Policy":
            assert not props.get("Users") and not props.get("Groups")
            roles = [
                role["Ref"] if isinstance(role, dict) else role_names.get(role, role)
                for role in props.get("Roles", [])
            ]
            assert roles, "Every standalone policy must have an audited role attachment"
            for statement in as_list(props["PolicyDocument"]["Statement"]):
                yield logical_id, roles, statement
        else:
            assert resource["Type"] != "AWS::IAM::ManagedPolicy"


def role_statements(template, operation):
    role = functions_by_operation(template)[operation]["Role"]["Fn::GetAtt"][0]
    return [
        statement
        for _, roles, statement in iam_statements(template)
        if role in roles and statement["Effect"] == "Allow"
    ]


def permits_action(statement, action):
    return any(fnmatch.fnmatchcase(action, pattern) for pattern in as_list(statement["Action"]))


def leading_keys(statement, config):
    conditions = statement["Condition"]
    assert conditions["Null"]["dynamodb:LeadingKeys"] == "false"
    supported = {"ForAllValues:StringEquals", "ForAllValues:StringLike"}
    operators = set(conditions) & supported
    assert len(operators) == 1
    operator = operators.pop()
    return operator, [
        resolve(key, config) for key in as_list(conditions[operator]["dynamodb:LeadingKeys"])
    ]


def test_storage_security(template):
    template.resource_count_is("AWS::S3::Bucket", 1)
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "VersioningConfiguration": {"Status": "Enabled"},
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            },
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        },
    )
    template.has_resource_properties(
        "AWS::S3::BucketPolicy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Deny",
                                "Principal": {"AWS": "*"},
                                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                            }
                        )
                    ]
                )
            }
        },
    )
    bucket = resources(template, "AWS::S3::Bucket")[0]
    assert bucket["DeletionPolicy"] == "Retain"
    assert bucket["UpdateReplacePolicy"] == "Retain"


def table_ids(template):
    tables = template.find_resources("AWS::DynamoDB::Table")
    return {
        resource["Properties"]["TableName"].rsplit("-", 1)[1]: logical_id
        for logical_id, resource in tables.items()
    }


def test_dynamodb_security_and_key_contract(template):
    template.resource_count_is("AWS::DynamoDB::Table", 2)
    ids = table_ids(template)
    assert set(ids) == {"state", "protected"}
    for resource in resources(template, "AWS::DynamoDB::Table"):
        props = resource["Properties"]
        assert props["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"}]
        assert props["AttributeDefinitions"] == [{"AttributeName": "pk", "AttributeType": "S"}]
        assert props["BillingMode"] == "PAY_PER_REQUEST"
        assert props["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
        assert props["SSESpecification"] == {"SSEEnabled": True}
        assert props["DeletionProtectionEnabled"] is True
        assert resource["DeletionPolicy"] == resource["UpdateReplacePolicy"] == "Retain"
        if props["TableName"].endswith("-state"):
            assert props["TimeToLiveSpecification"] == {
                "AttributeName": "expires_at",
                "Enabled": True,
            }
        else:
            assert "TimeToLiveSpecification" not in props


def test_protected_table_binding_is_base_environment_and_output(template):
    ids = table_ids(template)
    for props in functions_by_operation(template).values():
        environment = props["Environment"]["Variables"]
        assert environment["SEMSEARCH_TABLE"] == {"Ref": ids["state"]}
        assert environment["SEMSEARCH_PROTECTED_TABLE"] == {"Ref": ids["protected"]}
    assert template.to_json()["Outputs"]["ProtectedTableName"]["Value"] == {"Ref": ids["protected"]}


def test_functions_and_bounded_schedules(template):
    functions = resources(template, "AWS::Lambda::Function")
    assert len(functions) == 4
    assert {f["Properties"]["Handler"] for f in functions} == {
        f"semantic_service.runtime.{name}_handler"
        for name in ("search", "request", "indexer", "enhancer")
    }
    assert len({json.dumps(f["Properties"]["Role"]) for f in functions}) == 4
    for function in functions:
        props = function["Properties"]
        assert props["Architectures"] == ["x86_64"]
        assert props["Runtime"] == "python3.12"
        assert "TracingConfig" not in props
        if props["Handler"].endswith(("indexer_handler", "enhancer_handler")):
            assert props["ReservedConcurrentExecutions"] == 1
    schedules = resources(template, "AWS::Scheduler::Schedule")
    assert {s["Properties"]["ScheduleExpression"] for s in schedules} == {
        "rate(1 minute)",
        "rate(5 minutes)",
    }
    for schedule in schedules:
        props = schedule["Properties"]
        assert props["FlexibleTimeWindow"] == {"Mode": "OFF"}
        assert props["Target"]["RetryPolicy"] == {
            "MaximumEventAgeInSeconds": 60,
            "MaximumRetryAttempts": 0,
        }
        assert json.loads(props["Target"]["Input"]) == {"time": "<aws.scheduler.scheduled-time>"}
        assert "DeadLetterConfig" not in props["Target"]
    retries = resources(template, "AWS::Lambda::EventInvokeConfig")
    assert len(retries) == 2
    assert all(r["Properties"]["MaximumRetryAttempts"] == 0 for r in retries)
    assert all(r["Properties"]["MaximumEventAgeInSeconds"] == 60 for r in retries)


def test_iam_resources_partition_and_secret_isolation(template):
    document = template.to_json()
    rendered = json.dumps(document)
    assert "arn:" + "aws:" not in rendered
    assert "AWS::Partition" in rendered
    assert "AWS::URLSuffix" in rendered
    template.resource_count_is("AWS::SecretsManager::Secret", 0)
    template.resource_count_is("AWS::Lambda::Url", 0)
    template.resource_count_is("AWS::SQS::Queue", 0)
    template.resource_count_is("AWS::BedrockAgentCore::Gateway", 0)
    config = fixture_config(template)
    scanned = list(iam_statements(template))
    standalone = template.find_resources("AWS::IAM::Policy")
    assert standalone
    assert set(standalone).issubset({logical_id for logical_id, _, _ in scanned})
    for _, _, statement in scanned:
        assert "NotAction" not in statement and "NotResource" not in statement
        actions = as_list(statement["Action"])
        assert not any("*" in action or "?" in action for action in actions)
        for raw_resource in as_list(statement["Resource"]):
            resource = resolve(raw_resource, config)
            assert resource != "*" and "?" not in resource
            if "*" in resource:
                # Immutable object names are the sole explicit resource wildcard:
                # one known Gateway subtree, never a bucket, account or service.
                assert set(actions) <= {"s3:GetObjectVersion", "s3:PutObject"}
                assert resource.count("*") == 1
                assert resource.endswith(
                    ("/versions/*", "/policies/*", "/safety/*", "/enhancement/*")
                )
                assert f"/{config.account}/{config.region}/{config.gateway_id}/" in resource
        if "secretsmanager:GetSecretValue" in actions:
            assert all(
                resolve(resource, config).startswith(
                    f"arn:aws-cn:secretsmanager:{config.region}:{config.account}:secret:"
                )
                for resource in as_list(statement["Resource"])
            )
        if any(action.startswith("dynamodb:") for action in actions):
            state_arn = {"Fn::GetAtt": [table_ids(template)["state"], "Arn"]}
            protected_arn = {"Fn::GetAtt": [table_ids(template)["protected"], "Arn"]}
            assert as_list(statement["Resource"]) in ([state_arn], [protected_arn])
            if as_list(statement["Resource"]) == [state_arn]:
                leading_keys(statement, config)
            else:
                assert "Condition" not in statement
                assert set(actions) <= {"dynamodb:GetItem", "dynamodb:ConditionCheckItem"}
    assert "SynchronizeGatewayTargets" not in rendered
    assert "ListGateways" not in rendered


def test_protected_state_iam_matches_runtime_transaction_operations(template):
    config = fixture_config(template)
    namespace = f"{config.account}/{config.region}/{config.gateway_id}/"
    content = namespace + "CONTROL"
    ids = table_ids(template)
    state_arn = {"Fn::GetAtt": [ids["state"], "Arn"]}
    protected_arn = {"Fn::GetAtt": [ids["protected"], "Arn"]}
    for operation in ("search", "request", "indexer", "enhancer"):
        statements = [
            statement
            for statement in role_statements(template, operation)
            if any(action.startswith("dynamodb:") for action in as_list(statement["Action"]))
        ]
        protected = [
            statement
            for statement in statements
            if as_list(statement["Resource"]) == [protected_arn]
        ]
        assert protected
        actions = {action for statement in protected for action in as_list(statement["Action"])}
        expected = {"dynamodb:GetItem"}
        if operation in ("indexer", "enhancer"):
            expected.add("dynamodb:ConditionCheckItem")
        assert actions == expected
        assert all("Condition" not in statement for statement in protected)
        state = [
            statement for statement in statements if as_list(statement["Resource"]) == [state_arn]
        ]
        assert len(state) + len(protected) == len(statements)
        assert any(
            permits_action(statement, "dynamodb:GetItem")
            and leading_keys(statement, config) == ("ForAllValues:StringEquals", [content])
            for statement in state
        )
        if operation in ("indexer", "enhancer"):
            assert any(
                permits_action(statement, "dynamodb:PutItem")
                and leading_keys(statement, config) == ("ForAllValues:StringEquals", [content])
                for statement in state
            )
        for statement in state:
            assert "PROTECTED" not in json.dumps(statement.get("Condition", {}))
            assert not permits_action(statement, "dynamodb:ConditionCheckItem")


def test_no_principal_in_stack_can_write_the_protected_table(template):
    protected_arn = {"Fn::GetAtt": [table_ids(template)["protected"], "Arn"]}
    runtime_roles = {
        props["Role"]["Fn::GetAtt"][0] for props in functions_by_operation(template).values()
    }
    matches = 0
    for _, roles, statement in iam_statements(template):
        if protected_arn in as_list(statement["Resource"]):
            matches += 1
            assert statement["Effect"] == "Allow"
            assert as_list(statement["Resource"]) == [protected_arn]
            assert set(roles) <= runtime_roles
            # An allowlist also excludes transactional, batch and PartiQL writes.
            assert set(as_list(statement["Action"])) <= {
                "dynamodb:GetItem",
                "dynamodb:ConditionCheckItem",
            }
            assert "Condition" not in statement
    assert matches >= 4


def test_configuration_read_is_exact_object_and_exact_version_per_role(template):
    config = fixture_config(template)
    expected_documents = documents(config)
    all_keys = {
        props["Environment"]["Variables"]["SEMSEARCH_CONFIG_KEY"]
        for props in functions_by_operation(template).values()
    }
    assert len(all_keys) == 4
    for operation, props in functions_by_operation(template).items():
        env = props["Environment"]["Variables"]
        key = env["SEMSEARCH_CONFIG_KEY"]
        checksum = hashlib.sha256(expected_documents[operation]).hexdigest()
        assert key.endswith(f"/configuration/{operation}/{checksum}.json")
        assert env["SEMSEARCH_CONFIG_SHA256"] == checksum
        assert int(env["SEMSEARCH_CONFIG_BYTES"]) == len(expected_documents[operation])
        expected_arn = f"arn:aws-cn:s3:::semsearch-configuration-fixture/{key}"
        matches = []
        for statement in role_statements(template, operation):
            resources_ = [resolve(value, config) for value in as_list(statement["Resource"])]
            if permits_action(statement, "s3:GetObjectVersion") and any(
                fnmatch.fnmatchcase(expected_arn, resource) for resource in resources_
            ):
                matches.append(statement)
                assert resources_ == [expected_arn]
                assert statement["Condition"]["StringEquals"] == {
                    "s3:VersionId": env["SEMSEARCH_CONFIG_VERSION_ID"]
                }
            if any(
                permits_action(statement, action)
                for action in ("s3:GetObject", "s3:PutObject", "s3:DeleteObject")
            ):
                assert not any(
                    fnmatch.fnmatchcase(expected_arn, resource) for resource in resources_
                )
            assert not any(
                other != key and resource.endswith("/" + other)
                for other in all_keys
                for resource in resources_
            )
        assert len(matches) == 1


def assert_secret_permissions(template, config, *, models_enabled):
    expected = {
        "search": {"signing", "model"} if models_enabled else {"signing"},
        "request": {"signing"},
        "indexer": {"oauth"},
        "enhancer": {"model"} if models_enabled else set(),
    }
    arns = {
        kind: (
            f"arn:aws-cn:secretsmanager:{config.region}:{config.account}:"
            f"secret:{reference.name}-{reference.suffix}"
        )
        for kind, reference in config.secrets.items()
    }
    for operation, allowed in expected.items():
        statements = role_statements(template, operation)
        secrets = {
            resolve(resource, config)
            for statement in statements
            if permits_action(statement, "secretsmanager:GetSecretValue")
            for resource in as_list(statement["Resource"])
        }
        assert secrets == {arns[kind] for kind in allowed}
        decrypts = [
            statement for statement in statements if permits_action(statement, "kms:Decrypt")
        ]
        expected_kinds = {kind for kind in allowed if config.secrets[kind].kms_key_id is not None}
        assert len(decrypts) == len(expected_kinds)
        for kind in expected_kinds:
            expected_key = (
                f"arn:aws-cn:kms:{config.region}:{config.account}:"
                f"key/{config.secrets[kind].kms_key_id}"
            )
            matching = [
                statement
                for statement in decrypts
                if [resolve(value, config) for value in as_list(statement["Resource"])]
                == [expected_key]
            ]
            assert len(matching) == 1
            conditions = matching[0]["Condition"]["StringEquals"]
            assert set(conditions) == {"kms:ViaService", "kms:EncryptionContext:SecretARN"}
            assert resolve(conditions["kms:ViaService"], config) == (
                f"secretsmanager.{config.region}.amazonaws.com.cn"
            )
            assert resolve(conditions["kms:EncryptionContext:SecretARN"], config) == arns[kind]


def test_secret_role_separation_with_models_disabled(template):
    assert_secret_permissions(template, fixture_config(template), models_enabled=False)


def test_secret_role_separation_with_models_and_customer_kms(template, tmp_path):
    config = fixture_config(template)
    config = replace(
        config,
        models_enabled=True,
        model_settings={
            "MODEL_ALLOWED_HOSTS": "models.example.invalid",
            "MODEL_EMBEDDING_DIMENSIONS": "4",
        },
        secrets={
            kind: replace(reference, kms_key_id=f"00000000-0000-4000-8000-{index:012d}")
            for index, (kind, reference) in enumerate(config.secrets.items(), start=1)
        },
    )
    enabled = synthesize(config, tmp_path / "models-enabled")
    assert_secret_permissions(enabled, config, models_enabled=True)


def maximum_environment_size(template, config, environment):
    document = template.to_json()
    references = {"ConfigurationBucketName": "b" * 63}
    for name, parameter in document["Parameters"].items():
        if name.endswith("ConfigVersionId"):
            assert parameter["MinLength"] == 1
            assert parameter["MaxLength"] <= 1024
            references[name] = "v" * parameter["MaxLength"]
    for name, resource in document["Resources"].items():
        if resource["Type"] == "AWS::S3::Bucket":
            references[name] = "b" * 63
        if resource["Type"] == "AWS::DynamoDB::Table":
            references[name] = resource["Properties"]["TableName"]
    resolved = {key: resolve(value, config, references) for key, value in environment.items()}
    assert not any("ref:" in value for value in resolved.values())
    return sum(len(key.encode()) + len(value.encode()) for key, value in resolved.items())


def test_five_hundred_tools_stay_outside_the_bounded_lambda_environment(template, tmp_path):
    config = fixture_config(template)
    config = replace(
        config,
        registry=config.registry
        + [
            {
                "name": f"business{index:03d}___action",
                "target_id": f"target-{index:03d}",
                "incarnation": "v1",
                "role": "business",
            }
            for index in range(499)
        ],
    )
    payloads = documents(config)
    registry = json.loads(json.loads(payloads["indexer"])["settings"]["SEMSEARCH_REGISTRY_JSON"])
    assert len(registry) == 500
    assert len(payloads["indexer"]) > 4096
    expanded = synthesize(config, tmp_path / "large-registry")
    for operation, props in functions_by_operation(expanded).items():
        environment = props["Environment"]["Variables"]
        assert not CONFIGURATION_FIELDS.intersection(environment)
        assert "business498" not in json.dumps(environment)
        assert maximum_environment_size(expanded, config, environment) <= 4096
        assert int(environment["SEMSEARCH_CONFIG_BYTES"]) == len(payloads[operation])


def test_reject_configuration_whose_maximum_environment_exceeds_four_kib(template, tmp_path):
    config = fixture_config(template)
    tool_name = "discovery" + "x" * 230 + "___search_tools"
    target_id, incarnation = "t" * 100, "i" * 100
    config = replace(
        config,
        gateway_id="g" * 100,
        search_target_id=target_id,
        search_incarnation=incarnation,
        search_descriptor=config.search_descriptor | {"name": tool_name},
        registry=[
            {
                "name": tool_name,
                "target_id": target_id,
                "incarnation": incarnation,
                "role": "discovery",
            }
        ],
        secrets={
            kind: replace(reference, name=(f"semsearch/{kind}/" + "x" * 512)[:512])
            for kind, reference in config.secrets.items()
        },
    )
    with pytest.raises(ValueError, match="4 KiB"):
        synthesize(config, tmp_path / "oversized-environment")


@pytest.mark.parametrize(
    ("category", "key"),
    [
        ("oidc", "MODEL_SECRET_ID"),
        ("oidc", "SEMSEARCH_GATEWAY_ID"),
        ("oidc", "OIDC_UNKNOWN_SETTING"),
        ("model_settings", "OIDC_ISSUER"),
        ("model_settings", "SEMSEARCH_SIGNING_SECRET_ID"),
        ("model_settings", "MODEL_SECRET_ID"),
        ("model_settings", "SEMSEARCH_CONFIG_BUCKET"),
        ("service_settings", "MODEL_ALLOWED_HOSTS"),
        ("service_settings", "SEMSEARCH_CONFIG_VERSION_ID"),
        ("service_settings", "SEMSEARCH_BUCKET"),
    ],
)
def test_categorized_configuration_rejects_cross_category_injection(category, key):
    config = DeploymentConfig.from_file(Path("infra/config.example.json"))
    with pytest.raises(ValueError):
        candidate = replace(config, **{category: getattr(config, category) | {key: "injected"}})
        documents(candidate)


@pytest.mark.parametrize(
    "principals",
    [
        [],
        ["ab"],
        [[1, 2]],
        [["issuer", ""]],
        [["issuer"]],
        [["issuer", "subject", "extra"]],
        [("issuer", "subject")],
    ],
)
def test_machine_principals_require_explicit_two_string_json_arrays(principals):
    config = DeploymentConfig.from_file(Path("infra/config.example.json"))
    with pytest.raises(ValueError, match="issuer/subject"):
        replace(config, machine_principals=principals)


def test_runtime_signing_defaults_cover_complete_rotation_overlap(monkeypatch):
    class NoSecretReads:
        def get_secret_value(self, **kwargs):
            raise AssertionError("Constructor verification must not read any secret")

    cache = SecretsCache(NoSecretReads())
    constructor = runtime.SigningSecretsKeys
    observed = {}

    def capture_settings(*args, **kwargs):
        bound = inspect.signature(constructor).bind(*args, **kwargs)
        bound.apply_defaults()
        observed.update(bound.arguments)
        return constructor(*args, **kwargs)

    monkeypatch.setattr(runtime, "_configuration", lambda operation: {})
    monkeypatch.setattr(runtime, "_secrets", lambda: cache)
    monkeypatch.setattr(runtime, "SigningSecretsKeys", capture_settings)
    monkeypatch.setenv("SEMSEARCH_SIGNING_SECRET_ID", "synthetic-signing-secret")
    runtime._envelope.cache_clear()
    try:
        codec = runtime._envelope()
    finally:
        runtime._envelope.cache_clear()
    assert (
        observed["previous_window_seconds"],
        cache.ttl_seconds,
        codec.ttl,
        codec.clock_skew,
        observed["safety"],
    ) == (390, 300, 30, 30, 30)
    assert observed["envelope_ttl"] == codec.ttl
    assert observed["skew"] == codec.clock_skew
    assert observed["previous_window_seconds"] >= (
        cache.ttl_seconds + codec.ttl + codec.clock_skew + observed["safety"]
    )


def test_logs_alarms_and_resource_tags(template):
    groups = resources(template, "AWS::Logs::LogGroup")
    assert len(groups) == 4
    assert all(g["Properties"]["RetentionInDays"] == 30 for g in groups)
    assert len(resources(template, "AWS::CloudWatch::Alarm")) >= 6
    assert len(resources(template, "AWS::Logs::MetricFilter")) >= 4
    taggable = {
        "AWS::S3::Bucket",
        "AWS::DynamoDB::Table",
        "AWS::Lambda::Function",
        "AWS::IAM::Role",
        "AWS::Logs::LogGroup",
        "AWS::Scheduler::ScheduleGroup",
        "AWS::CloudWatch::Alarm",
    }
    for resource in template.to_json()["Resources"].values():
        if resource["Type"] in taggable:
            assert {"Key": "project", "Value": "semsearch"} in resource["Properties"]["Tags"]

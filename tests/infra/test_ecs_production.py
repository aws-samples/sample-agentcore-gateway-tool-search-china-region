"""Production Search topology is synthesized offline, without credential lookups."""

import json
import zipfile
from dataclasses import replace
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from infra.config import DeploymentConfig, EcsSearchConfig, SecretReference
from infra.stack import SemsearchStack, documents


def configuration(region="cn-north-1"):
    base = DeploymentConfig.from_file(Path("infra/config.example.json"), region=region)
    return replace(
        base,
        alarm_topic_arns=(f"arn:aws-cn:sns:{region}:{base.account}:operations",),
        ecs_search=EcsSearchConfig(
            image_uri=f"{base.account}.dkr.ecr.{region}.amazonaws.com.cn/search@sha256:" + "a" * 64,
            hostname="search.example.invalid",
            certificate_arn=f"arn:aws-cn:acm:{region}:{base.account}:certificate/" + "a" * 36,
            availability_zones=(region + "a", region + "b"),
            oauth_secret=SecretReference("semsearch/ecs-oauth", "AbCd04"),
            oauth_subject="fixture-ecs-client",
            oauth_scope="search.invoke",
        ),
    )


def synth(config, directory, *, ecs_asset_path=None, omit_ecs_asset=False, return_assembly=False):
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "lambda.zip"
    with zipfile.ZipFile(archive, "w") as package:
        for module in (
            "__init__",
            "runtime",
            "search",
            "indexer",
            "enhancer",
            "facade",
            "facade_lambda",
            "fallback",
        ):
            package.writestr(zipfile.ZipInfo(f"semantic_service/{module}.py"), "# fixture\n")
        package.writestr(zipfile.ZipInfo("semantic_core/__init__.py"), "# fixture\n")
    if config.ecs_search is not None and ecs_asset_path is None and not omit_ecs_asset:
        ecs_asset_path = directory / "ecs.zip"
        ecs_asset_path.write_bytes(archive.read_bytes())
    app = cdk.App(outdir=str(directory / "cdk.out"))
    stack = SemsearchStack(
        app,
        "fixture",
        config=config,
        asset_path=archive,
        ecs_asset_path=ecs_asset_path,
        env=cdk.Environment(account=config.account, region=config.region),
    )
    if return_assembly:
        return app.synth()
    return Template.from_stack(stack).to_json()


def resources(document, kind):
    return {key: value for key, value in document["Resources"].items() if value["Type"] == kind}


def props(document, kind):
    return [item["Properties"] for item in resources(document, kind).values()]


def statements(document, role):
    result = []
    for policy in document["Resources"][role]["Properties"].get("Policies", []):
        result.extend(policy["PolicyDocument"]["Statement"])
    for policy in props(document, "AWS::IAM::Policy"):
        if {"Ref": role} in policy.get("Roles", []):
            result.extend(policy["PolicyDocument"]["Statement"])
    return result


def listify(value):
    return value if isinstance(value, list) else [value]


@pytest.fixture(params=["cn-north-1", "cn-northwest-1"])
def enabled(request, tmp_path):
    config = configuration(request.param)
    return config, synth(config, tmp_path / "enabled")


def test_default_is_four_lambdas_and_request_is_unchanged(tmp_path):
    config = configuration()
    baseline = replace(config, ecs_search=None)
    before = synth(baseline, tmp_path / "before")
    after = synth(config, tmp_path / "after")
    assert len(props(before, "AWS::Lambda::Function")) == 4
    assert props(before, "AWS::ECS::Service") == []
    before_request = {
        k: v
        for k, v in before["Resources"].items()
        if k.startswith(("Functionsrequest", "request"))
    }
    after_request = {
        k: v for k, v in after["Resources"].items() if k.startswith(("Functionsrequest", "request"))
    }
    assert before_request == after_request
    assert documents(baseline) == documents(config)


def test_ecs_task_is_bounded_nonroot_readonly_and_uses_immutable_image(enabled):
    config, doc = enabled
    task = props(doc, "AWS::ECS::TaskDefinition")[0]
    assert task["Cpu"] == "2048" and task["Memory"] == "4096"
    assert task["RequiresCompatibilities"] == ["FARGATE"]
    assert task["NetworkMode"] == "awsvpc"
    assert task["ExecutionRoleArn"] != task["TaskRoleArn"]
    container = task["ContainerDefinitions"][0]
    assert container["Image"] == config.ecs_search.image_uri
    assert container["ReadonlyRootFilesystem"] is True
    assert container["User"] == "10001:10001"
    assert container["StopTimeout"] == 30
    assert "Command" not in container
    assert container["HealthCheck"]["Command"][-1] == "semantic_service.ecs_health"
    assert container["MountPoints"] == [
        {"ContainerPath": "/tmp", "ReadOnly": False, "SourceVolume": "tmp"}  # nosec B108
    ]
    env = {item["Name"]: item["Value"] for item in container["Environment"]}
    assert "SEMSEARCH_BACKEND" not in env
    assert env["SEMSEARCH_ECS_WORKERS"] == "4"
    assert env["SEMSEARCH_ECS_OAUTH_SUBJECT"] == config.ecs_search.oauth_subject
    assert "SEMSEARCH_ECS_OAUTH_SECRET_ID" not in env


def test_service_network_scale_and_tls(enabled):
    config, doc = enabled
    service = props(doc, "AWS::ECS::Service")[0]
    assert service["DesiredCount"] == 2
    assert service["NetworkConfiguration"]["AwsvpcConfiguration"]["AssignPublicIp"] == "DISABLED"
    assert len(service["NetworkConfiguration"]["AwsvpcConfiguration"]["Subnets"]) == 2
    assert service["DeploymentConfiguration"]["MinimumHealthyPercent"] == 100
    assert service["DeploymentConfiguration"]["MaximumPercent"] == 200
    assert service["DeploymentConfiguration"]["DeploymentCircuitBreaker"] == {
        "Enable": True,
        "Rollback": True,
    }
    assert len(props(doc, "AWS::EC2::NatGateway")) == 2
    nlb = props(doc, "AWS::ElasticLoadBalancingV2::LoadBalancer")[0]
    assert nlb["Type"] == "network" and nlb["Scheme"] == "internal"
    assert len(nlb["SecurityGroups"]) == 1
    listener = props(doc, "AWS::ElasticLoadBalancingV2::Listener")[0]
    assert listener["Protocol"] == "TLS" and listener["Port"] == 443
    assert listener["Certificates"] == [{"CertificateArn": config.ecs_search.certificate_arn}]
    target = props(doc, "AWS::ElasticLoadBalancingV2::TargetGroup")[0]
    assert target["Protocol"] == "TLS" and target["Port"] == 8443
    assert target["HealthCheckProtocol"] == "HTTPS" and target["HealthCheckPath"] == "/ready"
    attrs = {item["Key"]: item["Value"] for item in target["TargetGroupAttributes"]}
    assert attrs["deregistration_delay.timeout_seconds"] == "30"
    scalable = props(doc, "AWS::ApplicationAutoScaling::ScalableTarget")[0]
    assert scalable["MinCapacity"] == 2 and scalable["MaxCapacity"] == 4
    tracking = props(doc, "AWS::ApplicationAutoScaling::ScalingPolicy")[0][
        "TargetTrackingScalingPolicyConfiguration"
    ]
    assert tracking["TargetValue"] == 60
    assert (
        tracking["PredefinedMetricSpecification"]["PredefinedMetricType"]
        == "ECSServiceAverageCPUUtilization"
    )
    assert not props(doc, "AWS::SecretsManager::Secret")
    assert not props(doc, "AWS::CertificateManager::Certificate")


def test_task_accepts_only_nlb_and_egress_is_https(enabled):
    _, doc = enabled
    ingress = props(doc, "AWS::EC2::SecurityGroupIngress")
    backend = [rule for rule in ingress if rule.get("FromPort") == 8443]
    assert len(backend) == 1 and "SourceSecurityGroupId" in backend[0]
    assert "CidrIp" not in backend[0]
    for group in props(doc, "AWS::EC2::SecurityGroup"):
        for rule in group.get("SecurityGroupEgress", []):
            assert rule.get("IpProtocol") != "-1"
            if rule.get("CidrIp") == "0.0.0.0/0":
                assert rule["FromPort"] == rule["ToPort"] == 443


def test_gateway_routes_only_to_facade_and_request_and_facade_has_no_data_access(enabled):
    _, doc = enabled
    functions = props(doc, "AWS::Lambda::Function")
    facade = next(f for f in functions if f["Handler"] == "semantic_service.facade_lambda.handler")
    fallback = next(f for f in functions if f["Handler"] == "semantic_service.fallback.handler")
    assert len(functions) == 6
    env = facade["Environment"]["Variables"]
    assert env["SEMSEARCH_ECS_URL"] == "https://search.example.invalid/search"
    assert "SEMSEARCH_ROUTE_TOTAL_SECONDS" not in env
    assert "SEMSEARCH_ROUTE_ECS_SECONDS" not in env
    assert "SEMSEARCH_SIGNING_SECRET_ID" not in env
    role = facade["Role"]["Fn::GetAtt"][0]
    policy = statements(doc, role)
    actions = {
        action for stmt in policy if stmt["Effect"] == "Allow" for action in listify(stmt["Action"])
    }
    assert actions == {
        "lambda:InvokeFunction",
        "secretsmanager:GetSecretValue",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "ec2:CreateNetworkInterface",
        "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeSubnets",
        "ec2:DeleteNetworkInterface",
        "ec2:AssignPrivateIpAddresses",
        "ec2:UnassignPrivateIpAddresses",
    }
    invoke = next(stmt for stmt in policy if "lambda:InvokeFunction" in listify(stmt["Action"]))
    assert listify(invoke["Resource"]) == [env["SEMSEARCH_FALLBACK_ALIAS_ARN"]]
    gateway = next(p for k, p in resources(doc, "AWS::IAM::Policy").items() if "GatewayInvoke" in k)
    gateway_refs = json.dumps(gateway["Properties"]["PolicyDocument"])
    assert "facadeAlias" in gateway_refs and "requestAlias" in gateway_refs
    assert "searchAlias" in gateway_refs  # retained until approved cutover is verified
    assert "SEMSEARCH_BACKEND" not in fallback["Environment"]["Variables"]


def test_application_roles_share_read_permissions_but_replay_namespaces_are_separate(enabled):
    _, doc = enabled
    task = props(doc, "AWS::ECS::TaskDefinition")[0]
    task_role = task["TaskRoleArn"]["Fn::GetAtt"][0]
    ecs_statements = statements(doc, task_role)
    fallback = next(
        f
        for f in props(doc, "AWS::Lambda::Function")
        if f["Handler"] == "semantic_service.fallback.handler"
    )
    fallback_statements = statements(doc, fallback["Role"]["Fn::GetAtt"][0])
    replay = [t for t in props(doc, "AWS::DynamoDB::Table") if t["TableName"].endswith("-replay")][
        0
    ]
    assert replay["DeletionProtectionEnabled"] is True
    assert replay["TimeToLiveSpecification"] == {"AttributeName": "expires_at", "Enabled": True}
    for name, policies in (("ecs", ecs_statements), ("fallback", fallback_statements)):
        writes = [p for p in policies if "dynamodb:PutItem" in listify(p["Action"])]
        assert len(writes) == 1
        assert writes[0]["Condition"] == {
            "ForAllValues:StringLike": {"dynamodb:LeadingKeys": [name + "/*"]},
            "Null": {"dynamodb:LeadingKeys": "false"},
        }
        for stmt in policies:
            assert not set(listify(stmt["Action"])) & {
                "s3:PutObject",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "secretsmanager:PutSecretValue",
            }
    ecs_reads = [
        p
        for p in ecs_statements
        if listify(p["Action"])[0] not in ("dynamodb:PutItem", "logs:CreateLogStream")
    ]
    fallback_reads = [
        p
        for p in fallback_statements
        if listify(p["Action"])[0] not in ("dynamodb:PutItem", "logs:CreateLogStream")
    ]
    assert ecs_reads == fallback_reads


def test_ecs_alarms_and_route_metrics_are_present(enabled):
    _, doc = enabled
    alarms = props(doc, "AWS::CloudWatch::Alarm")
    names = {alarm["AlarmName"].rsplit("-", 1)[0] for alarm in alarms}
    assert names
    metrics = {alarm.get("MetricName") for alarm in alarms}
    assert {
        "UnHealthyHostCount",
        "CPUUtilization",
        "MemoryUtilization",
        "RunningTaskCount",
    } <= metrics
    filters = props(doc, "AWS::Logs::MetricFilter")
    metric_names = {m["MetricTransformations"][0]["MetricName"] for m in filters}
    assert {
        "route-fallback",
        "ecs-auth-failed",
        "ecs-replay-failed",
        "ecs-backend-errors",
    } <= metric_names
    assert any(
        alarm.get("MetricName") == "RunningTaskCount" and alarm["TreatMissingData"] == "breaching"
        for alarm in alarms
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("image_uri", "111122223333.dkr.ecr.cn-north-1.amazonaws.com.cn/search:latest"),
        ("image_uri", "444455556666.dkr.ecr.cn-north-1.amazonaws.com.cn/search@sha256:" + "a" * 64),
        ("certificate_arn", "arn:aws:acm:us-east-1:111122223333:certificate/fixture"),
        ("hostname", "https://search.example.invalid"),
        ("availability_zones", ("cn-north-1a", "cn-north-1a")),
        ("oauth_secret", SecretReference("semsearch/indexer-oauth", "AbCd03")),
        ("oauth_subject", ""),
        ("oauth_scope", ""),
    ],
)
def test_ecs_configuration_rejects_mutable_or_unbounded_inputs(field, value):
    config = configuration()
    with pytest.raises(ValueError):
        replace(config, ecs_search=replace(config.ecs_search, **{field: value}))


def test_ecs_configuration_roundtrips_explicit_public_file(tmp_path):
    config = configuration()
    raw = json.loads(Path("infra/config.example.json").read_text())
    raw["region"] = config.region
    raw["alarm_topic_arns"] = list(config.alarm_topic_arns)
    raw["ecs_search"] = {
        "image_uri": config.ecs_search.image_uri,
        "hostname": config.ecs_search.hostname,
        "certificate_arn": config.ecs_search.certificate_arn,
        "availability_zones": list(config.ecs_search.availability_zones),
        "oauth_secret": {"name": "semsearch/ecs-oauth", "suffix": "AbCd04"},
        "oauth_subject": "fixture-ecs-client",
        "oauth_scope": "search.invoke",
    }
    path = tmp_path / "configuration.json"
    path.write_text(json.dumps(raw))
    assert DeploymentConfig.from_file(path).ecs_search == config.ecs_search


def test_enabled_stack_rejects_asset_without_facade_and_fallback(tmp_path):
    from infra.functions import Functions

    archive = tmp_path / "lambda.zip"
    with zipfile.ZipFile(archive, "w") as package:
        for module in ("__init__", "runtime", "search", "indexer", "enhancer"):
            package.writestr(f"semantic_service/{module}.py", "# fixture\n")
        package.writestr("semantic_core/__init__.py", "# fixture\n")
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    stack = cdk.Stack(app, "fixture")
    with pytest.raises(ValueError, match="facade|fallback"):
        Functions(
            stack,
            "Functions",
            prefix="fixture",
            asset_path=archive,
            ecs_asset_path=archive,
            environments={
                name: {} for name in ("search", "request", "indexer", "enhancer", "facade")
            },
            permissions={
                name: [] for name in ("search", "request", "indexer", "enhancer", "facade")
            },
            ecs_enabled=True,
        )


def test_ecs_telemetry_keeps_actionable_search_fields_and_tls_alarms(enabled):
    _, doc = enabled
    metrics = {alarm.get("MetricName") for alarm in props(doc, "AWS::CloudWatch::Alarm")}
    assert {"ClientTLSNegotiationErrorCount", "TargetTLSNegotiationErrorCount"} <= metrics
    groups = resources(doc, "AWS::Logs::LogGroup")
    ecs_group = next(
        key
        for key, value in groups.items()
        if value["Properties"]["LogGroupName"].endswith("-ecs-search-logs")
    )
    filters = [
        item
        for item in props(doc, "AWS::Logs::MetricFilter")
        if item["LogGroupName"] == {"Ref": ecs_group}
    ]
    names = {item["MetricTransformations"][0]["MetricName"] for item in filters}
    assert {
        "search-count",
        "search-internal-errors",
        "search-degraded",
        "vector-failed",
    } <= names
    assert "retrieval-vector_bm25_fallback" not in names
    assert "embedding-error-timeout" not in names


def test_dedicated_oauth_kms_permission_is_exact_and_only_facade(tmp_path):
    config = configuration()
    key_id = "12345678-1234-1234-1234-123456789abc"
    config = replace(
        config,
        ecs_search=replace(
            config.ecs_search,
            oauth_secret=replace(config.ecs_search.oauth_secret, kms_key_id=key_id),
        ),
    )
    doc = synth(config, tmp_path)
    facade = next(
        f
        for f in props(doc, "AWS::Lambda::Function")
        if f["Handler"] == "semantic_service.facade_lambda.handler"
    )
    role = facade["Role"]["Fn::GetAtt"][0]
    kms = [item for item in statements(doc, role) if "kms:Decrypt" in listify(item["Action"])]
    assert len(kms) == 1
    assert (
        kms[0]["Condition"]["StringEquals"]["kms:EncryptionContext:SecretARN"]
        == facade["Environment"]["Variables"]["SEMSEARCH_ECS_OAUTH_SECRET_ID"]
    )
    assert key_id in json.dumps(kms[0]["Resource"])


def test_backend_metric_filters_match_runtime_status_contract(enabled):
    _, doc = enabled
    filters = {
        item["MetricTransformations"][0]["MetricName"]: item["FilterPattern"]
        for item in props(doc, "AWS::Logs::MetricFilter")
    }
    assert (
        filters["ecs-auth-failed"]
        == '{ ($.operation = "search_backend" || $.operation = "search_ingress") && '
        '$.phase = "end" && $.status = "unauthenticated" }'
    )
    assert (
        filters["ecs-replay-failed"] == '{ $.operation = "search_backend" && '
        '($.status = "replay rejected" || $.status = "replay unavailable") }'
    )
    assert (
        filters["ecs-backend-errors"]
        == '{ ($.operation = "search_backend" || $.operation = "search_server" || '
        '$.operation = "search_ingress") && $.phase = "end" && '
        '($.status = "internal failure" || $.status = "index not ready" || '
        '$.status = "deadline exceeded" || $.status = "clock skew") }'
    )


def test_container_entrypoint_and_nonroot_temporary_volume_are_deployable(enabled):
    _, doc = enabled
    task = props(doc, "AWS::ECS::TaskDefinition")[0]
    containers = {item["Name"]: item for item in task["ContainerDefinitions"]}
    app = containers["search"]
    assert "Command" not in app, "image ENTRYPOINT already starts the server"
    initializer = containers["initialize-tmp"]
    assert initializer["User"] == "0:0" and initializer["Essential"] is False
    assert initializer["ReadonlyRootFilesystem"] is True
    assert initializer["EntryPoint"] == ["python", "-c"]
    assert "os.chown" in initializer["Command"][0] and "10001" in initializer["Command"][0]
    assert "0o700" in initializer["Command"][0]
    assert app["DependsOn"] == [{"Condition": "SUCCESS", "ContainerName": "initialize-tmp"}]
    assert initializer["MountPoints"] == [
        {"ContainerPath": "/runtime-tmp", "ReadOnly": False, "SourceVolume": "tmp"}
    ]


def test_first_deployment_preserves_legacy_search_for_atomic_external_target_cutover(tmp_path):
    config = configuration()
    before = synth(replace(config, ecs_search=None), tmp_path / "before")
    after = synth(config, tmp_path / "after")
    legacy_before = {
        k: v for k, v in before["Resources"].items() if k.startswith("Functionssearch")
    }
    legacy_after = {k: v for k, v in after["Resources"].items() if k.startswith("Functionssearch")}
    assert legacy_before == legacy_after
    assert len(props(after, "AWS::Lambda::Function")) == 6
    facade = next(
        f
        for f in props(after, "AWS::Lambda::Function")
        if f["Handler"] == "semantic_service.facade_lambda.handler"
    )
    assert "-fallback:live" in json.dumps(
        facade["Environment"]["Variables"]["SEMSEARCH_FALLBACK_ALIAS_ARN"]
    )
    assert "FallbackAliasArn" in after["Outputs"]


def test_actual_replay_keys_match_synthesized_iam_boundaries(enabled):
    import base64
    import fnmatch
    from types import SimpleNamespace

    from semantic_service.ecs_backend import DDBReplayStore

    _, doc = enabled
    calls = []
    store = DDBReplayStore(
        SimpleNamespace(put_item=lambda **kwargs: calls.append(kwargs) or {}), "fixture-replay"
    )
    encoded = base64.urlsafe_b64encode(b"{}").decode().rstrip("=")
    for backend in ("ecs", "fallback"):
        store.consume(f"{encoded}.{encoded}.fixture", backend)
        key = calls[-1]["Item"]["pk"]["S"]
        if backend == "ecs":
            role = props(doc, "AWS::ECS::TaskDefinition")[0]["TaskRoleArn"]["Fn::GetAtt"][0]
        else:
            function = next(
                f
                for f in props(doc, "AWS::Lambda::Function")
                if f["Handler"] == "semantic_service.fallback.handler"
            )
            role = function["Role"]["Fn::GetAtt"][0]
        policy = next(
            p for p in statements(doc, role) if "dynamodb:PutItem" in listify(p["Action"])
        )
        allowed = policy["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]
        assert len(allowed) == 1 and fnmatch.fnmatchcase(key, allowed[0])
        assert not fnmatch.fnmatchcase(key.replace(backend, "other", 1), allowed[0])
        assert calls[-1]["ConditionExpression"] == "attribute_not_exists(pk) OR expires_at < :now"


def test_ecs_deployment_requires_explicit_separate_asset_selection(tmp_path):
    with pytest.raises(ValueError, match="ecs_asset"):
        synth(configuration(), tmp_path, omit_ecs_asset=True)


def test_ecs_asset_changes_only_new_backend_functions(tmp_path):
    config = configuration()
    before = synth(config, tmp_path / "before")
    changed = tmp_path / "changed.zip"
    with zipfile.ZipFile(changed, "w") as archive:
        for module in (
            "__init__",
            "runtime",
            "search",
            "indexer",
            "enhancer",
            "facade",
            "facade_lambda",
            "fallback",
        ):
            archive.writestr(f"semantic_service/{module}.py", "# changed ECS fixture\n")
        archive.writestr("semantic_core/__init__.py", "# changed ECS fixture\n")
    after = synth(config, tmp_path / "after", ecs_asset_path=changed)
    for operation in ("search", "request", "indexer", "enhancer"):
        old = {
            k: v for k, v in before["Resources"].items() if k.startswith("Functions" + operation)
        }
        new = {k: v for k, v in after["Resources"].items() if k.startswith("Functions" + operation)}
        assert old == new
    for operation in ("facade", "fallback"):
        old = next(
            f
            for f in props(before, "AWS::Lambda::Function")
            if f["FunctionName"] == config.prefix + "-" + operation
        )
        new = next(
            f
            for f in props(after, "AWS::Lambda::Function")
            if f["FunctionName"] == config.prefix + "-" + operation
        )
        assert old["Code"] != new["Code"]


def test_fallback_metrics_use_wrapper_logs_and_cover_actionable_failures(enabled):
    _, doc = enabled
    filters = {
        item["FilterName"]: item
        for item in props(doc, "AWS::Logs::MetricFilter")
        if "FilterName" in item
    }
    count = next(value for name, value in filters.items() if name.endswith("-fallback-count"))
    assert count["FilterPattern"] == '{ $.operation = "search_fallback" && $.duration_ms = * }'
    assert any(name.endswith("-vector-failed-fallback") for name in filters)
    assert not any(name.endswith("-embedding-error-timeout-fallback") for name in filters)
    assert any(name.endswith("-search-degraded-fallback") for name in filters)


def test_app_requires_explicit_ecs_asset_context_before_staging():
    from infra.assets import ecs_asset_from_context

    assert ecs_asset_from_context(False, None) is None
    assert ecs_asset_from_context(True, "new-search.zip") == Path("new-search.zip")
    for enabled, value in ((True, None), (False, "new.zip"), (True, " padded.zip"), (True, True)):
        with pytest.raises(ValueError, match="ecs_asset"):
            ecs_asset_from_context(enabled, value)


@pytest.mark.parametrize("region", ["cn-north-1", "cn-northwest-1"])
def test_explicit_ecs_zones_leave_no_cloud_assembly_lookup_requests(region, tmp_path):
    config = configuration(region)
    assembly = synth(config, tmp_path, return_assembly=True)
    manifest = json.loads((Path(assembly.directory) / "manifest.json").read_text())
    assert manifest.get("missing", []) == []
    template = assembly.get_stack_by_name("fixture").template
    subnets = props(template, "AWS::EC2::Subnet")
    assert {item["AvailabilityZone"] for item in subnets} == set(
        config.ecs_search.availability_zones
    )

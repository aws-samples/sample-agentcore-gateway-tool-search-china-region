"""China-partition stack; existing Gateway and versioned configuration are inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aws_cdk import Aws, CfnOutput, CfnParameter, Environment, RemovalPolicy, Stack, Tags
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.config import DeploymentConfig
from infra.ecs_search import EcsSearch, replay_permission
from infra.functions import Functions
from infra.monitoring import add_monitoring
from infra.scheduling import add_schedules
from infra.storage import Storage
from semantic_core.catalog import GatewayIdentity
from semantic_service.config import parse_search_config
from semantic_service.deployment_config import parse_configuration

OPERATIONS = ("search", "request", "indexer", "enhancer")


def documents(config: DeploymentConfig) -> dict[str, bytes]:
    """Create small operation-specific public configs; keys never contain secret values."""
    descriptor = json.dumps(config.search_descriptor, ensure_ascii=False, separators=(",", ":"))
    request_keys = set(config.oidc) - {"OIDC_TOKEN_ENDPOINT", "OIDC_MACHINE_SCOPES"}
    indexer_keys = {"OIDC_TOKEN_ENDPOINT", "OIDC_MACHINE_SCOPES", "OIDC_ALLOWED_HOSTS"}
    search_config = parse_search_config(config.model_settings | config.service_settings)
    model_settings = {
        "MODEL_RERANK_ENABLED": str(search_config.rerank_enabled).lower(),
        **config.model_settings,
    }
    settings = {
        "search": {
            "SEMSEARCH_SEARCH_DESCRIPTOR_JSON": descriptor,
            "SEMSEARCH_MODELS_ENABLED": str(config.models_enabled).lower(),
            "SEMSEARCH_FUSION_STRATEGY": search_config.fusion_strategy,
            **model_settings,
        },
        "request": {
            "SEMSEARCH_SEARCH_DESCRIPTOR_JSON": descriptor,
            "SEMSEARCH_MACHINE_PRINCIPALS_JSON": json.dumps(config.machine_principals),
            **{key: config.oidc[key] for key in request_keys},
        },
        "indexer": {
            "SEMSEARCH_REGISTRY_JSON": json.dumps(config.registry, separators=(",", ":")),
            "SEMSEARCH_GATEWAY_URL": config.gateway_url,
            "SEMSEARCH_GATEWAY_ALLOWED_HOSTS": ",".join(config.gateway_allowed_hosts),
            **{key: config.oidc[key] for key in indexer_keys},
        },
        "enhancer": {
            "SEMSEARCH_MODELS_ENABLED": str(config.models_enabled).lower(),
            **model_settings,
        },
    }
    role_settings = {
        "SEMSEARCH_SEARCH_TIMEOUT_SECONDS": "search",
        "SEMSEARCH_TOP_K": "search",
        "SEMSEARCH_FUSION_STRATEGY": "search",
        "SEMSEARCH_MIN_VECTOR_SIMILARITY": "search",
        "SEMSEARCH_MIN_VECTOR_MARGIN": "search",
        "SEMSEARCH_SUBJECT_CALLS_PER_MINUTE": "request",
        "SEMSEARCH_MACHINE_CALLS_PER_MINUTE": "request",
        "SEMSEARCH_MCP_VERSIONS": "indexer",
    }
    for key, value in config.service_settings.items():
        if key not in role_settings:
            raise ValueError("unsupported service setting")
        settings[role_settings[key]][key] = value
    result = {}
    for operation in OPERATIONS:
        document = {
            "version": 1,
            "gateway": {
                "account": config.account,
                "region": config.region,
                "gateway_id": config.gateway_id,
            },
            "operation": operation,
            "settings": settings[operation],
        }
        payload = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        if len(payload) > 1024 * 1024:
            raise ValueError("configuration exceeds the reviewed 1 MiB bound")
        parse_configuration(
            payload, GatewayIdentity(config.account, config.region, config.gateway_id), operation
        )
        result[operation] = payload
    return result


def write_configuration(config: DeploymentConfig, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for operation, payload in documents(config).items():
        checksum = hashlib.sha256(payload).hexdigest()
        (destination / f"{operation}.json").write_bytes(payload)
        manifest[operation] = {
            "file": f"{operation}.json",
            "key": (
                f"{config.account}/{config.region}/{config.gateway_id}/"
                + f"configuration/{operation}/{checksum}.json"
            ),
            "bytes": len(payload),
            "sha256": checksum,
            "version_parameter": f"{operation.title()}ConfigVersionId",
        }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


class SemsearchStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: DeploymentConfig,
        asset_path: Path,
        env: Environment,
        request_asset_path: Path | None = None,
        ecs_asset_path: Path | None = None,
    ) -> None:
        super().__init__(scope, construct_id, env=env)
        if self.region != config.region:
            # Tests and deploys must bind configuration documents to the deployed region.
            raise ValueError("configuration region must match the stack environment")
        if self.account != config.account:
            raise ValueError("configuration account must match the stack environment")
        if config.request_package == "shared" and request_asset_path is not None:
            raise ValueError("request_asset requires persistent request_package=request")
        if config.request_package == "request" and request_asset_path is None:
            request_asset_path = Path(".artifacts/request.zip")
        if (config.ecs_search is not None) != (ecs_asset_path is not None):
            raise ValueError("ecs_asset_path must be supplied exactly when ECS Search is enabled")
        if ecs_asset_path is not None and asset_path.resolve() == ecs_asset_path.resolve():
            raise ValueError("asset_path and ecs_asset_path must use different ZIP paths")
        if config.ecs_search is not None:
            # Some ECS/CDK constructs read Stack.availability_zones even when the
            # VPC already has explicit zones. Seed only this stack's exact lookup
            # key from the validated input so synth never requests AWS context.
            self.node.set_context(
                f"availability-zones:account={config.account}:region={config.region}",
                list(config.ecs_search.availability_zones),
            )
        Tags.of(self).add("project", "semsearch")
        Tags.of(self).add("stage", config.stage)
        prefix = config.prefix
        storage = Storage(self, "Storage", prefix=prefix)
        configuration_bucket = CfnParameter(
            self,
            "ConfigurationBucketName",
            type="String",
            allowed_pattern=r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]",
            description="Existing private, versioned, TLS-only regional S3 configuration bucket.",
        )
        config_bucket = s3.Bucket.from_bucket_name(
            self, "ConfigurationBucket", configuration_bucket.value_as_string
        )
        namespace = f"{Aws.ACCOUNT_ID}/{Aws.REGION}/{config.gateway_id}/"
        gateway_arn = (
            f"arn:{Aws.PARTITION}:bedrock-agentcore:{Aws.REGION}:"
            + f"{Aws.ACCOUNT_ID}:gateway/{config.gateway_id}"
        )
        secret_arns = {
            kind: (
                f"arn:{Aws.PARTITION}:secretsmanager:{Aws.REGION}:"
                + f"{Aws.ACCOUNT_ID}:secret:{ref.name}-{ref.suffix}"
            )
            for kind, ref in config.secrets.items()
        }
        environments = {}
        permissions: dict[str, list[iam.PolicyStatement]] = {}
        sized_environments: dict[str, dict[str, str]] = {}

        def object_arns(paths: list[str]) -> list[str]:
            # Immutable object UUIDs are not known at synth time. Each wildcard is
            # confined to one operation's required subtree of this Gateway prefix.
            return [storage.bucket.arn_for_objects(namespace + path + "/*") for path in paths]

        def ddb(
            actions: list[str], keys: list[str], *, pattern: bool = False
        ) -> iam.PolicyStatement:
            return iam.PolicyStatement(
                actions=actions,
                resources=[storage.table.table_arn],
                conditions={
                    "ForAllValues:StringLike" if pattern else "ForAllValues:StringEquals": {
                        "dynamodb:LeadingKeys": keys,
                    },
                    "Null": {"dynamodb:LeadingKeys": "false"},
                },
            )

        for operation, payload in documents(config).items():
            checksum = hashlib.sha256(payload).hexdigest()
            config_key = (
                f"{config.account}/{config.region}/{config.gateway_id}/"
                + f"configuration/{operation}/{checksum}.json"
            )
            version = CfnParameter(
                self,
                f"{operation.title()}ConfigVersionId",
                type="String",
                min_length=1,
                max_length=1024,
                allowed_pattern=r"(?!null$)[A-Za-z0-9._~+/=-]+",
                description="Exact VersionId returned by the authorized configuration upload.",
            )
            environments[operation] = {
                "SEMSEARCH_ACCOUNT": Aws.ACCOUNT_ID,
                "SEMSEARCH_REGION": Aws.REGION,
                "SEMSEARCH_GATEWAY_ID": config.gateway_id,
                "SEMSEARCH_SEARCH_TARGET_ID": config.search_target_id,
                "SEMSEARCH_SEARCH_TOOL_NAME": config.search_tool_name,
                "SEMSEARCH_SEARCH_INCARNATION": config.search_incarnation,
                "SEMSEARCH_BUCKET": storage.bucket.bucket_name,
                "SEMSEARCH_TABLE": storage.table.table_name,
                "SEMSEARCH_PROTECTED_TABLE": storage.protected_table.table_name,
                "SEMSEARCH_POLL_SECONDS": "60",
                "SEMSEARCH_SIGNING_SECRET_ID": secret_arns["signing"],
                "SEMSEARCH_OAUTH_SECRET_ID": secret_arns["oauth"],
                "MODEL_SECRET_ID": secret_arns["model"],
                "SEMSEARCH_CONFIG_BUCKET": configuration_bucket.value_as_string,
                "SEMSEARCH_CONFIG_KEY": config_key,
                "SEMSEARCH_CONFIG_VERSION_ID": version.value_as_string,
                "SEMSEARCH_CONFIG_SHA256": checksum,
                "SEMSEARCH_CONFIG_BYTES": str(len(payload)),
            }
            # Bound even the longest allowed parameter values before CloudFormation:
            # registry size no longer affects Lambda's aggregate 4 KiB environment.
            sized_environment = environments[operation] | {
                "SEMSEARCH_ACCOUNT": config.account,
                "SEMSEARCH_REGION": config.region,
                "SEMSEARCH_BUCKET": "b" * 63,
                "SEMSEARCH_TABLE": prefix + "-state",
                "SEMSEARCH_PROTECTED_TABLE": prefix + "-protected",
                "SEMSEARCH_CONFIG_BUCKET": "b" * 63,
                "SEMSEARCH_CONFIG_VERSION_ID": "v" * 1024,
            }
            for kind, env_name in (
                ("model", "MODEL_SECRET_ID"),
                ("signing", "SEMSEARCH_SIGNING_SECRET_ID"),
                ("oauth", "SEMSEARCH_OAUTH_SECRET_ID"),
            ):
                reference = config.secrets[kind]
                sized_environment[env_name] = (
                    f"arn:aws-cn:secretsmanager:{config.region}:{config.account}:"
                    + f"secret:{reference.name}-{reference.suffix}"
                )
            if (
                sum(
                    len(key.encode()) + len(value.encode())
                    for key, value in sized_environment.items()
                )
                > 4096
            ):
                raise ValueError("base Lambda environment can exceed 4 KiB; shorten configuration")
            sized_environments[operation] = sized_environment
            # Authorization/deletion state has a separate table ARN boundary.
            # Only an external administrator owns its writes. Cross-table
            # transactions use per-item permissions; ConditionCheckItem is read-only.
            statements = [
                ddb(["dynamodb:GetItem"], [namespace + "CONTROL"]),
                iam.PolicyStatement(
                    actions=["dynamodb:GetItem"],
                    resources=[storage.protected_table.table_arn],
                ),
                iam.PolicyStatement(
                    actions=["s3:GetObjectVersion"],
                    resources=[config_bucket.arn_for_objects(config_key)],
                    conditions={"StringEquals": {"s3:VersionId": version.value_as_string}},
                ),
            ]
            read_paths = {
                "search": ["versions", "policies", "safety"],
                "request": ["policies", "safety"],
                "indexer": ["versions", "safety"],
                "enhancer": ["versions", "safety", "enhancement"],
            }[operation]
            statements.append(
                iam.PolicyStatement(
                    actions=["s3:GetObjectVersion"], resources=object_arns(read_paths)
                )
            )
            if operation in ("indexer", "enhancer"):
                statements.extend(
                    [
                        ddb(["dynamodb:PutItem"], [namespace + "CONTROL"]),
                        iam.PolicyStatement(
                            actions=["dynamodb:ConditionCheckItem"],
                            resources=[storage.protected_table.table_arn],
                        ),
                        iam.PolicyStatement(
                            actions=["s3:PutObject"],
                            resources=object_arns(
                                ["versions", "safety"]
                                if operation == "indexer"
                                else ["versions", "enhancement"]
                            ),
                        ),
                    ]
                )
            if operation == "enhancer":
                # Revision/content hashes identify bounded per-Gateway progress/cache keys.
                statements.append(
                    ddb(
                        ["dynamodb:GetItem", "dynamodb:PutItem"],
                        [namespace + "enhancement-cache/*", namespace + "enhancement-progress/*"],
                        pattern=True,
                    )
                )
            if operation == "request":
                # Rate keys hash principal+Gateway, so only a dedicated physical table
                # makes this runtime-generated key family a per-Gateway IAM boundary.
                statements.append(ddb(["dynamodb:UpdateItem"], ["rate/*"], pattern=True))
            if operation == "indexer":
                statements.append(
                    iam.PolicyStatement(
                        actions=[
                            "bedrock-agentcore:ListGatewayTargets",
                            "bedrock-agentcore:GetGatewayTarget",
                        ],
                        resources=[gateway_arn],
                    )
                )
            needed_secrets = {
                "search": ["signing"],
                "request": ["signing"],
                "indexer": ["oauth"],
                "enhancer": [],
            }[operation]
            if config.models_enabled and operation in ("search", "enhancer"):
                needed_secrets.append("model")
            for kind in needed_secrets:
                statements.append(
                    iam.PolicyStatement(
                        actions=["secretsmanager:GetSecretValue"], resources=[secret_arns[kind]]
                    )
                )
                key_id = config.secrets[kind].kms_key_id
                if key_id is not None:
                    statements.append(
                        iam.PolicyStatement(
                            actions=["kms:Decrypt"],
                            resources=[
                                f"arn:{Aws.PARTITION}:kms:{Aws.REGION}:{Aws.ACCOUNT_ID}:key/{key_id}"
                            ],
                            conditions={
                                "StringEquals": {
                                    "kms:ViaService": (
                                        f"secretsmanager.{Aws.REGION}.{Aws.URL_SUFFIX}"
                                    ),
                                    "kms:EncryptionContext:SecretARN": secret_arns[kind],
                                }
                            },
                        )
                    )
            permissions[operation] = statements

        ecs_service = None
        if config.ecs_search is not None:
            ecs_config = config.ecs_search
            replay = dynamodb.Table(
                self,
                "ReplayTable",
                table_name=f"{prefix}-replay",
                partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
                billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
                encryption=dynamodb.TableEncryption.AWS_MANAGED,
                point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True,
                ),
                time_to_live_attribute="expires_at",
                deletion_protection=True,
                removal_policy=RemovalPolicy.RETAIN,
            )
            search_environment = environments["search"] | {
                "SEMSEARCH_REPLAY_TABLE": replay.table_name,
            }
            # Reuse the existing, fully bounded Search read permissions. Only each
            # backend's dedicated replay namespace differs; REQUEST is untouched.
            ecs_service = EcsSearch(
                self,
                "EcsSearch",
                prefix=prefix,
                config=ecs_config,
                environment=search_environment
                | {
                    **{
                        key: value
                        for key, value in config.oidc.items()
                        if key not in ("OIDC_TOKEN_ENDPOINT", "OIDC_MACHINE_SCOPES")
                    },
                    "SEMSEARCH_ECS_OAUTH_SUBJECT": ecs_config.oauth_subject,
                    "SEMSEARCH_ECS_OAUTH_SCOPE": ecs_config.oauth_scope,
                },
                permissions=[*permissions["search"], replay_permission(replay, "ecs")],
                retention_days=config.log_retention_days,
                alarm_actions=config.alarm_topic_arns,
            )
            environments["fallback"] = search_environment
            sized_fallback = sized_environments["search"] | {
                "SEMSEARCH_REPLAY_TABLE": f"{prefix}-replay",
            }
            if (
                sum(
                    len(key.encode()) + len(value.encode()) for key, value in sized_fallback.items()
                )
                > 4096
            ):
                raise ValueError(
                    "fallback Lambda environment can exceed 4 KiB; shorten configuration"
                )
            permissions["fallback"] = [
                *permissions["search"],
                replay_permission(replay, "fallback"),
            ]
            facade_secret = (
                f"arn:{Aws.PARTITION}:secretsmanager:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                + f"secret:{ecs_config.oauth_secret.name}-{ecs_config.oauth_secret.suffix}"
            )
            fallback_alias = (
                f"arn:{Aws.PARTITION}:lambda:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                + f"function:{prefix}-fallback:live"
            )
            environments["facade"] = {
                key: environments["search"][key]
                for key in (
                    "SEMSEARCH_ACCOUNT",
                    "SEMSEARCH_REGION",
                    "SEMSEARCH_GATEWAY_ID",
                    "SEMSEARCH_SEARCH_TARGET_ID",
                    "SEMSEARCH_SEARCH_TOOL_NAME",
                    "SEMSEARCH_SEARCH_INCARNATION",
                )
            } | {
                "SEMSEARCH_ECS_URL": f"https://{ecs_config.hostname}/search",
                "SEMSEARCH_FALLBACK_ALIAS_ARN": fallback_alias,
                "SEMSEARCH_ECS_OAUTH_SECRET_ID": facade_secret,
                "OIDC_TOKEN_ENDPOINT": config.oidc["OIDC_TOKEN_ENDPOINT"],
                "OIDC_ALLOWED_HOSTS": config.oidc["OIDC_ALLOWED_HOSTS"],
                "OIDC_MACHINE_SCOPES": ecs_config.oauth_scope,
            }
            sized_facade = environments["facade"] | {
                "SEMSEARCH_ACCOUNT": config.account,
                "SEMSEARCH_REGION": config.region,
                "SEMSEARCH_FALLBACK_ALIAS_ARN": (
                    f"arn:aws-cn:lambda:{config.region}:{config.account}:"
                    + f"function:{prefix}-fallback:live"
                ),
                "SEMSEARCH_ECS_OAUTH_SECRET_ID": (
                    f"arn:aws-cn:secretsmanager:{config.region}:{config.account}:"
                    + f"secret:{ecs_config.oauth_secret.name}-{ecs_config.oauth_secret.suffix}"
                ),
            }
            if (
                sum(len(key.encode()) + len(value.encode()) for key, value in sized_facade.items())
                > 4096
            ):
                raise ValueError(
                    "facade Lambda environment can exceed 4 KiB; shorten configuration"
                )
            permissions["facade"] = [
                iam.PolicyStatement(
                    actions=["lambda:InvokeFunction"],
                    resources=[fallback_alias],
                ),
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[facade_secret],
                ),
            ]
            if ecs_config.oauth_secret.kms_key_id is not None:
                permissions["facade"].append(
                    iam.PolicyStatement(
                        actions=["kms:Decrypt"],
                        resources=[
                            (
                                f"arn:{Aws.PARTITION}:kms:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                                + f"key/{ecs_config.oauth_secret.kms_key_id}"
                            )
                        ],
                        conditions={
                            "StringEquals": {
                                "kms:ViaService": f"secretsmanager.{Aws.REGION}.{Aws.URL_SUFFIX}",
                                "kms:EncryptionContext:SecretARN": facade_secret,
                            }
                        },
                    )
                )
            eni_actions = [
                "ec2:CreateNetworkInterface",
                "ec2:DescribeNetworkInterfaces",
                "ec2:DescribeSubnets",
                "ec2:DeleteNetworkInterface",
                "ec2:AssignPrivateIpAddresses",
                "ec2:UnassignPrivateIpAddresses",
            ]
            # Lambda's Hyperplane ENI service requires these exact actions on '*'.
            # SourceFunctionArn exists only on calls made by executing function
            # code: the explicit Deny leaves service ENI management available.
            permissions["facade"].extend(
                [
                    iam.PolicyStatement(actions=eni_actions, resources=["*"]),
                    iam.PolicyStatement(
                        effect=iam.Effect.DENY,
                        actions=[*eni_actions, "ec2:DetachNetworkInterface"],
                        resources=["*"],
                        conditions={
                            "ArnEquals": {
                                "lambda:SourceFunctionArn": (
                                    f"arn:{Aws.PARTITION}:lambda:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                                    + f"function:{prefix}-facade"
                                )
                            }
                        },
                    ),
                ]
            )
            CfnOutput(self, "ReplayTableName", value=replay.table_name)

        functions = Functions(
            self,
            "Functions",
            prefix=prefix,
            asset_path=asset_path,
            request_asset_path=request_asset_path,
            request_provisioned_concurrency=config.request_provisioned_concurrency,
            environments=environments,
            permissions=permissions,
            retention_days=config.log_retention_days,
            ecs_enabled=config.ecs_search is not None,
            ecs_asset_path=ecs_asset_path,
            facade_vpc_config=(
                lambda_.CfnFunction.VpcConfigProperty(
                    subnet_ids=ecs_service.facade_subnet_ids,
                    security_group_ids=[ecs_service.facade_security_group.security_group_id],
                )
                if ecs_service is not None
                else None
            ),
        )
        # Same-account Gateway invokes through its existing execution role. Its
        # separately managed trust policy must bind the correct Gateway/account.
        # Do not invent a direct service principal Lambda permission or SourceArn.
        gateway_role = iam.Role.from_role_name(
            self, "GatewayExecutionRole", config.gateway_execution_role_name
        )
        gateway_policy = iam.Policy(
            self,
            "GatewayInvokePolicy",
            policy_name=f"{prefix}-gateway-invoke",
            statements=[
                iam.PolicyStatement(
                    actions=["lambda:InvokeFunction"],
                    resources=[
                        functions.aliases[name].function_arn
                        for name in (
                            ("search", "facade", "request")
                            if config.ecs_search is not None
                            else ("search", "request")
                        )
                    ],
                )
            ],
        )
        gateway_policy.attach_to_role(gateway_role)
        add_schedules(
            self, prefix=prefix, aliases=functions.aliases, enabled=config.schedules_enabled
        )
        add_monitoring(
            self,
            prefix=prefix,
            log_groups=functions.log_groups,
            function_names=functions.names,
            alarm_actions=config.alarm_topic_arns,
            ecs_log_group=ecs_service.log_group if ecs_service is not None else None,
        )
        if config.request_provisioned_concurrency > 0:
            # Init can emit its only recovery/failure record as soon as PC is
            # allocated. Create its filters and alarms before updating the alias.
            for resource_id in (
                "request-init-auth-recoveredFilter",
                "request-init-auth-recoveredAlarm",
                "request-init-oidc-prefetch-failedFilter",
                "request-init-oidc-prefetch-failedAlarm",
            ):
                functions.aliases["request"].node.add_dependency(self.node.find_child(resource_id))
        CfnOutput(self, "IndexBucketName", value=storage.bucket.bucket_name)
        CfnOutput(self, "StateTableName", value=storage.table.table_name)
        CfnOutput(self, "ProtectedTableName", value=storage.protected_table.table_name)
        CfnOutput(self, "GatewayArn", value=gateway_arn)
        CfnOutput(self, "Partition", value=Aws.PARTITION)
        CfnOutput(self, "RegionalS3Endpoint", value=f"https://s3.{Aws.REGION}.{Aws.URL_SUFFIX}")
        for operation, alias in functions.aliases.items():
            CfnOutput(self, f"{operation.title()}AliasArn", value=alias.function_arn)
        CfnOutput(
            self,
            "GatewayProvisioning",
            value=(
                "Existing Gateway input only; China CloudFormation Gateway/Target "
                + "support awaits real verification."
            ),
        )

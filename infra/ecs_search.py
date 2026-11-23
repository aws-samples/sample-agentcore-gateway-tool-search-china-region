"""Always-on Fargate Search, exact-role boundaries and a TLS NLB, without lookups."""

from __future__ import annotations

from aws_cdk import Aws, CfnOutput, Duration, RemovalPolicy, Size
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

from infra.config import EcsSearchConfig


def replay_permission(table: dynamodb.Table, backend: str) -> iam.PolicyStatement:
    """Restrict each runtime to its prefix; the application enforces conditional writes."""
    if backend not in ("ecs", "fallback"):
        raise ValueError("unknown replay backend")
    return iam.PolicyStatement(
        actions=["dynamodb:PutItem"],
        resources=[table.table_arn],
        conditions={
            "ForAllValues:StringLike": {"dynamodb:LeadingKeys": [backend + "/*"]},
            "Null": {"dynamodb:LeadingKeys": "false"},
        },
    )


class EcsSearch(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        prefix: str,
        config: EcsSearchConfig,
        environment: dict[str, str],
        permissions: list[iam.PolicyStatement],
        retention_days: int,
        alarm_actions: tuple[str, ...] = (),
    ) -> None:
        super().__init__(scope, construct_id)
        retention = {
            7: logs.RetentionDays.ONE_WEEK,
            14: logs.RetentionDays.TWO_WEEKS,
            30: logs.RetentionDays.ONE_MONTH,
            60: logs.RetentionDays.TWO_MONTHS,
            90: logs.RetentionDays.THREE_MONTHS,
            180: logs.RetentionDays.SIX_MONTHS,
            365: logs.RetentionDays.ONE_YEAR,
        }[retention_days]
        self.log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"{prefix}-ecs-search-logs",
            retention=retention,
            removal_policy=RemovalPolicy.RETAIN,
        )
        vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("10.42.0.0/24"),
            availability_zones=list(config.availability_zones),
            nat_gateways=2,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=26,
                ),
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=26,
                ),
            ],
        )
        self.facade_subnet_ids = vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
        ).subnet_ids
        self.facade_security_group = ec2.SecurityGroup(
            self,
            "FacadeSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description="Search facade has no ingress; HTTPS egress uses private subnets and NAT",
        )
        self.facade_security_group.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443))
        nlb_security = ec2.SecurityGroup(
            self,
            "NlbSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description=(
                "Private TLS ingress only from the dedicated Search facade security group"
            ),
        )
        task_security = ec2.SecurityGroup(
            self,
            "TaskSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description="Search accepts TLS only from the dedicated NLB",
        )
        nlb_security.add_ingress_rule(self.facade_security_group, ec2.Port.tcp(443))
        task_security.add_ingress_rule(nlb_security, ec2.Port.tcp(8443))
        nlb_security.add_egress_rule(task_security, ec2.Port.tcp(8443))
        task_security.add_ingress_rule(nlb_security, ec2.Port.tcp(9443))
        nlb_security.add_egress_rule(task_security, ec2.Port.tcp(9443))
        task_security.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443))
        service_name = f"{prefix}-search"
        namespace = f"semsearch/{prefix}"
        self.cluster = ecs.Cluster(
            self,
            "Cluster",
            cluster_name=service_name,
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )
        task_role = iam.Role(
            self,
            "TaskRole",
            role_name=f"{prefix}-ecs-search-role",
            assumed_by=iam.ServicePrincipal(
                "ecs-tasks.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:{Aws.PARTITION}:ecs:{Aws.REGION}:{Aws.ACCOUNT_ID}:*"
                    },
                },
            ),
            inline_policies={"search-read-and-replay": iam.PolicyDocument(statements=permissions)},
        )
        # Regional gateway routes take precedence over NAT. ECR downloads its
        # layers from S3, so a restricted endpoint must allow that bucket too.
        private_subnets = vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
        endpoints = []
        for service_key, endpoint_service in (
            ("S3", ec2.GatewayVpcEndpointAwsService.S3),
            ("DynamoDB", ec2.GatewayVpcEndpointAwsService.DYNAMODB),
        ):
            endpoint = vpc.add_gateway_endpoint(
                f"{service_key}Endpoint",
                service=endpoint_service,
                subnets=[ec2.SubnetSelection(subnets=private_subnets.subnets)],
            )
            prefix_action = "s3:" if service_key == "S3" else "dynamodb:"
            for permission in permissions:
                statement = permission.to_statement_json()
                actions = statement["Action"]
                actions = actions if isinstance(actions, list) else [actions]
                if all(action.startswith(prefix_action) for action in actions):
                    # DynamoDB key conditions remain in the task role. The
                    # endpoint restricts actions, tables and the exact role;
                    # it does not repeat service-specific key evaluation.
                    conditions = {}
                    for operator, values in statement.get("Condition", {}).items():
                        retained = {
                            key: value
                            for key, value in values.items()
                            if service_key != "DynamoDB" or key != "dynamodb:LeadingKeys"
                        }
                        if retained:
                            conditions[operator] = retained
                    conditions["StringEquals"] = {
                        **conditions.get("StringEquals", {}),
                        "aws:PrincipalArn": task_role.role_arn,
                    }
                    endpoint.add_to_policy(
                        iam.PolicyStatement.from_json(
                            statement | {"Principal": "*", "Condition": conditions}
                        )
                    )
            if service_key == "S3":
                endpoint.add_to_policy(
                    iam.PolicyStatement(
                        principals=[iam.StarPrincipal()],
                        actions=["s3:GetObject"],
                        resources=[
                            f"arn:{Aws.PARTITION}:s3:::prod-{Aws.REGION}-starport-layer-bucket/*"
                        ],
                    )
                )
            endpoints.append(endpoint)
        repository_name = config.image_uri.split("/", 1)[1].split("@", 1)[0]
        execution_role = iam.Role(
            self,
            "ExecutionRole",
            role_name=f"{prefix}-ecs-execution-role",
            assumed_by=iam.ServicePrincipal(
                "ecs-tasks.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:{Aws.PARTITION}:ecs:{Aws.REGION}:{Aws.ACCOUNT_ID}:*"
                    },
                },
            ),
            inline_policies={
                "image-and-logs": iam.PolicyDocument(
                    statements=[
                        # ECR does not support resource ARNs for GetAuthorizationToken.
                        iam.PolicyStatement(
                            actions=["ecr:GetAuthorizationToken"],
                            resources=["*"],
                            conditions={"StringEquals": {"aws:RequestedRegion": Aws.REGION}},
                        ),
                        iam.PolicyStatement(
                            actions=[
                                "ecr:BatchCheckLayerAvailability",
                                "ecr:GetDownloadUrlForLayer",
                                "ecr:BatchGetImage",
                            ],
                            resources=[
                                f"arn:{Aws.PARTITION}:ecr:{Aws.REGION}:{Aws.ACCOUNT_ID}:repository/{repository_name}"
                            ],
                        ),
                        iam.PolicyStatement(
                            actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                            resources=[self.log_group.log_group_arn],
                        ),
                    ]
                )
            },
        )
        task = ecs.FargateTaskDefinition(
            self,
            "Task",
            family=f"{prefix}-search",
            cpu=2048,
            memory_limit_mib=4096,
            task_role=task_role,
            execution_role=execution_role,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        task.add_volume(name="tmp")
        linux = ecs.LinuxParameters(self, "Linux", init_process_enabled=True)
        linux.drop_capabilities(ecs.Capability.ALL)
        container = task.add_container(
            "Search",
            container_name="search",
            image=ecs.ContainerImage.from_registry(config.image_uri),
            environment=environment
            | {
                "SEMSEARCH_ECS_PORT": "8443",
                "SEMSEARCH_ECS_WORKERS": "4",
                "SEMSEARCH_ECS_HEALTH_PORT": "9443",
                "SEMSEARCH_ECS_METRIC_NAMESPACE": namespace,
                "SEMSEARCH_ECS_CLUSTER_NAME": self.cluster.cluster_name,
                "SEMSEARCH_ECS_SERVICE_NAME": service_name,
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            user="10001:10001",
            readonly_root_filesystem=True,
            stop_timeout=Duration.seconds(30),
            start_timeout=Duration.seconds(30),
            linux_parameters=linux,
            health_check=ecs.HealthCheck(
                command=["CMD", "python", "-m", "semantic_service.ecs_health"],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(120),
            ),
            logging=ecs.LogDrivers.aws_logs(
                log_group=self.log_group,
                stream_prefix="search",
                mode=ecs.AwsLogDriverMode.NON_BLOCKING,
                max_buffer_size=Size.mebibytes(4),
            ),
        )
        container.add_mount_points(
            # private task volume; init container sets uid 10001 and mode 0700.
            ecs.MountPoint(container_path="/tmp", source_volume="tmp", read_only=False)  # nosec B108
        )
        container.add_port_mappings(
            ecs.PortMapping(container_port=8443, protocol=ecs.Protocol.TCP),
            ecs.PortMapping(container_port=9443, protocol=ecs.Protocol.TCP),
        )
        # A fresh Fargate bind volume starts root-owned. Keep privilege confined
        # to this one-shot initializer; the server cannot start before it exits 0.
        # Fargate cannot Add CHOWN after Drop ALL. Retain only its default CHOWN
        # capability, and chmod while the directory is still owned by this uid.
        initializer_linux = ecs.LinuxParameters(self, "InitializeLinux")
        initializer_linux.drop_capabilities(
            *(
                capability
                for capability in ecs.Capability
                if capability not in (ecs.Capability.ALL, ecs.Capability.CHOWN)
            )
        )
        initializer = task.add_container(
            "InitializeTmp",
            container_name="initialize-tmp",
            image=ecs.ContainerImage.from_registry(config.image_uri),
            entry_point=["python", "-c"],
            command=[
                "import os; os.chmod('/runtime-tmp', 0o700); os.chown('/runtime-tmp', 10001, 10001)"
            ],
            user="0:0",
            essential=False,
            readonly_root_filesystem=True,
            linux_parameters=initializer_linux,
            logging=ecs.LogDrivers.aws_logs(
                log_group=self.log_group,
                stream_prefix="initialize",
                mode=ecs.AwsLogDriverMode.NON_BLOCKING,
                max_buffer_size=Size.mebibytes(4),
            ),
        )
        initializer.add_mount_points(
            ecs.MountPoint(
                container_path="/runtime-tmp",
                source_volume="tmp",
                read_only=False,
            )
        )
        container.add_container_dependencies(
            ecs.ContainerDependency(
                container=initializer,
                condition=ecs.ContainerDependencyCondition.SUCCESS,
            )
        )
        self.service = ecs.FargateService(
            self,
            "Service",
            service_name=service_name,
            cluster=self.cluster,
            task_definition=task,
            desired_count=2,
            min_healthy_percent=100,
            max_healthy_percent=200,
            assign_public_ip=False,
            security_groups=[task_security],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
            health_check_grace_period=Duration.seconds(210),
            platform_version=ecs.FargatePlatformVersion.VERSION1_4,
            enable_execute_command=False,
        )
        self.service.node.add_dependency(private_subnets.internet_connectivity_established)
        self.service.node.add_dependency(*endpoints)
        nlb = elb.NetworkLoadBalancer(
            self,
            "LoadBalancer",
            vpc=vpc,
            internet_facing=False,
            security_groups=[nlb_security],
            cross_zone_enabled=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )
        target = elb.NetworkTargetGroup(
            self,
            "Targets",
            vpc=vpc,
            port=8443,
            protocol=elb.Protocol.TLS,
            target_type=elb.TargetType.IP,
            targets=[self.service],
            preserve_client_ip=False,
            deregistration_delay=Duration.seconds(30),
            health_check=elb.HealthCheck(
                enabled=True,
                protocol=elb.Protocol.HTTPS,
                port="9443",
                path="/ready",
                interval=Duration.seconds(30),
                healthy_threshold_count=2,
                unhealthy_threshold_count=2,
                timeout=Duration.seconds(6),
            ),
        )
        nlb.add_listener(
            "Tls",
            port=443,
            protocol=elb.Protocol.TLS,
            certificates=[elb.ListenerCertificate.from_arn(config.certificate_arn)],
            ssl_policy=elb.SslPolicy.RECOMMENDED_TLS,
            default_target_groups=[target],
        )
        scaling = self.service.auto_scale_task_count(min_capacity=2, max_capacity=4)
        scaling.scale_on_cpu_utilization(
            "Cpu",
            target_utilization_percent=60,
            scale_in_cooldown=Duration.minutes(5),
            scale_out_cooldown=Duration.seconds(60),
        )
        slot_metric = cloudwatch.Metric(
            namespace=namespace,
            metric_name="SearchSlotUtilization",
            dimensions_map={
                "ClusterName": self.cluster.cluster_name,
                "ServiceName": service_name,
            },
            statistic="Average",
            period=Duration.minutes(1),
            unit=cloudwatch.Unit.PERCENT,
        )
        scaling.scale_to_track_custom_metric(
            "Slots",
            metric=slot_metric,
            target_value=60,
            scale_in_cooldown=Duration.minutes(5),
            scale_out_cooldown=Duration.seconds(60),
        )

        def alarm(
            name: str,
            metric: cloudwatch.IMetric,
            threshold: int,
            *,
            below: bool = False,
            missing: bool = False,
            periods: int = 2,
            description: str,
        ) -> None:
            alarm_resource = cloudwatch.Alarm(
                self,
                name + "Alarm",
                alarm_name=f"{prefix}-{name}",
                alarm_description=description,
                metric=metric,
                threshold=threshold,
                evaluation_periods=periods,
                datapoints_to_alarm=periods,
                comparison_operator=(
                    cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD
                    if below
                    else cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
                ),
                treat_missing_data=(
                    cloudwatch.TreatMissingData.BREACHING
                    if missing
                    else cloudwatch.TreatMissingData.NOT_BREACHING
                ),
            )
            cfn = alarm_resource.node.default_child
            assert isinstance(cfn, cloudwatch.CfnAlarm)
            if alarm_actions:
                cfn.alarm_actions = list(alarm_actions)

        for metric_name, threshold in (("CPUUtilization", 85), ("MemoryUtilization", 85)):
            alarm(
                "ecs-" + metric_name.lower(),
                cloudwatch.Metric(
                    namespace="AWS/ECS",
                    metric_name=metric_name,
                    dimensions_map={
                        "ClusterName": self.cluster.cluster_name,
                        "ServiceName": self.service.service_name,
                    },
                    period=Duration.minutes(1),
                    statistic="Average",
                ),
                threshold,
                description=(
                    f"ECS {metric_name} above {threshold} percent for two minutes; "
                    "inspect task pressure."
                ),
            )
        alarm(
            "ecs-running-tasks",
            cloudwatch.Metric(
                namespace="ECS/ContainerInsights",
                metric_name="RunningTaskCount",
                dimensions_map={
                    "ClusterName": self.cluster.cluster_name,
                    "ServiceName": self.service.service_name,
                },
                period=Duration.minutes(1),
                statistic="Minimum",
            ),
            2,
            below=True,
            missing=True,
            description=(
                "Fewer than two Search tasks or no task telemetry; inspect ECS service events."
            ),
        )
        alarm(
            "ecs-unhealthy-targets",
            target.metrics.un_healthy_host_count(
                period=Duration.minutes(1),
                statistic="Maximum",
            ),
            0,
            description=(
                "Unhealthy NLB Search targets for two minutes; "
                "inspect dedicated readiness and task logs."
            ),
        )
        alarm(
            "ecs-healthy-targets",
            target.metrics.healthy_host_count(
                period=Duration.minutes(1),
                statistic="Minimum",
            ),
            2,
            below=True,
            missing=True,
            description=(
                "Fewer than two healthy Search targets or missing telemetry; "
                "inspect readiness and rollout."
            ),
        )
        for metric_name in ("ClientTLSNegotiationErrorCount", "TargetTLSNegotiationErrorCount"):
            alarm(
                "ecs-" + metric_name.lower(),
                cloudwatch.Metric(
                    namespace="AWS/NetworkELB",
                    metric_name=metric_name,
                    dimensions_map={"LoadBalancer": nlb.load_balancer_full_name},
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
                0,
                periods=1,
                description=(
                    "Any private facade-to-NLB TLS failure; inspect certificate validity, "
                    "hostname, trust and client compatibility even at low traffic."
                    if metric_name == "ClientTLSNegotiationErrorCount"
                    else "Any NLB-to-task TLS failure; "
                    "check target certificates, ports and task health."
                ),
            )
        for name, pattern in (
            (
                "ecs-worker-failed",
                '{ ($.operation = "search_server" && '
                '($.status = "worker initialization failed" || '
                '$.status = "worker reclaim failed" || $.status = "internal failure")) || '
                '($.operation = "search_ingress" && $.status = "worker unavailable") }',
            ),
            (
                "ecs-auth-failed",
                '{ ($.operation = "search_backend" || $.operation = "search_ingress") && '
                '$.phase = "end" && $.status = "unauthenticated" }',
            ),
            (
                "ecs-replay-failed",
                '{ $.operation = "search_backend" && '
                '($.status = "replay rejected" || $.status = "replay unavailable") }',
            ),
            (
                "ecs-backend-errors",
                '{ ($.operation = "search_backend" || $.operation = "search_server" || '
                '$.operation = "search_ingress") && $.phase = "end" && '
                '($.status = "internal failure" || $.status = "index not ready" || '
                '$.status = "deadline exceeded" || $.status = "clock skew") }',
            ),
        ):
            logs.MetricFilter(
                self,
                name + "Filter",
                log_group=self.log_group,
                filter_pattern=logs.FilterPattern.literal(pattern),
                metric_namespace=namespace,
                metric_name=name,
                metric_value="1",
            )
            alarm(
                name,
                cloudwatch.Metric(
                    namespace=namespace,
                    metric_name=name,
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
                0,
                periods=1,
                description=(
                    f"Any {name} event; inspect the redacted ingress/backend logs "
                    "and the Search operations runbook."
                ),
            )
        alarm(
            "ecs-slot-utilization",
            slot_metric,
            85,
            description=(
                "Search execution slots exceed 85 percent for two minutes, including I/O waits; "
                "verify autoscaling and fallback capacity."
            ),
        )
        for metric_name, statistic, threshold in (
            ("NewFlowCount", "Sum", 1200),
            ("ActiveFlowCount", "Maximum", 400),
        ):
            alarm(
                "ecs-" + metric_name.lower(),
                cloudwatch.Metric(
                    namespace="AWS/NetworkELB",
                    metric_name=metric_name,
                    dimensions_map={"LoadBalancer": nlb.load_balancer_full_name},
                    statistic=statistic,
                    period=Duration.minutes(1),
                ),
                threshold,
                description=(
                    f"NLB {metric_name} exceeds the provisional connection-pressure threshold; "
                    "inspect connection-capacity logs and legitimate traffic before tuning."
                ),
            )
        logs.MetricFilter(
            self,
            "BackendDurationFilter",
            log_group=self.log_group,
            filter_pattern=logs.FilterPattern.literal(
                '{ $.operation = "search_backend" && $.phase = "end" && $.duration_ms = * }'
            ),
            metric_namespace=namespace,
            metric_name="ecs-search-duration-ms",
            metric_value="$.duration_ms",
            unit=cloudwatch.Unit.MILLISECONDS,
        )
        alarm(
            "ecs-search-p95",
            cloudwatch.Metric(
                namespace=namespace,
                metric_name="ecs-search-duration-ms",
                statistic="p95",
                period=Duration.minutes(1),
                unit=cloudwatch.Unit.MILLISECONDS,
            ),
            3000,
            description=(
                "ECS backend p95 exceeds 3 seconds for two minutes; inspect admission, "
                "model budgets and snapshot timings even if fallback is zero."
            ),
        )
        CfnOutput(scope, "SearchEcsEndpoint", value=f"https://{config.hostname}/search")
        CfnOutput(scope, "SearchNlbDnsName", value=nlb.load_balancer_dns_name)
        CfnOutput(
            scope,
            "SearchNlbCanonicalHostedZoneId",
            value=nlb.load_balancer_canonical_hosted_zone_id,
        )
        CfnOutput(scope, "SearchEcsTaskRoleArn", value=task_role.role_arn)
        CfnOutput(scope, "SearchEcsImageDigest", value=config.image_uri)

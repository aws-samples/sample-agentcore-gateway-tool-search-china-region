"""Bounded delivery to published aliases with a schedule-group trust boundary."""

from __future__ import annotations

from aws_cdk import Aws, Duration
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_scheduler as scheduler
from constructs import Construct


def add_schedules(
    scope: Construct,
    *,
    prefix: str,
    aliases: dict[str, lambda_.Alias],
    enabled: bool,
) -> None:
    group = scheduler.CfnScheduleGroup(scope, "ScheduleGroup", name=f"{prefix}-ticks")
    for operation, expression in (("indexer", "rate(1 minute)"), ("enhancer", "rate(5 minutes)")):
        alias = aliases[operation]
        role = iam.Role(
            scope,
            f"{operation}ScheduleRole",
            role_name=f"{prefix}-{operation}-schedule",
            # Documented China principal is unchanged; do not derive from URL_SUFFIX.
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
                    # Scheduler uses the group ARN, not the individual schedule ARN.
                    "ArnEquals": {"aws:SourceArn": group.attr_arn},
                },
            ),
            inline_policies={
                f"{prefix}-{operation}-invoke": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["lambda:InvokeFunction"], resources=[alias.function_arn]
                        )
                    ],
                ),
            },
        )
        scheduler.CfnSchedule(
            scope,
            f"{operation}Schedule",
            name=f"{prefix}-{operation}",
            group_name=group.ref,
            schedule_expression=expression,
            schedule_expression_timezone="UTC",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            state="ENABLED" if enabled else "DISABLED",
            target=scheduler.CfnSchedule.TargetProperty(
                arn=alias.function_arn,
                role_arn=role.role_arn,
                input='{"time":"<aws.scheduler.scheduled-time>"}',
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_event_age_in_seconds=60,
                    maximum_retry_attempts=0,
                ),
            ),
        )
        alias.configure_async_invoke(
            max_event_age=Duration.seconds(60),
            retry_attempts=0,
        )

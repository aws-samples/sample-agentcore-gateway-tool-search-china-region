"""Alarms use only Lambda metrics and the service's existing redacted log fields."""

from __future__ import annotations

from aws_cdk import Duration, Tags
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_logs as logs
from constructs import Construct

from semantic_service._indexer_telemetry import COUNTER_FIELDS


def add_monitoring(
    scope: Construct,
    *,
    prefix: str,
    log_groups: dict[str, logs.LogGroup],
    function_names: dict[str, str],
    alarm_actions: tuple[str, ...] = (),
    ecs_log_group: logs.LogGroup | None = None,
) -> None:
    namespace = f"semsearch/{prefix}"

    def metric_filter(name: str, operation: str, pattern: str, value: str = "1") -> None:
        logs.MetricFilter(
            scope,
            f"{name}Filter",
            filter_name=f"{prefix}-{name}",
            log_group=log_groups[operation],
            filter_pattern=logs.FilterPattern.literal(pattern),
            metric_namespace=namespace,
            metric_name=name,
            metric_value=value,
        )
        if operation == "search" and name in {
            "search-count",
            "search-internal-errors",
            "search-degraded",
            "vector-failed",
        }:
            for backend, group in (
                ("ecs", ecs_log_group),
                ("fallback", log_groups.get("fallback")),
            ):
                if group is not None:
                    logs.MetricFilter(
                        scope,
                        f"{name}{backend.title()}Filter",
                        filter_name=f"{prefix}-{name}-{backend}",
                        log_group=group,
                        filter_pattern=logs.FilterPattern.literal(pattern),
                        metric_namespace=f"{namespace}/{backend}",
                        metric_name=name,
                        metric_value=value,
                    )

    def alarm(
        name: str,
        metric: cloudwatch.IMetric,
        threshold: float,
        periods: int = 1,
        *,
        missing: cloudwatch.TreatMissingData = cloudwatch.TreatMissingData.NOT_BREACHING,
        comparison: cloudwatch.ComparisonOperator = (
            cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
        ),
        description: str,
    ) -> None:
        resource = cloudwatch.Alarm(
            scope,
            f"{name}Alarm",
            alarm_name=f"{prefix}-{name}",
            alarm_description=description,
            metric=metric,
            threshold=threshold,
            evaluation_periods=periods,
            datapoints_to_alarm=periods,
            treat_missing_data=missing,
            comparison_operator=comparison,
        )
        # SNS topics belong to the operator. No topic/subscription/message is created here.
        cfn_alarm = resource.node.default_child
        assert isinstance(cfn_alarm, cloudwatch.CfnAlarm)
        if alarm_actions:
            cfn_alarm.alarm_actions = list(alarm_actions)
        Tags.of(resource).add("project", "semsearch")

    for operation, name in function_names.items():
        logged_operation = {"facade": "search_route", "fallback": "search_fallback"}.get(
            operation, operation
        )
        metric_filter(
            f"{operation}-count",
            operation,
            f'{{ $.operation = "{logged_operation}" && $.duration_ms = * }}',
        )
        metric_filter(
            f"{operation}-internal-errors",
            operation,
            (
                '{ $.operation = "search_route" && $.outcome = "error" }'
                if operation == "facade"
                else f'{{ $.operation = "{logged_operation}" && '
                '($.status = "internal failure" || $.status = "index not ready") }'
            ),
        )
        alarm(
            f"{operation}-lambda-errors",
            cloudwatch.Metric(
                namespace="AWS/Lambda",
                metric_name="Errors",
                dimensions_map={"FunctionName": name},
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            0,
            description=(
                "Lambda invocation errors, including timeouts/OOM/import errors with no final log. "
                "Also includes deliberate input/authentication rejections; classify using logs."
            ),
        )
        alarm(
            f"{operation}-throttles",
            cloudwatch.Metric(
                namespace="AWS/Lambda",
                metric_name="Throttles",
                dimensions_map={"FunctionName": name},
                statistic="Sum",
                period=Duration.minutes(1),
            ),
            0,
            description="Lambda throttles; check approved account and reserved concurrency.",
        )
        error_rate = cloudwatch.MathExpression(
            expression="IF(calls > 0, 100 * FILL(errors, 0) / calls, 0)",
            using_metrics={
                "errors": cloudwatch.Metric(
                    namespace=namespace, metric_name=f"{operation}-internal-errors", statistic="Sum"
                ),
                "calls": cloudwatch.Metric(
                    namespace=namespace, metric_name=f"{operation}-count", statistic="Sum"
                ),
            },
            period=Duration.minutes(5),
        )
        alarm(
            f"{operation}-internal-error-rate",
            error_rate,
            1,
            description=(
                "Internal service failures exceed 1 percent in 5 minutes; "
                "authentication errors are separate."
            ),
        )

    if ecs_log_group is not None:
        for backend in ("ecs", "fallback"):
            backend_namespace = f"{namespace}/{backend}"
            alarm(
                f"{backend}-search-internal-error-rate",
                cloudwatch.MathExpression(
                    expression="IF(calls > 0, 100 * FILL(errors, 0) / calls, 0)",
                    using_metrics={
                        "calls": cloudwatch.Metric(
                            namespace=backend_namespace,
                            metric_name="search-count",
                            statistic="Sum",
                        ),
                        "errors": cloudwatch.Metric(
                            namespace=backend_namespace,
                            metric_name="search-internal-errors",
                            statistic="Sum",
                        ),
                    },
                    period=Duration.minutes(5),
                ),
                1,
                description=(
                    f"{backend} Search internal failures exceed 1 percent in five minutes; "
                    "inspect this backend's Search logs and route attempts."
                ),
            )
            for name in ("search-degraded", "vector-failed"):
                alarm(
                    f"{backend}-{name}",
                    cloudwatch.Metric(
                        namespace=backend_namespace,
                        metric_name=name,
                        statistic="Sum",
                        period=Duration.minutes(1),
                    ),
                    0,
                    description=(
                        f"Any {backend} {name} event; inspect backend quality, "
                        "embedding diagnostics and the completed Search result."
                    ),
                )

    if "facade" in log_groups:
        alarm(
            "facade-execution-p95",
            cloudwatch.Metric(
                namespace="AWS/Lambda",
                metric_name="Duration",
                dimensions_map={"FunctionName": function_names["facade"]},
                statistic="p95",
                period=Duration.minutes(1),
            ),
            4000,
            periods=2,
            description=(
                "Search facade p95 exceeds 4 seconds for two minutes; "
                "inspect route/attempt/OAuth logs, fallback and cold starts."
            ),
        )
        for name, pattern in (
            ("route-fallback", '{ $.operation = "search_route" && $.backend = "fallback" }'),
            ("route-failed", '{ $.operation = "search_route" && $.outcome = "error" }'),
        ):
            metric_filter(name, "facade", pattern)
            alarm(
                name,
                cloudwatch.Metric(
                    namespace=namespace,
                    metric_name=name,
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
                0,
                description="Search routing switched to fallback or failed; inspect route logs.",
            )

    for mode in ("bm25", "rrf", "vector", "vector_bm25_fallback", "rerank"):
        metric_filter(
            f"retrieval-{mode}",
            "search",
            f'{{ $.operation = "search" && $.ranking_mode = "{mode}" }}',
        )
    metric_filter(
        "search-unranked", "search", '{ $.operation = "search" && $.ranking_mode IS NULL }'
    )
    for field in ("empty_result", "below_threshold", "degraded", "lambda_deadline_ignored"):
        metric_filter(
            f"search-{field.replace('_', '-')}",
            "search",
            f'{{ $.operation = "search" && $.{field} IS TRUE }}',
        )
    from typing import get_args

    from semantic_core.providers import FailureKind

    # 固定类别只累加事件数，供应方状态及头部数值不进入指标值或维度。
    for kind in get_args(FailureKind):
        metric_filter(
            f"embedding-error-{kind.replace('_', '-')}",
            "search",
            f'{{ $.operation = "search" && $.embedding_error.kind = "{kind}" }}',
        )
    for state in ("applied", "disabled", "skipped", "failed"):
        metric_filter(
            f"rerank-{state}",
            "search",
            f'{{ $.operation = "search" && $.rerank_state = "{state}" }}',
        )
    for state in ("available", "disabled", "unavailable", "failed", "not_attempted"):
        metric_filter(
            f"vector-{state}",
            "search",
            f'{{ $.operation = "search" && $.vector_availability = "{state}" }}',
        )
    for mode in ("rrf", "vector", "vector_bm25_fallback"):
        metric_filter(
            f"fusion-{mode}",
            "search",
            f'{{ $.operation = "search" && $.configured_fusion = "{mode}" }}',
        )
    metric_filter(
        "poll-success",
        "indexer",
        '{ $.operation = "indexer" && ($.status = "published" || $.status = "reconciled") }',
    )
    for field in COUNTER_FIELDS:
        metric_filter(
            f"indexer-{field.replace('_', '-')}",
            "indexer",
            f'{{ $.operation = "indexer" && $.{field} = * }}',
            f"$.{field}",
        )
    for operation in ("indexer", "enhancer"):
        metric_filter(
            f"{operation}-expired",
            operation,
            f'{{ $.operation = "{operation}" && $.status = "expired" }}',
        )
        alarm(
            f"{operation}-async-dropped",
            cloudwatch.Metric(
                namespace="AWS/Lambda",
                metric_name="AsyncEventsDropped",
                dimensions_map={"FunctionName": function_names[operation]},
                statistic="Sum",
                period=Duration.minutes(1),
            ),
            0,
            description=(
                "An asynchronous tick was dropped; subsequent full polls recover missed work."
            ),
        )
    for minutes in (3, 10):
        alarm(
            f"poll-missing-{minutes}m",
            cloudwatch.Metric(
                namespace=namespace,
                metric_name="poll-success",
                statistic="Sum",
                period=Duration.minutes(1),
            ),
            1,
            minutes,
            missing=cloudwatch.TreatMissingData.BREACHING,
            comparison=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            description=(
                f"No successful full reconciliation for {minutes} consecutive minutes. "
                "Poll heartbeat does not measure catalog freshness."
            ),
        )
    metric_filter(
        "rate-limited", "request", '{ $.operation = "request" && $.status = "rate limited" }'
    )
    metric_filter(
        "authentication-failed",
        "request",
        '{ $.operation = "request" && $.status = "unauthenticated" }',
    )
    metric_filter(
        "request-auth-recovered",
        "request",
        '{ $.operation = "request" && $.auth_recovered IS TRUE }',
    )
    alarm(
        "request-auth-recovered",
        cloudwatch.Metric(
            namespace=namespace,
            metric_name="request-auth-recovered",
            statistic="Sum",
            period=Duration.minutes(5),
        ),
        2,
        description=(
            "At least three REQUEST invocations recovered an OIDC metadata GET in 5 minutes; "
            "the request may still be rejected later. Inspect final status and stage timings."
        ),
    )
    for name, pattern, threshold, description in (
        (
            "request-init-auth-recovered",
            '{ $.operation = "request_init" && $.oidc_prefetch.auth_recovered IS TRUE }',
            2,
            "At least three REQUEST Init prefetches recovered an OIDC metadata GET in 5 minutes; "
            "metadata prefetch completed. Inspect attempt events and stage timings.",
        ),
        (
            "request-init-oidc-prefetch-failed",
            '{ $.operation = "request_init" && $.oidc_prefetch.status = "failed" }',
            0,
            "REQUEST Init completed without a warm OIDC cache; inspect prefetch failure_kind "
            "and auth_failure. The first invocation will retry metadata loading.",
        ),
    ):
        metric_filter(name, "request", pattern)
        alarm(
            name,
            cloudwatch.Metric(
                namespace=namespace,
                metric_name=name,
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold,
            description=description,
        )
    metric_filter(
        "enhancement-paused", "enhancer", '{ $.operation = "enhancer" && $.status = "paused" }'
    )
    alarm(
        "search-execution-p95",
        cloudwatch.Metric(
            namespace="AWS/Lambda",
            metric_name="Duration",
            dimensions_map={"FunctionName": function_names["search"]},
            statistic="p95",
            period=Duration.minutes(5),
        ),
        2000,
        description=(
            "Search Lambda execution p95 exceeds 2 seconds; excludes Gateway time "
            "and does not distinguish warm/cold/refresh."
        ),
    )

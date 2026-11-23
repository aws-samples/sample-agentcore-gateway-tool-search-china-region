"""Offline endpoint, retained-resource and service-capacity deployment guards.

Inputs are explicitly captured non-secret metadata. This module never contacts
AWS, reads credentials, creates DNS records or changes/deletes cloud resources.
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from infra.config import DeploymentConfig
from infra.live_evidence import (
    bound_response as _bound_response,
)
from infra.live_evidence import (
    decode_json,
    fresh,
    validate_live_evidence,
)
from infra.live_evidence import (
    object_value as _object,
)
from infra.live_evidence import (
    rows as _rows,
)
from infra.live_evidence import (
    text as _text,
)
from infra.live_evidence import (
    timestamp as _time,
)

JsonObject = dict[str, Any]
_FIXED_NAMES = {
    "AWS::Logs::LogGroup": "LogGroupName",
    "AWS::DynamoDB::Table": "TableName",
    "AWS::S3::Bucket": "BucketName",
}
_PHYSICAL_IDENTITIES = {
    **{kind: (name,) for kind, name in _FIXED_NAMES.items()},
    "AWS::Lambda::Function": ("FunctionName",),
    "AWS::Lambda::Alias": ("Name", "FunctionName"),
    "AWS::IAM::Role": ("RoleName", "Path"),
    "AWS::IAM::Policy": ("PolicyName",),
    "AWS::ECS::Cluster": ("ClusterName",),
    "AWS::ECS::Service": ("ServiceName", "Cluster"),
    "AWS::ECS::TaskDefinition": ("Family",),
    "AWS::ElasticLoadBalancingV2::LoadBalancer": ("Name",),
    "AWS::ElasticLoadBalancingV2::TargetGroup": ("Name",),
    "AWS::EC2::SecurityGroup": ("GroupName", "VpcId"),
    "AWS::SecretsManager::Secret": ("Name",),
    "AWS::ECR::Repository": ("RepositoryName",),
    "AWS::SNS::Topic": ("TopicName",),
    "AWS::CloudWatch::Alarm": ("AlarmName",),
    "AWS::Logs::MetricFilter": ("FilterName", "LogGroupName"),
}
_REMOVABLE_DIAGNOSTICS = frozenset(
    {
        "search-unranked",
        "search-empty-result",
        "search-below-threshold",
        "search-lambda-deadline-ignored",
        *(
            f"retrieval-{mode}"
            for mode in ("bm25", "rrf", "vector", "vector_bm25_fallback", "rerank")
        ),
        *(
            f"embedding-error-{kind}"
            for kind in (
                "http-rate-limited",
                "http-client-error",
                "http-server-error",
                "http-status-error",
                "connect-timeout",
                "read-timeout",
                "write-timeout",
                "pool-timeout",
                "timeout",
                "deadline-exceeded",
                "network-error",
                "response-parse",
                "authentication",
                "invalid-request",
                "unavailable",
                "rate-limited",
                "unknown",
            )
        ),
        *(f"rerank-{state}" for state in ("applied", "disabled", "skipped", "failed")),
        *(f"vector-{state}" for state in ("available", "disabled", "unavailable", "not_attempted")),
        *(f"fusion-{mode}" for mode in ("rrf", "vector", "vector_bm25_fallback")),
    }
)


def _dns(value: object) -> str:
    name = _text(value, "DNS name").rstrip(".").lower()
    if len(name) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in name.split(".")
    ):
        raise ValueError("DNS name is invalid")
    return name


def _zone_id(value: object) -> str:
    result = _text(value, "hosted zone ID").removeprefix("/hostedzone/")
    if not re.fullmatch(r"Z[A-Z0-9]+", result):
        raise ValueError("hosted zone ID is invalid")
    return result


def _certificate_matches(hostname: str, value: object) -> bool:
    name = _text(value, "certificate SAN").lower()
    if name.startswith("*."):
        suffix = _dns(name[2:])
        return hostname.split(".", 1)[1:] == [suffix]
    return hostname == _dns(name)


def check_endpoint(
    config: DeploymentConfig,
    evidence: JsonObject,
    *,
    hosted_zone_id: str,
    load_balancer_arn: str | None = None,
    phase: str = "activate",
    live_evidence: JsonObject | None = None,
    expected_stack_id: str | None = None,
    now: datetime | None = None,
) -> JsonObject:
    """Verify a certificate/listener/DNS binding using metadata at most 15 minutes old.

    READY means only this endpoint preflight passed. It is not permission to deploy
    or switch Gateway, and does not replace the post-deployment network/health checks.
    DNS absence emits a CREATE-only plan and ready=False; conflicting DNS fails.
    """
    ecs = config.ecs_search
    if ecs is None:
        raise ValueError("endpoint preflight requires ecs_search configuration")
    evidence = _object(evidence, "endpoint evidence")
    current = fresh(evidence, now)
    if phase not in {"predeploy", "activate"}:
        raise ValueError("endpoint phase must be predeploy or activate")
    if evidence.get("account") != config.account or evidence.get("region") != config.region:
        raise ValueError("metadata account/region does not match configuration")
    zone_id = _zone_id(hosted_zone_id)
    hostname = _dns(ecs.hostname)
    certificate = _object(
        _bound_response(evidence, "certificate", {"CertificateArn": ecs.certificate_arn}).get(
            "Certificate"
        ),
        "certificate",
    )
    if certificate.get("CertificateArn") != ecs.certificate_arn:
        raise ValueError("certificate ARN does not match configuration")
    if certificate.get("Status") != "ISSUED":
        raise ValueError("certificate must be ISSUED")
    if not _time(certificate.get("NotBefore")) <= current < _time(certificate.get("NotAfter")):
        raise ValueError("certificate is not currently valid")
    sans = certificate.get("SubjectAlternativeNames")
    if not isinstance(sans, list) or not any(_certificate_matches(hostname, san) for san in sans):
        raise ValueError("certificate SAN does not cover the configured hostname")

    zone_response = _bound_response(evidence, "hosted_zone", {"Id": zone_id})
    zone = _object(zone_response.get("HostedZone"), "hosted zone")
    zone_name = _dns(zone.get("Name"))
    if _zone_id(zone.get("Id")) != zone_id or not (
        hostname == zone_name or hostname.endswith("." + zone_name)
    ):
        raise ValueError("DNS hostname must be within the expected hosted zone")
    private = _object(zone.get("Config"), "hosted zone config").get("PrivateZone")
    if type(private) is not bool:
        raise ValueError("hosted zone privacy must be explicit")
    if phase == "predeploy":
        return {
            "status": "PREDEPLOY_CHECKED",
            "passed": True,
            "ready": False,
            "scope": "ACM validity, hostname coverage and hosted zone ownership only",
            "pending": [
                "Fresh stack-bound activation capture after deployment",
                "NLB listener, VPC zone association and exact DNS alias",
                "Separate network, task health and change-set review",
            ],
            "captured_at": evidence["captured_at"],
            "hostname": hostname,
            "certificate_arn": ecs.certificate_arn,
            "hosted_zone_id": zone_id,
        }
    capture = _object(live_evidence, "live stack evidence")
    template = _object(capture.get("get_template"), "get_template").get("response")
    template = _object(template, "get_template response").get("TemplateBody")
    if isinstance(template, str):
        template = decode_json(template)
    live = validate_live_evidence(template, capture, expected_stack_id, now=current)
    if live.account != config.account or live.region != config.region:
        raise ValueError("live stack does not match the endpoint account and region")
    load_balancer_arn = _text(load_balancer_arn, "NLB ARN")
    expected_arn_prefix = (
        f"arn:aws-cn:elasticloadbalancing:{config.region}:{config.account}:loadbalancer/net/"
    )
    if not load_balancer_arn.startswith(expected_arn_prefix):
        raise ValueError("NLB ARN must match the configured account and region")
    matches = [
        row
        for row in live.inventory.values()
        if row["ResourceType"] == "AWS::ElasticLoadBalancingV2::LoadBalancer"
        and row["PhysicalResourceId"] == load_balancer_arn
    ]
    if len(matches) != 1:
        raise ValueError("NLB is not the exact resource in the captured stack inventory")
    if live.outputs.get("SearchEcsEndpoint") != f"https://{hostname}/search":
        raise ValueError("stack endpoint output does not match the configured hostname")
    balancer_response = _bound_response(
        evidence, "load_balancer", {"LoadBalancerArns": [load_balancer_arn]}
    )
    balancers = _rows(balancer_response.get("LoadBalancers"), "load balancers")
    if len(balancers) != 1 or balancer_response.get("NextMarker"):
        raise ValueError("expected exactly one complete NLB description")
    balancer = balancers[0]
    if (
        balancer.get("LoadBalancerArn") != load_balancer_arn
        or balancer.get("Type") != "network"
        or balancer.get("Scheme") != "internal"
        or _object(balancer.get("State"), "NLB state").get("Code") != "active"
    ):
        raise ValueError("NLB must be the expected active internal network load balancer")
    if _dns(live.outputs.get("SearchNlbDnsName")) != _dns(balancer.get("DNSName")) or _zone_id(
        live.outputs.get("SearchNlbCanonicalHostedZoneId")
    ) != _zone_id(balancer.get("CanonicalHostedZoneId")):
        raise ValueError("stack NLB outputs do not match the captured NLB")
    vpc_id = _text(balancer.get("VpcId"), "NLB VPC")
    listener_response = _bound_response(
        evidence, "listeners", {"LoadBalancerArn": load_balancer_arn}
    )
    listeners = _rows(listener_response.get("Listeners"), "listeners")
    if (
        listener_response.get("NextMarker")
        or len(listeners) != 1
        or listeners[0].get("LoadBalancerArn") != load_balancer_arn
        or listeners[0].get("Port") != 443
        or listeners[0].get("Protocol") != "TLS"
        or listeners[0].get("Certificates") != [{"CertificateArn": ecs.certificate_arn}]
    ):
        raise ValueError("NLB listener must use the verified certificate on TLS port 443")

    if private and not any(
        row.get("VPCId") == vpc_id and row.get("VPCRegion") == config.region
        for row in _rows(zone_response.get("VPCs", []), "zone VPCs")
    ):
        raise ValueError("private hosted zone must be associated with the NLB VPC")
    records_response = _bound_response(evidence, "record_sets", {"HostedZoneId": zone_id})
    if records_response.get("IsTruncated") is not False or records_response.get("NextRecordName"):
        raise ValueError("DNS inventory must be complete, including all pagination")
    records = _rows(records_response.get("ResourceRecordSets"), "DNS records")
    matching = [
        row
        for row in records
        if _text(row.get("Name"), "DNS record name").rstrip(".").lower() == hostname
    ]
    alias = {
        "Name": hostname + ".",
        "Type": "A",
        "AliasTarget": {
            "DNSName": _dns(balancer.get("DNSName")) + ".",
            "HostedZoneId": _zone_id(balancer.get("CanonicalHostedZoneId")),
            "EvaluateTargetHealth": True,
        },
    }
    # Certificate-validation records live below the endpoint name and do not match.
    if matching:
        actual = deepcopy(matching[0])
        actual["Name"] = _dns(actual.get("Name")) + "."
        target = _object(actual.get("AliasTarget"), "DNS alias")
        target["DNSName"] = _dns(target.get("DNSName")) + "."
        if len(matching) != 1 or actual != alias:
            raise ValueError("existing DNS must be the exact simple A alias to this NLB")
    return {
        "status": "READY" if matching else "DNS_CREATE_REQUIRED",
        "ready": bool(matching),
        "passed": bool(matching),
        "scope": "certificate, TLS listener and DNS endpoint only",
        "captured_at": evidence["captured_at"],
        "hostname": hostname,
        "certificate_arn": ecs.certificate_arn,
        "load_balancer_arn": load_balancer_arn,
        "hosted_zone_id": zone_id,
        "stack_id": live.stack_id,
        "dns_change": None if matching else {"Action": "CREATE", "ResourceRecordSet": alias},
    }


def prepare_update_overlay(
    live: JsonObject,
    replacements: JsonObject,
    *,
    allowed_ids: set[str],
    live_evidence: JsonObject | None = None,
    expected_stack_id: str | None = None,
    remove_metric_filters: set[str] | None = None,
    output_additions: JsonObject | None = None,
    allowed_output_ids: set[str] | None = None,
    now: datetime | None = None,
) -> JsonObject:
    """Preserve the complete captured live template and replace only explicitly allowed nodes.

    Every existing ECS service must be explicitly selected: DesiredCount is omitted
    on updates, so CloudFormation does not reset autoscaling. New services require
    DesiredCount >= 2. Separately allowed outputs may be added, never replaced or
    removed. Their values are verified against a new capture after deployment.
    This is an offline preparation step, not a deploy operation.
    """
    live = _object(live, "live template")
    bound = validate_live_evidence(live, live_evidence, expected_stack_id, now=now)
    old = _object(live.get("Resources"), "live resources")
    replacements = _object(replacements, "replacement resources")
    if not isinstance(allowed_ids, set) or any(not isinstance(key, str) for key in allowed_ids):
        raise ValueError("allowed resource IDs must be a set of strings")
    if set(replacements) - allowed_ids:
        raise ValueError("replacement resource is outside the explicit allow list")
    removals = set() if remove_metric_filters is None else remove_metric_filters
    if not isinstance(removals, set) or any(not isinstance(key, str) for key in removals):
        raise ValueError("removed metric filter IDs must be a set of strings")
    if removals - allowed_ids or removals & set(replacements):
        raise ValueError("metric filter removal must be exclusively and explicitly allowed")
    for name in removals:
        resource = _object(old.get(name), "removed metric filter")
        properties = _object(resource.get("Properties"), "removed metric filter properties")
        transforms = _rows(properties.get("MetricTransformations"), "metric transformations")
        if (
            resource.get("Type") != "AWS::Logs::MetricFilter"
            or len(transforms) != 1
            or _text(transforms[0].get("MetricName"), "metric name") not in _REMOVABLE_DIAGNOSTICS
        ):
            raise ValueError("only authorized extra backend metric filters may be removed")
        metric_name = transforms[0]["MetricName"]
        filter_name = _text(properties.get("FilterName"), "metric filter name")
        source = _object(properties.get("LogGroupName"), "metric filter log source")
        source_id = _text(source.get("Ref"), "metric filter log reference")
        group = _object(old.get(source_id), "metric filter log group")
        group_name = _object(group.get("Properties"), "log group properties").get("LogGroupName")
        authorized = False
        for backend, log_suffix in (("ecs", "ecs-search-logs"), ("fallback", "fallback-logs")):
            suffix = f"-{metric_name}-{backend}"
            prefix = filter_name.removesuffix(suffix)
            if filter_name == prefix or not prefix:
                continue
            expected_group = f"{prefix}-{log_suffix}"
            if (
                set(source) == {"Ref"}
                and group.get("Type") == "AWS::Logs::LogGroup"
                and group_name == expected_group
                and bound.inventory[source_id]["PhysicalResourceId"] == expected_group
                and transforms[0].get("MetricNamespace")
                in {f"semsearch/{prefix}", f"semsearch/{prefix}/{backend}"}
            ):
                authorized = True
        if not authorized:
            raise ValueError("metric filter source is outside the authorized backend logs")
    for name, resource in old.items():
        if _object(resource, name).get("Type") == "AWS::ECS::Service" and name not in replacements:
            raise ValueError(
                f"existing ECS service {name} requires an explicit capacity-safe overlay"
            )
    additions = {} if output_additions is None else _object(output_additions, "output additions")
    output_ids = set() if allowed_output_ids is None else allowed_output_ids
    if not isinstance(output_ids, set) or any(not isinstance(key, str) for key in output_ids):
        raise ValueError("allowed output IDs must be a set of strings")
    if set(additions) - output_ids:
        raise ValueError("output addition is outside the explicit allow list")
    old_outputs = _object(live.get("Outputs", {}), "live outputs")
    for name, value in additions.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", name):
            raise ValueError("output ID must be an alphanumeric CloudFormation logical ID")
        if name in old_outputs:
            raise ValueError(f"existing output cannot be replaced: {name}")
        if "Value" not in _object(value, "output definition"):
            raise ValueError(f"output definition requires Value: {name}")
    result = deepcopy(live)
    if additions:
        result.setdefault("Outputs", {}).update(deepcopy(additions))
    for name in removals:
        del result["Resources"][name]
    for name, value in replacements.items():
        resource = deepcopy(_object(value, name))
        kind = _text(resource.get("Type"), "resource type")
        if name in old and kind != _object(old[name], name).get("Type"):
            raise ValueError(f"cannot change existing resource type: {name}")
        properties = _object(resource.get("Properties"), name + " properties")
        if name in old:
            previous = _object(old[name], name)
            old_properties = _object(previous.get("Properties"), name + " live properties")
            for key in ("DeletionPolicy", "UpdateReplacePolicy"):
                if resource.get(key) != previous.get(key):
                    raise ValueError(f"existing resource retention must be preserved: {name}")
            for key in _PHYSICAL_IDENTITIES.get(kind, ()):
                if properties.get(key) != old_properties.get(key):
                    raise ValueError(f"existing physical identity must be preserved: {name}.{key}")
        if kind == "AWS::ECS::Service":
            network = _object(properties.get("NetworkConfiguration", {}), "service network")
            awsvpc = _object(network.get("AwsvpcConfiguration", {}), "service awsvpc")
            if awsvpc.get("AssignPublicIp") == "ENABLED":
                raise ValueError("public task IPs are not allowed in the deployment overlay")
            if name in old:
                properties.pop("DesiredCount", None)
            elif type(properties.get("DesiredCount")) is not int or properties["DesiredCount"] < 2:
                raise ValueError("a new ECS service must explicitly start with at least two tasks")
        result["Resources"][name] = resource
    return result


def retained_resource_recovery(
    live: JsonObject,
    candidate: JsonObject,
    inventory: list[JsonObject],
    *,
    live_evidence: JsonObject | None = None,
    expected_stack_id: str | None = None,
    now: datetime | None = None,
) -> JsonObject:
    """Plan an IMPORT-only recovery for observed fixed-name retained-resource collisions.

    Inventory Properties must be normalized CloudFormation properties, independently
    checked against captured metadata. Missing inventory is never evidence of absence.
    The destructive branch is instructions gated on separate explicit authorization.
    """
    live = _object(live, "live template")
    candidate = _object(candidate, "candidate template")
    bound = validate_live_evidence(live, live_evidence, expected_stack_id, now=now)
    old = _object(live.get("Resources"), "live resources")
    new = _object(candidate.get("Resources"), "candidate resources")
    inventory = _rows(inventory, "retained resource inventory")
    for row in inventory:
        _text(row.get("Type"), "inventory type")
        _text(row.get("PhysicalId"), "inventory physical ID")
        _object(row.get("Properties"), "inventory properties")
    managed: set[tuple[str, str]] = set()
    for value in old.values():
        row = _object(value, "live resource")
        kind = _text(row.get("Type"), "live resource type")
        if kind in _FIXED_NAMES:
            properties = _object(row.get("Properties"), "live resource properties")
            name_value = properties.get(_FIXED_NAMES[kind])
            if name_value is not None:
                managed.add((kind, _text(bound.resolve(name_value), "physical name")))
    imports: list[JsonObject] = []
    import_template = deepcopy(live)
    for logical_id, raw in new.items():
        resource = _object(raw, logical_id)
        kind = _text(resource.get("Type"), "candidate resource type")
        if logical_id in old:
            continue
        if kind not in _FIXED_NAMES or resource.get("DeletionPolicy") != "Retain":
            continue
        name_property = _FIXED_NAMES[kind]
        properties = _object(resource.get("Properties"), "retained resource properties")
        if name_property not in properties:
            continue  # An automatically generated name cannot cause a fixed-name collision.
        resolved = _object(bound.resolve(properties), "resolved retained properties")
        physical_id = _text(resolved.get(name_property), "retained resource physical name")
        if (kind, physical_id) in managed:
            raise ValueError("physical resource is already managed under another logical ID")
        matches = [
            row
            for row in inventory
            if row.get("Type") == kind and row.get("PhysicalId") == physical_id
        ]
        if not matches:
            continue
        if len(matches) != 1 or matches[0].get("Properties") != resolved:
            raise ValueError("retained resource properties require independent reconciliation")
        import_template["Resources"][logical_id] = deepcopy(resource)
        imports.append(
            {
                "ResourceType": kind,
                "LogicalResourceId": logical_id,
                "ResourceIdentifier": {name_property: physical_id},
            }
        )
    return {
        "status": "IMPORT_REQUIRED" if imports else "NO_OBSERVED_COLLISIONS",
        "ready": False,
        "resources_to_import": imports,
        "import_template": import_template if imports else None,
        "steps": [
            "Verify resource ownership and reconcile captured physical properties.",
            "Review an IMPORT change set containing only the listed imports.",
            "Wait for IMPORT_COMPLETE and capture the new full live template.",
            "Prepare a separate update overlay from that captured template.",
        ],
        "destructive_alternative": {
            "requires_explicit_approval": True,
            "steps": [
                "Approve deletion of the exact retained resource and its data.",
                "Disable DynamoDB deletion protection only after approval.",
                "Verify protection is disabled, then delete only the approved resource.",
                "Wait for confirmed absence before retrying resource creation.",
            ],
        },
    }


def load_json(path: Path) -> JsonObject:
    """Reject duplicate keys and non-JSON numeric constants in deployment evidence."""

    return decode_json(path.read_text())

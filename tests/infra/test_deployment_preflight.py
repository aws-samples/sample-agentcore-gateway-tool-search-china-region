"""Offline deployment guards use bound metadata, never credentials or SDK calls."""

import json
import subprocess  # nosec B404
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_ecs_production import configuration

NOW = datetime(2026, 10, 15, 8, tzinfo=UTC)
ZONE = "Z0123456789"


def evidence():
    config = configuration()
    arn = (
        f"arn:aws-cn:elasticloadbalancing:{config.region}:{config.account}:"
        "loadbalancer/net/search/0123456789abcdef"
    )
    certificate = config.ecs_search.certificate_arn
    alias = {
        "Name": config.ecs_search.hostname + ".",
        "Type": "A",
        "AliasTarget": {
            "DNSName": "internal-search.elb.cn-north-1.amazonaws.com.cn.",
            "HostedZoneId": "Z0987654321",
            "EvaluateTargetHealth": True,
        },
    }
    captured = {
        "captured_at": NOW.isoformat(),
        "account": config.account,
        "region": config.region,
        "certificate": {
            "request": {"CertificateArn": certificate},
            "response": {
                "Certificate": {
                    "CertificateArn": certificate,
                    "Status": "ISSUED",
                    "NotBefore": "2026-10-01T00:00:00+00:00",
                    "NotAfter": "2027-10-01T00:00:00+00:00",
                    "SubjectAlternativeNames": [config.ecs_search.hostname],
                }
            },
        },
        "hosted_zone": {
            "request": {"Id": ZONE},
            "response": {
                "HostedZone": {
                    "Id": "/hostedzone/" + ZONE,
                    "Name": "example.invalid.",
                    "Config": {"PrivateZone": False},
                }
            },
        },
        "record_sets": {
            "request": {"HostedZoneId": ZONE},
            "response": {"ResourceRecordSets": [alias], "IsTruncated": False},
        },
        "load_balancer": {
            "request": {"LoadBalancerArns": [arn]},
            "response": {
                "LoadBalancers": [
                    {
                        "LoadBalancerArn": arn,
                        "Type": "network",
                        "Scheme": "internal",
                        "State": {"Code": "active"},
                        "DNSName": alias["AliasTarget"]["DNSName"].rstrip("."),
                        "CanonicalHostedZoneId": alias["AliasTarget"]["HostedZoneId"],
                        "VpcId": "vpc-0123456789abcdef0",
                    }
                ]
            },
        },
        "listeners": {
            "request": {"LoadBalancerArn": arn},
            "response": {
                "Listeners": [
                    {
                        "LoadBalancerArn": arn,
                        "Port": 443,
                        "Protocol": "TLS",
                        "Certificates": [{"CertificateArn": certificate}],
                    }
                ]
            },
        },
    }
    return config, arn, captured


def check(captured=None):
    from infra.deployment_preflight import check_endpoint

    config, arn, original = evidence()
    return check_endpoint(
        config,
        captured or original,
        hosted_zone_id=ZONE,
        load_balancer_arn=arn,
        **endpoint_stack_binding(config, arn),
    )


def bound_kwargs(template):
    from test_deployment_evidence_binding import STACK_ID, bound

    return {"live_evidence": bound(template), "expected_stack_id": STACK_ID, "now": NOW}


def endpoint_stack_binding(config, arn):
    template = live_template()
    template["Resources"]["LoadBalancer"] = {
        "Type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "Properties": {"Scheme": "internal", "Type": "network"},
    }
    kwargs = bound_kwargs(template)
    capture = kwargs["live_evidence"]
    for row in capture["list_stack_resources"]["pages"][0]["response"]["StackResourceSummaries"]:
        if row["LogicalResourceId"] == "LoadBalancer":
            row["PhysicalResourceId"] = arn
    capture["describe_stacks"]["response"]["Stacks"][0]["Outputs"] = [
        {
            "OutputKey": "SearchEcsEndpoint",
            "OutputValue": f"https://{config.ecs_search.hostname}/search",
        },
        {
            "OutputKey": "SearchNlbDnsName",
            "OutputValue": "internal-search.elb.cn-north-1.amazonaws.com.cn",
        },
        {"OutputKey": "SearchNlbCanonicalHostedZoneId", "OutputValue": "Z0987654321"},
    ]
    return kwargs


def test_issued_certificate_exact_alias_and_bound_scope_are_ready():
    assert check()["status"] == "READY"


@pytest.mark.parametrize(
    ("section", "mutation"),
    [
        ("certificate", ("Status", "PENDING_VALIDATION")),
        ("certificate", ("CertificateArn", "arn:wrong")),
        ("certificate", ("NotBefore", "2026-11-01T00:00:00Z")),
        ("certificate", ("NotAfter", "2026-10-14T00:00:00Z")),
        ("certificate", ("SubjectAlternativeNames", ["other.example.invalid"])),
        ("certificate", ("SubjectAlternativeNames", ["*.invalid"])),
        ("load_balancer", ("Scheme", "internet-facing")),
        ("load_balancer", ("Type", "application")),
        ("load_balancer", ("State", {"Code": "provisioning"})),
    ],
)
def test_bad_certificate_or_nlb_is_rejected(section, mutation):
    _, _, captured = evidence()
    row = captured[section]["response"]
    row = row["Certificate"] if section == "certificate" else row["LoadBalancers"][0]
    row[mutation[0]] = mutation[1]
    with pytest.raises(ValueError):
        check(captured)


def test_single_label_certificate_wildcard_is_accepted():
    _, _, captured = evidence()
    captured["certificate"]["response"]["Certificate"]["SubjectAlternativeNames"] = [
        "*.example.invalid"
    ]
    assert check(captured)["status"] == "READY"


@pytest.mark.parametrize("section", ["certificate", "hosted_zone", "load_balancer", "listeners"])
def test_evidence_cannot_be_rebound_to_another_request(section):
    _, _, captured = evidence()
    captured[section]["request"] = {"different": "scope"}
    with pytest.raises(ValueError, match="request"):
        check(captured)


def test_missing_dns_produces_create_plan_but_cannot_pass_activation():
    _, _, captured = evidence()
    captured["record_sets"]["response"]["ResourceRecordSets"] = []
    result = check(captured)
    assert result["status"] == "DNS_CREATE_REQUIRED"
    assert result["ready"] is False
    assert result["dns_change"]["Action"] == "CREATE"


@pytest.mark.parametrize(
    "mutation",
    [
        {"Type": "CNAME", "ResourceRecords": [{"Value": "other.invalid."}]},
        {"AliasTarget": {"DNSName": "other.invalid", "HostedZoneId": "Z0"}},
        {"SetIdentifier": "weighted", "Weight": 1},
    ],
)
def test_conflicting_existing_dns_is_never_upserted(mutation):
    _, _, captured = evidence()
    captured["record_sets"]["response"]["ResourceRecordSets"][0].update(mutation)
    with pytest.raises(ValueError, match="DNS"):
        check(captured)


def test_paginated_or_stale_evidence_cannot_pass():
    _, _, captured = evidence()
    captured["record_sets"]["response"]["IsTruncated"] = True
    with pytest.raises(ValueError, match="complete"):
        check(captured)
    captured["record_sets"]["response"]["IsTruncated"] = False
    captured["captured_at"] = "2026-10-15T07:44:00Z"
    with pytest.raises(ValueError, match="fresh"):
        check(captured)


def test_private_zone_must_be_visible_from_nlb_vpc():
    _, _, captured = evidence()
    captured["hosted_zone"]["response"]["HostedZone"]["Config"]["PrivateZone"] = True
    with pytest.raises(ValueError, match="VPC"):
        check(captured)
    captured["hosted_zone"]["response"]["VPCs"] = [
        {"VPCRegion": "cn-north-1", "VPCId": "vpc-0123456789abcdef0"}
    ]
    assert check(captured)["status"] == "READY"


def test_listener_must_use_the_verified_certificate():
    _, _, captured = evidence()
    captured["listeners"]["response"]["Listeners"][0]["Certificates"] = [
        {"CertificateArn": "arn:wrong"}
    ]
    with pytest.raises(ValueError, match="listener"):
        check(captured)


def test_unrelated_acm_validation_records_do_not_invalidate_hosted_zone():
    _, _, captured = evidence()
    captured["record_sets"]["response"]["ResourceRecordSets"].append(
        {
            "Name": "_acm-validation.search.example.invalid.",
            "Type": "CNAME",
            "TTL": 300,
            "ResourceRecords": [{"Value": "_acm.acm-validations.aws."}],
        }
    )
    assert check(captured)["status"] == "READY"


def live_template():
    return {
        "Description": "Captured live deployment",
        "Parameters": {"Original": {"Type": "String", "Default": "preserve"}},
        "Outputs": {"Original": {"Value": "preserve"}},
        "Resources": {
            "OldFunction": {
                "Type": "AWS::Lambda::Function",
                "Properties": {"Code": {"S3Key": "preserve.zip"}, "Timeout": 10},
            },
            "Service": {
                "Type": "AWS::ECS::Service",
                "Properties": {"DesiredCount": 2, "TaskDefinition": "old"},
            },
        },
    }


def test_update_overlay_starts_from_complete_live_and_omits_only_existing_desired_count():
    from infra.deployment_preflight import prepare_update_overlay

    live = live_template()
    before = deepcopy(live)
    updates = {
        "Service": {
            "Type": "AWS::ECS::Service",
            "Properties": {"DesiredCount": 2, "TaskDefinition": "new"},
        }
    }
    candidate = prepare_update_overlay(live, updates, allowed_ids={"Service"}, **bound_kwargs(live))
    assert live == before
    assert candidate["Parameters"] == live["Parameters"]
    assert candidate["Outputs"] == live["Outputs"]
    assert candidate["Resources"]["OldFunction"] == live["Resources"]["OldFunction"]
    assert candidate["Resources"]["Service"]["Properties"] == {"TaskDefinition": "new"}


def test_first_create_keeps_two_tasks_and_unapproved_replacements_fail():
    from infra.deployment_preflight import prepare_update_overlay

    new_service = live_template()["Resources"]["Service"]
    candidate = prepare_update_overlay(
        {"Resources": {}},
        {"Service": new_service},
        allowed_ids={"Service"},
        **bound_kwargs({"Resources": {}}),
    )
    assert candidate["Resources"]["Service"]["Properties"]["DesiredCount"] == 2
    with pytest.raises(ValueError, match="allow"):
        prepare_update_overlay(
            live_template(),
            {"OldFunction": {}},
            allowed_ids={"Service"},
            **bound_kwargs(live_template()),
        )


def test_existing_service_requires_explicit_overlay_even_for_unrelated_updates():
    from infra.deployment_preflight import prepare_update_overlay

    with pytest.raises(ValueError, match="Service"):
        prepare_update_overlay(
            live_template(), {}, allowed_ids=set(), **bound_kwargs(live_template())
        )


def test_overlay_cannot_replace_existing_resource_types():
    from infra.deployment_preflight import prepare_update_overlay

    with pytest.raises(ValueError, match="type"):
        prepare_update_overlay(
            live_template(),
            {"Service": {"Type": "AWS::S3::Bucket", "Properties": {}}},
            allowed_ids={"Service"},
            **bound_kwargs(live_template()),
        )


def retained_table():
    return {
        "Type": "AWS::DynamoDB::Table",
        "DeletionPolicy": "Retain",
        "UpdateReplacePolicy": "Retain",
        "Properties": {
            "TableName": "fixture-replay",
            "DeletionProtectionEnabled": True,
            "BillingMode": "PAY_PER_REQUEST",
            "KeySchema": [{"AttributeName": "key", "KeyType": "HASH"}],
            "AttributeDefinitions": [{"AttributeName": "key", "AttributeType": "S"}],
        },
    }


def test_orphan_recovery_is_import_only_and_preserves_every_live_node():
    from infra.deployment_preflight import retained_resource_recovery

    live = live_template()
    orphan = retained_table()
    candidate = deepcopy(live)
    candidate["Resources"]["Replay"] = orphan
    inventory = [
        {"Type": orphan["Type"], "PhysicalId": "fixture-replay", "Properties": orphan["Properties"]}
    ]
    result = retained_resource_recovery(live, candidate, inventory, **bound_kwargs(live))
    assert result["status"] == "IMPORT_REQUIRED"
    assert result["resources_to_import"] == [
        {
            "ResourceType": "AWS::DynamoDB::Table",
            "LogicalResourceId": "Replay",
            "ResourceIdentifier": {"TableName": "fixture-replay"},
        }
    ]
    for key, row in live["Resources"].items():
        assert result["import_template"]["Resources"][key] == row
    assert result["import_template"]["Resources"]["Replay"] == orphan
    alternative = result["destructive_alternative"]
    assert alternative["requires_explicit_approval"] is True
    assert alternative["steps"][0] == (
        "Approve deletion of the exact retained resource and its data."
    )
    assert "Disable DynamoDB deletion protection only after approval." in alternative["steps"]


def test_orphan_property_mismatch_or_already_managed_identity_refuses_import():
    from infra.deployment_preflight import retained_resource_recovery

    orphan = retained_table()
    candidate = {"Resources": {"Replay": orphan}}
    inventory = [{"Type": orphan["Type"], "PhysicalId": "fixture-replay", "Properties": {}}]
    with pytest.raises(ValueError, match="properties"):
        retained_resource_recovery(
            {"Resources": {}}, candidate, inventory, **bound_kwargs({"Resources": {}})
        )
    inventory[0]["Properties"] = orphan["Properties"]
    with pytest.raises(ValueError, match="managed"):
        retained_resource_recovery(
            {"Resources": {"OldReplay": orphan}},
            candidate,
            inventory,
            **bound_kwargs({"Resources": {"OldReplay": orphan}}),
        )


def test_cli_overlay_and_recovery_remain_offline_and_preserve_live_metadata(tmp_path):
    live = tmp_path / "live.json"
    replacements = tmp_path / "replacements.json"
    capture = tmp_path / "capture.json"
    from test_deployment_evidence_binding import STACK_ID, bound

    metadata = bound(live_template())
    metadata["captured_at"] = datetime.now(UTC).isoformat()
    capture.write_text(json.dumps(metadata))
    output = tmp_path / "candidate.json"
    live.write_text(json.dumps(live_template()))
    replacements.write_text(json.dumps({"Service": live_template()["Resources"]["Service"]}))
    # Fixed local test program and fixture paths; argv is not request-controlled.
    # nosemgrep: dangerous-subprocess-use-audit
    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            "scripts/preflight_ecs_deployment.py",
            "overlay",
            "--live-template",
            str(live),
            "--replacements",
            str(replacements),
            "--live-evidence",
            str(capture),
            "--expected-stack-id",
            STACK_ID,
            "--allow-resource",
            "Service",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "AWS_EC2_METADATA_DISABLED": "true"},
    )
    assert result.returncode == 0, result.stderr
    candidate = json.loads(output.read_text())
    assert candidate["Parameters"] == live_template()["Parameters"]
    assert "DesiredCount" not in candidate["Resources"]["Service"]["Properties"]
    assert not any(
        token in Path("scripts/preflight_ecs_deployment.py").read_text()
        for token in ("import boto", "subprocess", "get_secret")
    )


def test_cli_rejects_duplicate_metadata_before_writing_output(tmp_path):
    source = tmp_path / "ambiguous.json"
    source.write_text('{"Resources": {}, "Resources": {"secret": "hidden"}}')
    output = tmp_path / "candidate.json"
    # Fixed local test program and fixture paths; argv is not request-controlled.
    # nosemgrep: dangerous-subprocess-use-audit
    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            "scripts/preflight_ecs_deployment.py",
            "overlay",
            "--live-template",
            str(source),
            "--replacements",
            str(source),
            "--live-evidence",
            str(source),
            "--expected-stack-id",
            "invalid",
            "--allow-resource",
            "Service",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "duplicate" in result.stderr
    assert not output.exists()

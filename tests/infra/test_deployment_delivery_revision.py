"""Offline deployment guard regressions from the second independent review."""

import json
import subprocess  # nosec B404
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from infra.deployment_preflight import check_endpoint, prepare_update_overlay
from infra.live_evidence import fresh
from test_deployment_evidence_binding import NOW, STACK_ID, bound
from test_deployment_preflight import evidence, live_template


def legacy_endpoint_template():
    config, arn, captured = evidence()
    template = live_template()
    template["Resources"]["LoadBalancer"] = {
        "Type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "Properties": {"Scheme": "internal", "Type": "network"},
    }
    additions = {
        "SearchEcsEndpoint": {"Value": f"https://{config.ecs_search.hostname}/search"},
        "SearchNlbDnsName": {"Value": {"Fn::GetAtt": ["LoadBalancer", "DNSName"]}},
        "SearchNlbCanonicalHostedZoneId": {
            "Value": {"Fn::GetAtt": ["LoadBalancer", "CanonicalHostedZoneID"]}
        },
    }
    return config, arn, captured, template, additions


def overlay(template, additions, allowed):
    return prepare_update_overlay(
        template,
        {"Service": template["Resources"]["Service"]},
        allowed_ids={"Service"},
        output_additions=additions,
        allowed_output_ids=allowed,
        live_evidence=bound(template),
        expected_stack_id=STACK_ID,
        now=NOW,
    )


def test_approved_output_additions_allow_later_stack_bound_activation():
    config, arn, captured, template, additions = legacy_endpoint_template()
    original = deepcopy(template)
    candidate = overlay(template, additions, set(additions))
    assert template == original
    assert candidate["Outputs"] == {**template["Outputs"], **additions}
    assert candidate["Parameters"] == template["Parameters"]
    assert set(candidate["Resources"]) == set(template["Resources"])
    for name in set(template["Resources"]) - {"Service"}:
        assert candidate["Resources"][name] == template["Resources"][name]
    assert "DesiredCount" not in candidate["Resources"]["Service"]["Properties"]

    # Emulate a separate, fresh capture AFTER a reviewed update. The old capture
    # never claims these newly added outputs already exist in the live stack.
    capture = bound(candidate)
    summaries = capture["list_stack_resources"]["pages"][0]["response"]["StackResourceSummaries"]
    next(row for row in summaries if row["LogicalResourceId"] == "LoadBalancer")[
        "PhysicalResourceId"
    ] = arn
    load_balancer = captured["load_balancer"]["response"]["LoadBalancers"][0]
    capture["describe_stacks"]["response"]["Stacks"][0]["Outputs"] = [
        {"OutputKey": "Original", "OutputValue": "preserve"},
        {
            "OutputKey": "SearchEcsEndpoint",
            "OutputValue": additions["SearchEcsEndpoint"]["Value"],
        },
        {"OutputKey": "SearchNlbDnsName", "OutputValue": load_balancer["DNSName"]},
        {
            "OutputKey": "SearchNlbCanonicalHostedZoneId",
            "OutputValue": load_balancer["CanonicalHostedZoneId"],
        },
    ]
    result = check_endpoint(
        config,
        captured,
        hosted_zone_id="Z0123456789",
        load_balancer_arn=arn,
        live_evidence=capture,
        expected_stack_id=STACK_ID,
        now=NOW,
    )
    assert result["ready"] is True


@pytest.mark.parametrize(
    ("additions", "allowed", "message"),
    [
        ({"Unexpected": {"Value": "value"}}, set(), "allow"),
        ({"Original": {"Value": "different"}}, {"Original"}, "existing output"),
        ({"Original": {"Value": "preserve"}}, {"Original"}, "existing output"),
        ({"MissingValue": {}}, {"MissingValue"}, "Value"),
        ({"WrongShape": None}, {"WrongShape"}, "object"),
        ({"Bad-Name": {"Value": "value"}}, {"Bad-Name"}, "output ID"),
    ],
)
def test_output_overlay_is_add_only_and_explicit(additions, allowed, message):
    with pytest.raises(ValueError, match=message):
        overlay(live_template(), additions, allowed)


def test_output_additions_do_not_weaken_original_capture_binding():
    template = live_template()
    capture = bound(template)
    template["Resources"].pop("OldFunction")
    with pytest.raises(ValueError, match="captured template hash"):
        prepare_update_overlay(
            template,
            {"Service": template["Resources"]["Service"]},
            allowed_ids={"Service"},
            output_additions={"New": {"Value": "value"}},
            allowed_output_ids={"New"},
            live_evidence=capture,
            expected_stack_id=STACK_ID,
            now=NOW,
        )


def test_cli_output_overlay_requires_independent_output_allow_list(tmp_path):
    template = live_template()
    capture = bound(template)
    capture["captured_at"] = datetime.now(UTC).isoformat()
    inputs = {
        "live": template,
        "capture": capture,
        "replacements": {"Service": template["Resources"]["Service"]},
        "outputs": {"SearchEcsEndpoint": {"Value": "https://search.example.invalid/search"}},
    }
    for name, value in inputs.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(value))
    output = tmp_path / "candidate.json"
    command = [
        sys.executable,
        "scripts/preflight_ecs_deployment.py",
        "overlay",
        "--live-template",
        str(tmp_path / "live.json"),
        "--replacements",
        str(tmp_path / "replacements.json"),
        "--live-evidence",
        str(tmp_path / "capture.json"),
        "--expected-stack-id",
        STACK_ID,
        "--allow-resource",
        "Service",
        "--output-additions",
        str(tmp_path / "outputs.json"),
        "--output",
        str(output),
    ]
    denied = subprocess.run(command, capture_output=True, text=True)  # nosec B603
    assert denied.returncode == 2 and not output.exists()
    allowed = subprocess.run(  # nosec B603
        [*command, "--allow-output", "SearchEcsEndpoint"], capture_output=True, text=True
    )
    assert allowed.returncode == 0, allowed.stderr
    assert json.loads(output.read_text())["Outputs"] == {
        **template["Outputs"],
        **inputs["outputs"],
    }


def test_predeploy_cli_has_distinct_nonready_exit_status(tmp_path):
    config, _, capture = evidence()
    capture["captured_at"] = datetime.now(UTC).isoformat()
    raw_config = json.loads(Path("infra/config.example.json").read_text())
    raw_config["alarm_topic_arns"] = list(config.alarm_topic_arns)
    raw_config["ecs_search"] = {
        "image_uri": config.ecs_search.image_uri,
        "hostname": config.ecs_search.hostname,
        "certificate_arn": config.ecs_search.certificate_arn,
        "availability_zones": list(config.ecs_search.availability_zones),
        "oauth_secret": {"name": "semsearch/ecs-oauth", "suffix": "AbCd04"},
        "oauth_subject": config.ecs_search.oauth_subject,
        "oauth_scope": config.ecs_search.oauth_scope,
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(raw_config))
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(json.dumps(capture))
    output = tmp_path / "plan.json"
    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            "scripts/preflight_ecs_deployment.py",
            "endpoint",
            "--config",
            str(config_path),
            "--region",
            config.region,
            "--evidence",
            str(capture_path),
            "--hosted-zone-id",
            "Z0123456789",
            "--phase",
            "predeploy",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3, (result.stdout, result.stderr)
    assert "PREDEPLOY_CHECKED" in result.stdout
    assert json.loads(output.read_text())["ready"] is False


@pytest.mark.parametrize("offset", [0.001, 1, 59, 60, 61])
def test_future_capture_is_rejected_even_within_old_skew_allowance(offset):
    with pytest.raises(ValueError, match="future"):
        fresh({"captured_at": (NOW + timedelta(seconds=offset)).isoformat()}, NOW)


@pytest.mark.parametrize("offset", [0, 899, 900])
def test_current_and_fifteen_minute_old_captures_are_fresh(offset):
    assert fresh({"captured_at": (NOW - timedelta(seconds=offset)).isoformat()}, NOW) == NOW


@pytest.mark.parametrize("region", ["cn-north-1", "cn-northwest-1"])
def test_ecr_layer_gateway_policy_has_exact_partition_region_bucket_and_read_action(
    tmp_path, region
):
    from test_ecs_production import configuration, props, synth

    def resolve(value):
        if isinstance(value, str):
            return value
        if "Ref" in value:
            return {"AWS::Partition": "aws-cn", "AWS::Region": region}[value["Ref"]]
        separator, parts = value["Fn::Join"]
        return separator.join(resolve(part) for part in parts)

    document = synth(configuration(region), tmp_path)
    endpoints = props(document, "AWS::EC2::VPCEndpoint")
    s3 = next(row for row in endpoints if resolve(row["ServiceName"]).endswith(".s3"))
    layers = [row for row in s3["PolicyDocument"]["Statement"] if row["Action"] == "s3:GetObject"]
    assert len(layers) == 1
    layer = layers[0]
    assert resolve(layer["Resource"]) == (f"arn:aws-cn:s3:::prod-{region}-starport-layer-bucket/*")
    assert layer == {
        "Action": "s3:GetObject",
        "Effect": "Allow",
        "Principal": "*",
        "Resource": layer["Resource"],
    }


@pytest.mark.parametrize("change", ["filter-name", "source-group", "remove-source"])
def test_automatic_metric_filter_names_do_not_allow_physical_identity_changes(change):
    template = live_template()
    template["Resources"]["AutomaticFilter"] = {
        "Type": "AWS::Logs::MetricFilter",
        "Properties": {
            "LogGroupName": "existing-ecs-logs",
            "FilterPattern": '{ $.event = "worker_failed" }',
            "MetricTransformations": [
                {
                    "MetricName": "ecs-worker-failed",
                    "MetricNamespace": "fixture",
                    "MetricValue": "1",
                }
            ],
        },
    }
    changed = deepcopy(template["Resources"]["AutomaticFilter"])
    if change == "filter-name":
        changed["Properties"]["FilterName"] = "replacement-name"
    elif change == "source-group":
        changed["Properties"]["LogGroupName"] = "different-logs"
    else:
        changed["Properties"].pop("LogGroupName")
    with pytest.raises(ValueError, match="physical identity"):
        prepare_update_overlay(
            template,
            {"Service": template["Resources"]["Service"], "AutomaticFilter": changed},
            allowed_ids={"Service", "AutomaticFilter"},
            live_evidence=bound(template),
            expected_stack_id=STACK_ID,
            now=NOW,
        )


def test_unchanged_automatic_filter_keeps_logical_identity_and_omitted_physical_name():
    template = live_template()
    automatic = {
        "Type": "AWS::Logs::MetricFilter",
        "Properties": {
            "LogGroupName": "existing-ecs-logs",
            "FilterPattern": '{ $.event = "worker_failed" }',
        },
    }
    template["Resources"]["AutomaticFilter"] = automatic
    result = prepare_update_overlay(
        template,
        {"Service": template["Resources"]["Service"], "AutomaticFilter": automatic},
        allowed_ids={"Service", "AutomaticFilter"},
        live_evidence=bound(template),
        expected_stack_id=STACK_ID,
        now=NOW,
    )
    assert result["Resources"]["AutomaticFilter"] == automatic

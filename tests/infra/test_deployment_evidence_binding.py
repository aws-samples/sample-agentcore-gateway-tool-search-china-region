"""A deployment guard requires bound, complete captures, not an arbitrary template."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from test_deployment_preflight import evidence as endpoint_evidence
from test_deployment_preflight import live_template, retained_table

NOW = datetime(2026, 10, 15, 8, tzinfo=UTC)
STACK_ID = (
    "arn:aws-cn:cloudformation:cn-north-1:111122223333:"
    "stack/sample-tool-search/00000000-1111-2222-3333-444444444444"
)


def digest(template):
    return hashlib.sha256(
        json.dumps(template, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def bound(template=None):
    template = deepcopy(template or live_template())
    rows = []
    for name, resource in template["Resources"].items():
        properties = resource.get("Properties", {})
        physical = properties.get("TableName", name + "-physical")
        if resource["Type"] == "AWS::Logs::LogGroup":
            physical = properties["LogGroupName"]
        rows.append(
            {
                "LogicalResourceId": name,
                "PhysicalResourceId": physical,
                "ResourceType": resource["Type"],
                "ResourceStatus": "CREATE_COMPLETE",
            }
        )
    return {
        "version": 1,
        "captured_at": NOW.isoformat(),
        "account": "111122223333",
        "region": "cn-north-1",
        "stack_id": STACK_ID,
        "template_sha256": digest(template),
        "get_template": {
            "request": {"StackName": STACK_ID, "TemplateStage": "Original"},
            "response": {"TemplateBody": template},
        },
        "describe_stacks": {
            "request": {"StackName": STACK_ID},
            "response": {
                "Stacks": [
                    {
                        "StackId": STACK_ID,
                        "StackName": "sample-tool-search",
                        "StackStatus": "UPDATE_COMPLETE",
                        "Parameters": [],
                        "Outputs": [],
                    }
                ]
            },
        },
        "list_stack_resources": {
            "pages": [
                {"request": {"StackName": STACK_ID}, "response": {"StackResourceSummaries": rows}}
            ]
        },
    }


def overlay(template=None, captures=None, replacements=None):
    from infra.deployment_preflight import prepare_update_overlay

    template = template or live_template()
    replacements = (
        {"Service": deepcopy(template["Resources"]["Service"])}
        if replacements is None
        else replacements
    )
    return prepare_update_overlay(
        template,
        replacements,
        allowed_ids=set(replacements),
        live_evidence=captures or bound(template),
        expected_stack_id=STACK_ID,
        now=NOW,
    )


def test_unbound_live_template_cannot_become_a_passed_update():
    from infra.deployment_preflight import prepare_update_overlay

    with pytest.raises(ValueError, match="evidence"):
        prepare_update_overlay(
            live_template(),
            {"Service": live_template()["Resources"]["Service"]},
            allowed_ids={"Service"},
        )


def test_trimmed_live_or_capture_identity_cannot_pass():
    template = live_template()
    captures = bound(template)
    template["Resources"].pop("OldFunction")
    with pytest.raises(ValueError, match="template|inventory"):
        overlay(template, captures)
    for field, value in [
        ("captured_at", "2026-10-15T07:00:00Z"),
        ("account", "444455556666"),
        ("stack_id", STACK_ID + "-wrong"),
        ("template_sha256", "0" * 64),
    ]:
        captures = bound()
        captures[field] = value
        with pytest.raises(ValueError):
            overlay(captures=captures)


@pytest.mark.parametrize("field", ["get_template", "describe_stacks"])
def test_capture_requests_are_exactly_bound(field):
    captures = bound()
    captures[field]["request"]["StackName"] = "another-stack"
    with pytest.raises(ValueError, match="request"):
        overlay(captures=captures)


def test_resource_inventory_requires_complete_nonduplicate_pagination():
    for kind in ("missing", "duplicate", "unclosed-page", "wrong-type"):
        captures = bound()
        response = captures["list_stack_resources"]["pages"][0]["response"]
        if kind == "missing":
            response["StackResourceSummaries"].pop()
        elif kind == "duplicate":
            response["StackResourceSummaries"].append(response["StackResourceSummaries"][0])
        elif kind == "unclosed-page":
            response["NextToken"] = "not-followed"
        else:
            response["StackResourceSummaries"][0]["ResourceType"] = "AWS::S3::Bucket"
        with pytest.raises(ValueError, match="inventory|pagination"):
            overlay(captures=captures)


def test_bound_update_preserves_retention_and_fixed_resource_identity():
    template = live_template()
    template["Resources"]["Table"] = retained_table()
    for field in ("DeletionPolicy", "UpdateReplacePolicy", "TableName"):
        changed = deepcopy(template["Resources"]["Table"])
        if field == "TableName":
            changed["Properties"][field] += "-replacement"
        else:
            changed.pop(field)
        with pytest.raises(ValueError, match="retention|physical"):
            overlay(
                template,
                replacements={"Service": template["Resources"]["Service"], "Table": changed},
            )
    result = overlay(template)
    assert result["Resources"]["Table"] == template["Resources"]["Table"]
    assert "DesiredCount" not in result["Resources"]["Service"]["Properties"]


def test_update_rejects_public_task_network_even_when_node_is_allowed():
    template = live_template()
    template["Resources"]["Service"]["Properties"]["NetworkConfiguration"] = {
        "AwsvpcConfiguration": {"AssignPublicIp": "DISABLED", "Subnets": ["subnet-private"]}
    }
    replacement = deepcopy(template["Resources"]["Service"])
    replacement["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]["AssignPublicIp"] = (
        "ENABLED"
    )
    with pytest.raises(ValueError, match="public"):
        overlay(template, replacements={"Service": replacement})


def test_certificate_preflight_can_run_before_nlb_exists_but_cannot_activate():
    from infra.deployment_preflight import check_endpoint

    config, arn, capture = endpoint_evidence()
    for key in ("load_balancer", "listeners", "record_sets"):
        capture.pop(key)
    result = check_endpoint(
        config, capture, hosted_zone_id="Z0123456789", phase="predeploy", now=NOW
    )
    assert result["status"] == "PREDEPLOY_CHECKED"
    assert result["ready"] is False and result["passed"] is True
    with pytest.raises(ValueError, match="stack|evidence"):
        check_endpoint(
            config, capture, hosted_zone_id="Z0123456789", load_balancer_arn=arn, now=NOW
        )


def test_endpoint_cannot_activate_another_stacks_nlb():
    from infra.deployment_preflight import check_endpoint

    config, arn, capture = endpoint_evidence()
    template = live_template()
    template["Resources"]["LoadBalancer"] = {
        "Type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "Properties": {"Scheme": "internal", "Type": "network"},
    }
    captures = bound(template)
    rows = captures["list_stack_resources"]["pages"][0]["response"]["StackResourceSummaries"]
    next(row for row in rows if row["LogicalResourceId"] == "LoadBalancer")[
        "PhysicalResourceId"
    ] = arn + "-other"
    with pytest.raises(ValueError, match="stack.*NLB|NLB.*stack"):
        check_endpoint(
            config,
            capture,
            hosted_zone_id="Z0123456789",
            load_balancer_arn=arn,
            live_evidence=captures,
            expected_stack_id=STACK_ID,
            now=NOW,
        )


def test_retained_bucket_with_pseudo_parameter_name_has_an_import_plan():
    from infra.deployment_preflight import retained_resource_recovery

    template = live_template()
    bucket = {
        "Type": "AWS::S3::Bucket",
        "DeletionPolicy": "Retain",
        "UpdateReplacePolicy": "Retain",
        "Properties": {
            "BucketName": {
                "Fn::Join": [
                    "",
                    ["fixture-", {"Ref": "AWS::AccountId"}, "-", {"Ref": "AWS::Region"}],
                ]
            },
            "VersioningConfiguration": {"Status": "Enabled"},
        },
    }
    candidate = deepcopy(template)
    candidate["Resources"]["RetainedBucket"] = bucket
    actual = {
        "BucketName": "fixture-111122223333-cn-north-1",
        "VersioningConfiguration": {"Status": "Enabled"},
    }
    result = retained_resource_recovery(
        template,
        candidate,
        [{"Type": "AWS::S3::Bucket", "PhysicalId": actual["BucketName"], "Properties": actual}],
        live_evidence=bound(template),
        expected_stack_id=STACK_ID,
        now=NOW,
    )
    assert result["resources_to_import"][0]["ResourceIdentifier"] == {
        "BucketName": actual["BucketName"]
    }
    assert result["import_template"]["Resources"]["RetainedBucket"] == bucket
    assert (
        result["import_template"]["Resources"]["OldFunction"]
        == template["Resources"]["OldFunction"]
    )


@pytest.mark.parametrize("inventory", [None, {}, [None], ["wrong"], [{"Properties": []}]])
def test_bad_inventory_is_a_validation_error(inventory):
    from infra.deployment_preflight import retained_resource_recovery

    with pytest.raises(ValueError):
        retained_resource_recovery(
            live_template(),
            live_template(),
            inventory,
            live_evidence=bound(),
            expected_stack_id=STACK_ID,
            now=NOW,
        )


def test_explicit_metric_filter_removal_does_not_authorize_other_deletions():
    from infra.deployment_preflight import prepare_update_overlay

    template = live_template()
    template["Resources"]["EcsLogs"] = {
        "Type": "AWS::Logs::LogGroup",
        "Properties": {"LogGroupName": "fixture-ecs-search-logs"},
    }
    template["Resources"]["Extra"] = {
        "Type": "AWS::Logs::MetricFilter",
        "Properties": {
            "FilterName": "fixture-embedding-error-connect-timeout-ecs",
            "LogGroupName": {"Ref": "EcsLogs"},
            "MetricTransformations": [
                {
                    "MetricName": "embedding-error-connect-timeout",
                    "MetricNamespace": "semsearch/fixture/ecs",
                }
            ],
        },
    }
    kwargs = {
        "allowed_ids": {"Service", "Extra"},
        "live_evidence": bound(template),
        "expected_stack_id": STACK_ID,
        "now": NOW,
        "remove_metric_filters": {"Extra"},
    }
    result = prepare_update_overlay(
        template, {"Service": template["Resources"]["Service"]}, **kwargs
    )
    assert set(result["Resources"]) == {"Service", "OldFunction", "EcsLogs"}
    for name in ("search-count", "unrelated-custom-metric"):
        template["Resources"]["Extra"]["Properties"]["MetricTransformations"][0]["MetricName"] = (
            name
        )
        kwargs["live_evidence"] = bound(template)
        with pytest.raises(ValueError, match="authorized"):
            prepare_update_overlay(
                template, {"Service": template["Resources"]["Service"]}, **kwargs
            )
    kwargs["remove_metric_filters"] = {"OldFunction"}
    kwargs["allowed_ids"].add("OldFunction")
    with pytest.raises(ValueError):
        prepare_update_overlay(template, {"Service": template["Resources"]["Service"]}, **kwargs)


def test_factory_entry_is_selected_without_a_redundant_facade_assignment():
    from pathlib import Path

    source = Path("infra/functions.py").read_text()
    assert 'if operation in ("fallback", "facade"):' not in source
    assert 'handler = "semantic_service.facade_lambda.handler"' in source


def test_complete_inventory_pagination_accepts_each_request_bound_page():
    capture = bound()
    pages = capture["list_stack_resources"]["pages"]
    extra = pages[0]["response"]["StackResourceSummaries"].pop()
    pages[0]["response"]["NextToken"] = "continuation"
    pages.append(
        {
            "request": {"StackName": STACK_ID, "NextToken": "continuation"},
            "response": {"StackResourceSummaries": [extra]},
        }
    )
    assert "DesiredCount" not in overlay(captures=capture)["Resources"]["Service"]["Properties"]
    pages[1]["request"]["NextToken"] = "another-token"
    with pytest.raises(ValueError, match="pagination"):
        overlay(captures=capture)


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("AWS::Lambda::Function", "FunctionName"),
        ("AWS::Logs::LogGroup", "LogGroupName"),
        ("AWS::S3::Bucket", "BucketName"),
        ("AWS::IAM::Role", "RoleName"),
        ("AWS::ECS::Service", "ServiceName"),
    ],
)
def test_allowed_nodes_still_cannot_change_an_existing_physical_name(kind, name):
    template = live_template()
    template["Resources"]["Fixed"] = {
        "Type": kind,
        "Properties": {name: "fixed-name"},
    }
    changed = deepcopy(template["Resources"]["Fixed"])
    changed["Properties"][name] += "-new"
    with pytest.raises(ValueError, match="physical"):
        overlay(
            template,
            replacements={"Fixed": changed, "Service": template["Resources"]["Service"]},
        )


def test_retained_lambda_versions_are_not_fixed_name_import_collisions():
    from infra.deployment_preflight import retained_resource_recovery

    template = live_template()
    candidate = deepcopy(template)
    candidate["Resources"]["Version"] = {
        "Type": "AWS::Lambda::Version",
        "DeletionPolicy": "Retain",
        "UpdateReplacePolicy": "Retain",
        "Properties": {"FunctionName": {"Ref": "OldFunction"}},
    }
    result = retained_resource_recovery(
        template,
        candidate,
        [],
        live_evidence=bound(template),
        expected_stack_id=STACK_ID,
        now=NOW,
    )
    assert result["resources_to_import"] == [] and result["ready"] is False


@pytest.mark.parametrize("bad", [None, [], "wrong", {"Type": []}])
def test_recovery_validates_even_existing_candidate_nodes(bad):
    from infra.deployment_preflight import retained_resource_recovery

    candidate = live_template()
    candidate["Resources"]["OldFunction"] = bad
    with pytest.raises(ValueError):
        retained_resource_recovery(
            live_template(),
            candidate,
            [],
            live_evidence=bound(),
            expected_stack_id=STACK_ID,
            now=NOW,
        )


def test_endpoint_requires_all_stack_outputs_to_match_actual_metadata():
    from infra.deployment_preflight import check_endpoint
    from test_deployment_preflight import endpoint_stack_binding

    config, arn, capture = endpoint_evidence()
    for output in ("SearchEcsEndpoint", "SearchNlbDnsName", "SearchNlbCanonicalHostedZoneId"):
        kwargs = endpoint_stack_binding(config, arn)
        row = next(
            row
            for row in kwargs["live_evidence"]["describe_stacks"]["response"]["Stacks"][0][
                "Outputs"
            ]
            if row["OutputKey"] == output
        )
        row["OutputValue"] += "-other"
        with pytest.raises(ValueError):
            check_endpoint(
                config,
                capture,
                hosted_zone_id="Z0123456789",
                load_balancer_arn=arn,
                **kwargs,
            )


def test_synthetic_mixed_namespace_filters_can_be_removed_without_deleting_legacy_signal():
    from pathlib import Path

    from infra.deployment_preflight import prepare_update_overlay

    fixture = json.loads(Path("tests/infra/fixtures/metric_filter_migration.json").read_text())
    template = live_template()
    template["Resources"].update(fixture["resources"])
    ecs_id = "SyntheticEcsBm25Filter"
    fallback_id = "SyntheticFallbackBm25Filter"
    legacy_id = "SyntheticSearchBm25Filter"
    kwargs = {
        "allowed_ids": {"Service", ecs_id, fallback_id, legacy_id},
        "live_evidence": bound(template),
        "expected_stack_id": STACK_ID,
        "now": NOW,
        "remove_metric_filters": {ecs_id, fallback_id},
    }
    result = prepare_update_overlay(
        template, {"Service": template["Resources"]["Service"]}, **kwargs
    )
    assert ecs_id not in result["Resources"] and fallback_id not in result["Resources"]
    assert result["Resources"][legacy_id] == template["Resources"][legacy_id]
    for case in ("legacy", "wrong-source", "forged-log-name"):
        changed = deepcopy(template)
        kwargs["remove_metric_filters"] = {legacy_id} if case == "legacy" else {ecs_id}
        if case == "wrong-source":
            changed["Resources"][ecs_id]["Properties"]["LogGroupName"] = {
                "Ref": "SyntheticSearchLogs"
            }
        if case == "forged-log-name":
            changed["Resources"]["SyntheticEcsLogs"]["Properties"]["LogGroupName"] = (
                "sample-tool-search-search-logs"
            )
        kwargs["live_evidence"] = bound(changed)
        with pytest.raises(ValueError, match="authorized"):
            prepare_update_overlay(changed, {"Service": changed["Resources"]["Service"]}, **kwargs)

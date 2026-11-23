"""Gateway endpoint string conditions must remain literal in both China Regions."""

from copy import deepcopy

import pytest
from test_ecs_production import configuration, resources, synth


def _string_leaves(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _string_leaves(child)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _string_leaves(child)


def _assert_literal_gateway_string_conditions(document):
    endpoints = {
        name: row["Properties"]
        for name, row in resources(document, "AWS::EC2::VPCEndpoint").items()
        if row["Properties"]["VpcEndpointType"] == "Gateway"
    }
    assert endpoints, "the fixture must include gateway endpoints"
    violations = []
    for name, endpoint in endpoints.items():
        statements = endpoint["PolicyDocument"]["Statement"]
        statements = [statements] if isinstance(statements, dict) else statements
        for index, statement in enumerate(statements):
            for operator, values in statement.get("Condition", {}).items():
                # Set prefixes and IfExists suffixes retain the String* family.
                if operator.rsplit(":", 1)[-1].startswith("String"):
                    for value in _string_leaves(values):
                        if "*" in value or "?" in value:
                            violations.append((name, index, operator, value))
    assert not violations, f"gateway string condition contains a wildcard: {violations}"


@pytest.mark.parametrize("region", ["cn-north-1", "cn-northwest-1"])
def test_all_gateway_endpoint_string_condition_values_are_literal(tmp_path, region):
    _assert_literal_gateway_string_conditions(synth(configuration(region), tmp_path))


def _document(service, conditions):
    return {
        "Resources": {
            "Endpoint": {
                "Type": "AWS::EC2::VPCEndpoint",
                "Properties": {
                    "VpcEndpointType": "Gateway",
                    "ServiceName": f"com.amazonaws.cn-north-1.{service}",
                    "PolicyDocument": {
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": f"{service}:Get*",
                                "Resource": "arn:aws-cn:s3:::bucket/objects/*?",
                                "Principal": "*",
                                "Condition": conditions,
                            }
                        ]
                    },
                },
            }
        }
    }


@pytest.mark.parametrize("service", ["s3", "dynamodb"])
@pytest.mark.parametrize("wildcard", ["ab*", "ab?"])
@pytest.mark.parametrize(
    "operator",
    [
        "StringEquals",
        "StringNotEquals",
        "StringEqualsIgnoreCase",
        "StringNotEqualsIgnoreCase",
        "StringLike",
        "StringNotLike",
        "StringEqualsIfExists",
        "StringNotLikeIfExists",
        "ForAllValues:StringLike",
        "ForAnyValue:StringEquals",
        "ForAllValues:StringNotEqualsIfExists",
        "ForAnyValue:StringLikeIfExists",
    ],
)
def test_string_condition_guard_finds_nested_wildcards(service, wildcard, operator):
    # The key is deliberately not LeadingKeys; Fn::If/Sub/Join hold nested leaves.
    value = {
        "Fn::If": [
            "UsePrefix",
            "literal",
            {"Fn::Sub": ["${Prefix}/object", {"Prefix": {"Fn::Join": ["", ["safe/", wildcard]]}}]},
        ]
    }
    key = "s3:prefix" if service == "s3" else "dynamodb:Attributes"
    document = _document(service, {operator: {key: value}})
    with pytest.raises(AssertionError, match="string condition contains a wildcard"):
        _assert_literal_gateway_string_conditions(document)


def test_string_condition_guard_ignores_resource_principal_and_nonstring_operators():
    document = _document(
        "s3",
        {
            "StringEquals": {
                "aws:PrincipalArn": {"Fn::GetAtt": ["TaskRole", "Arn"]},
                "s3:VersionId": {"Ref": "SearchConfigVersionId"},
            },
            "ArnLike": {"aws:SourceArn": "arn:aws-cn:iam::*:role/*"},
        },
    )
    _assert_literal_gateway_string_conditions(document)


@pytest.mark.parametrize("location", ["later_statement", "later_endpoint"])
def test_string_condition_guard_checks_every_statement_and_gateway_endpoint(location):
    document = _document("s3", {"StringEquals": {"s3:VersionId": "literal"}})
    bad = _document("dynamodb", {"StringLike": {"dynamodb:Attributes": ["literal", "ab*"]}})
    if location == "later_statement":
        statement = bad["Resources"]["Endpoint"]["Properties"]["PolicyDocument"]["Statement"][0]
        document["Resources"]["Endpoint"]["Properties"]["PolicyDocument"]["Statement"].append(
            statement
        )
    else:
        document["Resources"]["AdditionalEndpoint"] = deepcopy(bad["Resources"]["Endpoint"])
    with pytest.raises(AssertionError, match="string condition contains a wildcard"):
        _assert_literal_gateway_string_conditions(document)

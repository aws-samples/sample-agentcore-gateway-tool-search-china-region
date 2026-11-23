"""Validate explicitly captured, non-secret CloudFormation evidence offline.

This validates consistency, freshness and pagination. It cannot authenticate a
forged capture or prove it is still the current AWS state. A separately reviewed
AWS change set remains necessary before deployment.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

JsonObject = dict[str, Any]


def object_value(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return value


def rows(value: object, label: str) -> list[JsonObject]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return [object_value(row, label) for row in value]


def text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def timestamp(value: object) -> datetime:
    try:
        result = datetime.fromisoformat(text(value, "timestamp").replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("timestamp must be an ISO 8601 time with timezone") from error
    if result.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return result.astimezone(UTC)


def fresh(evidence: JsonObject, now: datetime | None) -> datetime:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("current time must include a timezone")
    if not 0 <= (current - timestamp(evidence.get("captured_at"))).total_seconds() <= 900:
        raise ValueError("metadata must be fresh (at most 15 minutes old, never in the future)")
    return current


def decode_json(value: str) -> JsonObject:
    def pairs(entries: list[tuple[str, Any]]) -> JsonObject:
        result: JsonObject = {}
        for key, item in entries:
            if key in result:
                raise ValueError("duplicate key in deployment metadata")
            result[key] = item
        return result

    def number(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("non-finite number in deployment metadata")
        return result

    def invalid(value: str) -> None:
        raise ValueError("non-finite constant in deployment metadata")

    return object_value(
        json.loads(value, object_pairs_hook=pairs, parse_float=number, parse_constant=invalid),
        "JSON document",
    )


def template_digest(template: JsonObject) -> str:
    return hashlib.sha256(
        json.dumps(template, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def bound_response(evidence: JsonObject, name: str, expected_request: JsonObject) -> JsonObject:
    capture = object_value(evidence.get(name), name)
    if capture.get("request") != expected_request:
        raise ValueError(f"{name} request does not match the expected resource")
    return object_value(capture.get("response"), name + " response")


def _unique_fields(value: object, key: str, item: str, label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in rows(value, label):
        name = text(row.get(key), label + " key")
        if name in result:
            raise ValueError(f"duplicate {label} key")
        result[name] = text(row.get(item), label + " value")
    return result


@dataclass(frozen=True)
class LiveEvidence:
    stack_id: str
    account: str
    region: str
    partition: str
    stack_name: str
    template: JsonObject
    inventory: JsonObject
    parameters: dict[str, str]
    outputs: dict[str, str]

    def resolve(self, value: Any) -> Any:
        """Resolve supported capture-bound values; never guess future resources."""
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "Ref" in value:
            if set(value) != {"Ref"}:
                raise ValueError("invalid Ref in retained resource properties")
            key = text(value["Ref"], "Ref")
            known: dict[str, Any] = {
                "AWS::AccountId": self.account,
                "AWS::Region": self.region,
                "AWS::Partition": self.partition,
                "AWS::StackName": self.stack_name,
                "AWS::StackId": self.stack_id,
                "AWS::URLSuffix": (
                    "amazonaws.com.cn" if self.partition == "aws-cn" else "amazonaws.com"
                ),
                **self.parameters,
            }
            if key in known:
                return known[key]
            if key in self.inventory:
                return self.inventory[key]["PhysicalResourceId"]
            raise ValueError("retained resource depends on an unresolved future resource")
        if "Fn::Join" in value:
            parts = value["Fn::Join"]
            if (
                set(value) != {"Fn::Join"}
                or not isinstance(parts, list)
                or len(parts) != 2
                or not isinstance(parts[0], str)
                or not isinstance(parts[1], list)
            ):
                raise ValueError("invalid Fn::Join in retained resource properties")
            resolved = self.resolve(parts[1])
            if any(not isinstance(item, str) for item in resolved):
                raise ValueError("Fn::Join requires resolved string values")
            return parts[0].join(resolved)
        if "Fn::Sub" in value:
            raw = value["Fn::Sub"]
            variables: JsonObject = {}
            if set(value) != {"Fn::Sub"}:
                raise ValueError("invalid Fn::Sub")
            if isinstance(raw, list) and len(raw) == 2:
                expression = text(raw[0], "Fn::Sub expression")
                variables = object_value(self.resolve(raw[1]), "Fn::Sub variables")
            else:
                expression = text(raw, "Fn::Sub expression")

            def substitute(match: re.Match[str]) -> str:
                key = match.group(1)
                if key.startswith("!"):
                    return "${" + key[1:] + "}"
                result = variables[key] if key in variables else self.resolve({"Ref": key})
                if not isinstance(result, str):
                    raise ValueError("Fn::Sub requires resolved string values")
                return result

            return re.sub(r"\$\{([^}]+)\}", substitute, expression)
        if any(key.startswith("Fn::") for key in value):
            raise ValueError("unsupported intrinsic in retained resource properties")
        return {key: self.resolve(item) for key, item in value.items()}


def validate_live_evidence(
    template: object,
    evidence: object,
    expected_stack_id: object,
    *,
    now: datetime | None = None,
) -> LiveEvidence:
    live = object_value(template, "live template")
    capture = object_value(evidence, "live evidence")
    stack_id = text(expected_stack_id, "expected stack ID")
    match = re.fullmatch(
        r"arn:(aws(?:-cn|-us-gov)?):cloudformation:([a-z0-9-]+):([0-9]{12}):"
        r"stack/([A-Za-z][A-Za-z0-9-]*)/([A-Za-z0-9-]+)",
        stack_id,
    )
    if match is None:
        raise ValueError("expected stack ID must be a full CloudFormation stack ARN")
    partition, region, account, stack_name, _ = match.groups()
    fresh(capture, now)
    if (
        capture.get("version") != 1
        or capture.get("stack_id") != stack_id
        or capture.get("account") != account
        or capture.get("region") != region
    ):
        raise ValueError("live evidence identity does not match the expected stack")
    captured_template = bound_response(
        capture, "get_template", {"StackName": stack_id, "TemplateStage": "Original"}
    ).get("TemplateBody")
    if isinstance(captured_template, str):
        captured_template = decode_json(captured_template)
    captured_template = object_value(captured_template, "captured template")
    digest = template_digest(live)
    if capture.get("template_sha256") != digest or template_digest(captured_template) != digest:
        raise ValueError("live template does not match the captured template hash")
    response = bound_response(capture, "describe_stacks", {"StackName": stack_id})
    stacks = rows(response.get("Stacks"), "stacks")
    if len(stacks) != 1 or response.get("NextToken"):
        raise ValueError("stack description must contain exactly the requested stack")
    stack = stacks[0]
    if (
        stack.get("StackId") != stack_id
        or stack.get("StackName") != stack_name
        or stack.get("StackStatus")
        not in {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "IMPORT_COMPLETE"}
    ):
        raise ValueError("stack identity or stable deployment status is invalid")
    resources = object_value(live.get("Resources"), "live resources")
    for name, resource in resources.items():
        text(object_value(resource, name).get("Type"), "resource type")
    pages = rows(
        object_value(capture.get("list_stack_resources"), "resource inventory").get("pages"),
        "resource inventory pages",
    )
    if not pages:
        raise ValueError("resource inventory pagination is missing")
    inventory: JsonObject = {}
    token: str | None = None
    seen_tokens: set[str] = set()
    for index, page in enumerate(pages):
        request = {"StackName": stack_id}
        if token is not None:
            request["NextToken"] = token
        if page.get("request") != request:
            raise ValueError("resource inventory pagination request does not match")
        response = object_value(page.get("response"), "resource inventory response")
        for row in rows(response.get("StackResourceSummaries"), "resource inventory"):
            name = text(row.get("LogicalResourceId"), "inventory logical ID")
            if name in inventory or name not in resources:
                raise ValueError("resource inventory contains duplicate or unexpected resources")
            if row.get("ResourceType") != resources[name]["Type"]:
                raise ValueError("resource inventory type does not match the live template")
            text(row.get("PhysicalResourceId"), "inventory physical ID")
            if row.get("ResourceStatus") not in {
                "CREATE_COMPLETE",
                "UPDATE_COMPLETE",
                "IMPORT_COMPLETE",
                "UPDATE_ROLLBACK_COMPLETE",
            }:
                raise ValueError("resource inventory contains an unstable resource")
            inventory[name] = row
        raw_token = response.get("NextToken")
        token = text(raw_token, "inventory next token") if raw_token is not None else None
        if token is not None:
            if token in seen_tokens:
                raise ValueError("resource inventory pagination repeats a token")
            seen_tokens.add(token)
        if (token is None) != (index == len(pages) - 1):
            raise ValueError("resource inventory pagination is incomplete or has extra pages")
    if set(inventory) != set(resources):
        raise ValueError("resource inventory does not cover the complete live template")
    return LiveEvidence(
        stack_id,
        account,
        region,
        partition,
        stack_name,
        live,
        inventory,
        _unique_fields(stack.get("Parameters", []), "ParameterKey", "ParameterValue", "parameters"),
        _unique_fields(stack.get("Outputs", []), "OutputKey", "OutputValue", "outputs"),
    )

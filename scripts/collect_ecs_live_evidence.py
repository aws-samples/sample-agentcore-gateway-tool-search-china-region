"""Collect read-only CloudFormation evidence only with an explicit --collect.

The collection code calls only describe_stacks, get_template and
list_stack_resources; it never deploys or requests Secrets Manager values.
--help is offline. With --collect, the existing SDK credential provider may
refresh credentials (including via STS); its use requires existing authorization.
This script does not grant permission for any later operation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infra.live_evidence import (  # noqa: E402
    JsonObject,
    decode_json,
    object_value,
    rows,
    template_digest,
    text,
    validate_live_evidence,
)

MAX_CAPTURE_SECONDS = 900
STABLE_STATUSES = {
    "CREATE_COMPLETE",
    "UPDATE_COMPLETE",
    "UPDATE_ROLLBACK_COMPLETE",
    "IMPORT_COMPLETE",
}


class CloudFormation(Protocol):
    def describe_stacks(self, **kwargs: str) -> JsonObject: ...

    def get_template(self, **kwargs: str) -> JsonObject: ...

    def list_stack_resources(self, **kwargs: str) -> JsonObject: ...


def stack_identity(stack_id: str) -> tuple[str, str, str, str]:
    match = re.fullmatch(
        r"arn:(aws(?:-cn|-us-gov)?):cloudformation:([a-z0-9-]+):([0-9]{12}):"
        r"stack/([A-Za-z][A-Za-z0-9-]*)/([A-Za-z0-9-]+)",
        stack_id,
    )
    if match is None:
        raise ValueError("expected stack ID must be a full CloudFormation stack ARN")
    partition, region, account, stack_name, _ = match.groups()
    return partition, region, account, stack_name


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _json_default(value: object) -> str:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).isoformat()
    raise TypeError("CloudFormation response contains an unsupported JSON value")


def _json_response(value: object) -> JsonObject:
    return decode_json(json.dumps(value, default=_json_default, allow_nan=False))


def _stack(response: JsonObject, stack_id: str, stack_name: str) -> JsonObject:
    stacks = rows(response.get("Stacks"), "stacks")
    if len(stacks) != 1 or response.get("NextToken"):
        raise ValueError("stack description must contain exactly the requested stack")
    stack = stacks[0]
    if stack.get("StackId") != stack_id or stack.get("StackName") != stack_name:
        raise ValueError("stack identity does not match the expected stack")
    if stack.get("StackStatus") not in STABLE_STATUSES:
        raise ValueError("stack must have a stable deployment status before collection")
    return stack


def collect(
    client: CloudFormation,
    expected_stack_id: str,
    *,
    wall_clock: Callable[[], datetime] = _utc_now,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[JsonObject, JsonObject]:
    """Return complete, validated data without refreshing its initial timestamp.

    A final DescribeStacks catches observable changes during the read sequence.
    CloudFormation has no snapshot transaction here: this is not an authenticated
    state proof and cannot detect changes after the final read. An independently
    reviewed change set and a fresh capture remain required before execution.
    """
    partition, region, account, stack_name = stack_identity(expected_stack_id)
    started = wall_clock()
    if started.tzinfo is None:
        raise ValueError("capture clock must include a timezone")
    started = started.astimezone(UTC)
    monotonic_start = monotonic()

    def check_budget() -> datetime:
        current = wall_clock()
        if current.tzinfo is None:
            raise ValueError("capture clock must include a timezone")
        current = current.astimezone(UTC)
        elapsed = monotonic() - monotonic_start
        wall_elapsed = (current - started).total_seconds()
        if not 0 <= elapsed <= MAX_CAPTURE_SECONDS:
            raise ValueError("collection exceeded its original 900-second budget")
        if not 0 <= wall_elapsed <= MAX_CAPTURE_SECONDS:
            raise ValueError("capture clock is outside the original freshness window")
        return current

    def read(method: Callable[..., Any], request: dict[str, str]) -> JsonObject:
        check_budget()
        response = _json_response(method(**request))
        check_budget()
        return {"request": request, "response": response}

    initial = read(client.describe_stacks, {"StackName": expected_stack_id})
    initial_stack = _stack(initial["response"], expected_stack_id, stack_name)
    template_capture = read(
        client.get_template, {"StackName": expected_stack_id, "TemplateStage": "Original"}
    )
    template_value = template_capture["response"].get("TemplateBody")
    if isinstance(template_value, str):
        template_value = decode_json(template_value)
    template = object_value(template_value, "Original template body")
    pages: list[JsonObject] = []
    token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        request = {"StackName": expected_stack_id}
        if token is not None:
            request["NextToken"] = token
        page = read(client.list_stack_resources, request)
        pages.append(page)
        raw_token = page["response"].get("NextToken")
        if raw_token is None:
            break
        token = text(raw_token, "inventory next token")
        if token in seen_tokens:
            raise ValueError("resource inventory pagination repeats a token")
        seen_tokens.add(token)
    final = read(client.describe_stacks, {"StackName": expected_stack_id})
    final_stack = _stack(final["response"], expected_stack_id, stack_name)
    if final_stack != initial_stack:
        raise ValueError("stack changed during collection; obtain an entirely new capture")
    completed = check_budget()
    capture: JsonObject = {
        "version": 1,
        "captured_at": started.isoformat(),
        "collection_completed_at": completed.isoformat(),
        "expires_at": (started + timedelta(seconds=MAX_CAPTURE_SECONDS)).isoformat(),
        "expected_stack_id": expected_stack_id,
        "stack_id": expected_stack_id,
        "partition": partition,
        "account": account,
        "region": region,
        "template_sha256": template_digest(template),
        "initial_describe_stacks": initial,
        "describe_stacks": final,
        "get_template": template_capture,
        "list_stack_resources": {"pages": pages},
    }
    validate_live_evidence(template, capture, expected_stack_id, now=completed)
    check_budget()
    return template, capture


def _client(region: str, profile: str | None) -> CloudFormation:
    # No SDK session, configuration file or credential chain is touched by --help
    # or by a missing --collect switch. The runtime's ordinary credential provider
    # chain is used only after explicit collection. Providers may refresh via STS
    # or a configured credential_process; this tool does not broaden authorization.
    import boto3
    from botocore.config import Config

    session = boto3.Session(profile_name=profile, region_name=region)
    return cast(
        CloudFormation,
        session.client(
            "cloudformation",
            region_name=region,
            config=Config(
                connect_timeout=5,
                read_timeout=20,
                retries={"mode": "standard", "total_max_attempts": 3},
            ),
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--collect", action="store_true", help="Explicitly allow the three read-only API calls."
    )
    parser.add_argument("--expected-stack-id", required=True, help="Full CloudFormation stack ARN.")
    parser.add_argument(
        "--profile", help="Explicit local SDK profile, if required by the operator."
    )
    parser.add_argument("--output-dir", required=True, type=Path, help="A new capture directory.")
    args = parser.parse_args(argv)
    if not args.collect:
        parser.error("--collect is required before any AWS client is constructed")
    try:
        _, region, _, _ = stack_identity(args.expected_stack_id)
        if args.output_dir.exists():
            raise ValueError("output directory already exists; captures are never overwritten")
        template, capture = collect(_client(region, args.profile), args.expected_stack_id)
        # Publish only a validated bundle. A fresh directory also prevents a second
        # collection from silently relabelling an old template with a new timestamp.
        args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name, value in (
            ("live-template.json", template),
            ("live-evidence.json", capture),
        ):
            with (args.output_dir / name).open("x", encoding="utf-8") as output:
                output.write(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    except (OSError, ValueError, TypeError) as error:
        parser.error(str(error))
    except Exception as error:
        # Do not echo SDK response bodies, config or credential-provider details.
        parser.error(f"CloudFormation capture failed ({type(error).__name__})")
    print("CAPTURED: read-only evidence does not authorize a deployment or activation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

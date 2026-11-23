"""Prepare and check ECS deployment evidence locally; no AWS operations are performed."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from infra.config import DeploymentConfig  # noqa: E402
from infra.deployment_preflight import (  # noqa: E402
    check_endpoint,
    load_json,
    prepare_update_overlay,
    retained_resource_recovery,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    endpoint = commands.add_parser(
        "endpoint", help="Verify fresh, request-bound ACM/NLB/Route53 metadata."
    )
    endpoint.add_argument("--config", required=True, type=Path)
    endpoint.add_argument("--region", required=True, choices=("cn-north-1", "cn-northwest-1"))
    endpoint.add_argument("--evidence", required=True, type=Path)
    endpoint.add_argument("--hosted-zone-id", required=True)
    endpoint.add_argument("--load-balancer-arn")
    endpoint.add_argument("--phase", choices=("predeploy", "activate"), default="activate")
    endpoint.add_argument("--live-evidence", type=Path)
    endpoint.add_argument("--expected-stack-id")
    overlay = commands.add_parser(
        "overlay", help="Overlay selected resource nodes onto the entire current live template."
    )
    overlay.add_argument("--live-template", required=True, type=Path)
    overlay.add_argument("--replacements", required=True, type=Path)
    overlay.add_argument("--allow-resource", required=True, action="append")
    overlay.add_argument("--remove-metric-filter", action="append", default=[])
    overlay.add_argument("--output-additions", type=Path)
    overlay.add_argument("--allow-output", action="append", default=[])
    recovery = commands.add_parser(
        "recovery", help="Plan retained-resource IMPORT or a separately approved deletion."
    )
    recovery.add_argument("--live-template", required=True, type=Path)
    recovery.add_argument("--candidate-template", required=True, type=Path)
    recovery.add_argument("--inventory", required=True, type=Path)
    for command in (overlay, recovery):
        command.add_argument("--live-evidence", required=True, type=Path)
        command.add_argument("--expected-stack-id", required=True)
    for command in (endpoint, overlay, recovery):
        command.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "endpoint":
            # Strict JSON is checked before the existing typed config parser runs.
            load_json(args.config)
            result = check_endpoint(
                DeploymentConfig.from_file(args.config, region=args.region),
                load_json(args.evidence),
                hosted_zone_id=args.hosted_zone_id,
                load_balancer_arn=args.load_balancer_arn,
                phase=args.phase,
                live_evidence=load_json(args.live_evidence) if args.live_evidence else None,
                expected_stack_id=args.expected_stack_id,
            )
            # Only a fully checked endpoint is exit 0. Certificate-only predeploy
            # is a distinct plan state, so exit-code-only wrappers cannot activate.
            code = 0 if result["ready"] else 3 if result["passed"] else 2
        elif args.command == "overlay":
            result = prepare_update_overlay(
                load_json(args.live_template),
                load_json(args.replacements),
                allowed_ids=set(args.allow_resource),
                live_evidence=load_json(args.live_evidence),
                expected_stack_id=args.expected_stack_id,
                remove_metric_filters=set(args.remove_metric_filter),
                output_additions=(
                    load_json(args.output_additions) if args.output_additions else None
                ),
                allowed_output_ids=set(args.allow_output),
            )
            code = 0
        else:
            inventory = load_json(args.inventory).get("resources")
            if not isinstance(inventory, list) or not all(
                isinstance(row, dict) for row in inventory
            ):
                raise ValueError("inventory must contain a resources array")
            result = retained_resource_recovery(
                load_json(args.live_template),
                load_json(args.candidate_template),
                inventory,
                live_evidence=load_json(args.live_evidence),
                expected_stack_id=args.expected_stack_id,
            )
            code = 2  # A recovery plan cannot approve a deployment or a deletion.
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    if code == 3:
        print("PREDEPLOY_CHECKED: certificate and zone only; endpoint activation remains pending")
    elif code != 0:
        print("PLAN_ONLY: unresolved deployment prerequisites")
    elif args.command == "endpoint":
        print(result["status"] + ": this phase does not authorize deployment or activation")
    else:
        print("OVERLAY_PREPARED: independent change-set review still required")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the local CDK CLI with no AWS credentials, lookups, telemetry or user config."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUEST_HANDLER = "semantic_service.request_runtime.request_handler"
SHARED_HANDLER = "semantic_service.runtime.request_handler"


def _asset_argument(value: str) -> Path:
    if not value.strip() or value != value.strip():
        raise argparse.ArgumentTypeError("asset argument requires a nonempty ZIP path")
    return Path(value).resolve()


def _request_handler(output: Path) -> str | None:
    """Read the active assembly manifest, excluding stale templates left in its directory."""
    manifest_path = output / "manifest.json"
    if not manifest_path.exists():
        return None
    handlers = []
    try:
        manifest = json.loads(manifest_path.read_text())
        for artifact in manifest["artifacts"].values():
            if artifact.get("type") != "aws:cloudformation:stack":
                continue
            template_file = artifact["properties"]["templateFile"]
            template = json.loads((output / template_file).read_text())
            for resource in template["Resources"].values():
                if resource["Type"] != "AWS::Lambda::Function":
                    continue
                handler = resource.get("Properties", {}).get("Handler", "")
                if isinstance(handler, str) and handler.endswith(".request_handler"):
                    handlers.append(handler)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise SystemExit("Cannot verify the REQUEST handler in the cloud assembly.") from error
    if len(handlers) != 1:
        raise SystemExit("The cloud assembly must contain exactly one REQUEST handler.")
    return handlers[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("infra/config.example.json"))
    parser.add_argument("--region", choices=("cn-north-1", "cn-northwest-1"), required=True)
    parser.add_argument("--stage")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--asset",
        type=_asset_argument,
        help="Existing shared Lambda ZIP; keep this unchanged when adding ECS.",
    )
    parser.add_argument(
        "--ecs-asset",
        type=_asset_argument,
        help="New shared ZIP used only by the ECS facade and fallback functions.",
    )
    parser.add_argument(
        "--request-asset",
        type=_asset_argument,
        help="Override the ZIP path when config request_package is request.",
    )
    parser.add_argument(
        "--confirm-request-rollback",
        action="store_true",
        help="Allow this assembly to switch from the REQUEST package back to shared.",
    )
    args = parser.parse_args()
    if Path(sys.prefix).resolve() != ROOT / ".venv":
        raise SystemExit("Use this worktree's .venv/bin/python.")
    node = shutil.which("node")
    if node is None:
        raise SystemExit("An existing supported Node.js runtime is required.")
    node_path = Path(node).absolute()
    cdk = (ROOT / "node_modules/aws-cdk/bin/cdk").resolve()
    if not node_path.is_file() or not os.access(node_path, os.X_OK) or not cdk.is_file():
        parser.error("local Node.js and installed CDK executables are required")
    node = str(node_path)
    args.config = args.config.resolve()
    if not args.config.is_file():
        parser.error("--config must be an existing JSON file")
    if args.stage is not None and re.fullmatch(r"[a-z][a-z0-9-]{0,11}", args.stage) is None:
        parser.error("--stage must be 1-12 lowercase name characters")
    config = json.loads(args.config.read_text())
    package = config.get("request_package", "shared")
    if package not in ("shared", "request"):
        raise SystemExit("request_package must be shared or request.")
    if args.request_asset is not None and package != "request":
        raise SystemExit("--request-asset requires persistent config request_package=request.")
    ecs_enabled = config.get("ecs_search") is not None
    if ecs_enabled:
        if args.asset is None:
            raise SystemExit(
                "--asset is required when ecs_search is enabled; select the legacy ZIP."
            )
        if args.ecs_asset is None:
            raise SystemExit("--ecs-asset is required when ecs_search is enabled.")
        if args.asset.resolve() == args.ecs_asset.resolve():
            raise SystemExit("--asset and --ecs-asset must use different ZIP paths.")
    elif args.ecs_asset is not None:
        raise SystemExit("--ecs-asset requires ecs_search configuration.")
    for name in ("asset", "ecs_asset", "request_asset"):
        asset = getattr(args, name)
        if asset is not None and (asset.suffix.lower() != ".zip" or not asset.is_file()):
            raise SystemExit(f"--{name.replace('_', '-')} requires an existing ZIP file.")
    output = (args.output or ROOT / ".artifacts" / f"cdk-{args.region}").resolve()
    expected_handler = REQUEST_HANDLER if package == "request" else SHARED_HANDLER
    if (
        _request_handler(output) == REQUEST_HANDLER
        and package == "shared"
        and not args.confirm_request_rollback
    ):
        raise SystemExit(
            "This assembly used the REQUEST package; pass --confirm-request-rollback "
            "to intentionally return to shared."
        )
    arguments = [
        node,
        str(cdk),
        "synth",
        "--no-lookups",
        "--no-notices",
        "--no-version-reporting",
        "--no-path-metadata",
        "--output",
        str(output),
        "--context",
        f"config={args.config.resolve()}",
        "--context",
        f"region={args.region}",
    ]
    if args.stage:
        arguments.extend(["--context", f"stage={args.stage}"])
    if args.request_asset is not None:
        arguments.extend(["--context", f"request_asset={args.request_asset.resolve()}"])
    if args.asset is not None:
        arguments.extend(["--context", f"asset={args.asset.resolve()}"])
    if args.ecs_asset is not None:
        arguments.extend(["--context", f"ecs_asset={args.ecs_asset.resolve()}"])
    # The child receives no inherited environment, profile, AWS key or HOME. CDK
    # gets an explicit account/region from the JSON app and cannot perform lookups.
    with tempfile.TemporaryDirectory(prefix="synth-", dir=ROOT / ".artifacts") as temporary:
        environment = {
            "PATH": str(Path(node).parent) + os.pathsep + "/usr/bin:/bin",
            "HOME": temporary,
            "TMPDIR": temporary,
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_EC2_METADATA_DISABLED": "true",
            "CDK_DISABLE_VERSION_CHECK": "1",
            "PYTHONNOUSERSITE": "1",
        }
        # All paths/region/stage validated above; fixed local CDK, no shell or inherited env.
        # nosemgrep: dangerous-subprocess-use-audit, dangerous-subprocess-use-tainted-env-args
        subprocess.run(arguments, cwd=ROOT, env=environment, check=True, shell=False)  # nosec B603
    actual_handler = _request_handler(output)
    if actual_handler != expected_handler:
        raise SystemExit(
            f"REQUEST handler mismatch in synthesized assembly: expected {expected_handler}, "
            f"found {actual_handler!r}."
        )
    print(f"PASS: synthesized REQUEST handler is {actual_handler}.")


if __name__ == "__main__":
    main()

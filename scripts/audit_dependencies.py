"""Check the installed, approved dependency closure without accessing the network."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import sys
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Literal

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Write an inventory and reject unlocked or unnecessary distributions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("all", "request"), default="all")
    parser.add_argument(
        "--output",
        type=Path,
        help="Local inventory output; do not commit generated reports.",
    )
    args = parser.parse_args()
    if Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        raise SystemExit("Run this command using the worktree's .venv/bin/python.")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    roots = project["dependencies"] + project["optional-dependencies"]["dev"]
    environment = {str(key): str(value) for key, value in default_environment().items()}
    environment["extra"] = ""
    lock_text = (ROOT / "requirements.lock").read_text()
    locked: dict[str, str] = {}
    for line in lock_text.splitlines():
        if re.match(r"^[A-Za-z0-9]", line):
            requirement = Requirement(line.rstrip(" \\"))
            if requirement.marker is None or requirement.marker.evaluate(environment):
                pins = list(requirement.specifier)
                if len(pins) != 1 or pins[0].operator != "==":
                    raise SystemExit(f"Requirement is not an exact pin: {requirement}")
                locked[canonicalize_name(requirement.name)] = pins[0].version

    installed = {
        canonicalize_name(distribution.metadata["Name"]): distribution
        for distribution in importlib.metadata.distributions()
    }
    versions = {name: distribution.version for name, distribution in installed.items()}
    if versions != locked:
        raise SystemExit(
            "Installed distributions differ from the platform's lock: "
            + json.dumps({"installed": versions, "locked": locked}, sort_keys=True)
        )

    required = set()
    pending = [Requirement(requirement) for requirement in roots]
    while pending:
        requirement = pending.pop()
        if requirement.marker is not None and not requirement.marker.evaluate(environment):
            continue
        name = canonicalize_name(requirement.name)
        distribution = installed.get(name)
        if distribution is None or distribution.version not in requirement.specifier:
            raise SystemExit(f"Missing or incompatible dependency: {requirement}")
        if name in required:
            continue
        required.add(name)
        pending.extend(Requirement(value) for value in distribution.requires or [])
    if required != set(installed):
        raise SystemExit(f"Unnecessary installed distributions: {set(installed) - required}")

    records = []
    for name, distribution in sorted(installed.items()):
        metadata = distribution.read_text("METADATA") or ""
        records.append(
            {
                "name": name,
                "version": distribution.version,
                "license_expression": distribution.metadata.get("License-Expression"),
                "license_text": distribution.metadata.get("License"),
                "license_files": [
                    str(path)
                    for path in distribution.files or []
                    if "license" in str(path).lower() or "copying" in str(path).lower()
                ],
                "requires_dist": distribution.requires or [],
                "wheel": distribution.read_text("WHEEL"),
                "metadata_sha256": hashlib.sha256(metadata.encode()).hexdigest(),
                "pypi_metadata": f"https://pypi.org/pypi/{name}/{distribution.version}/json",
            }
        )
    result = {
        "scope": (
            "retrieval, Lambda runtime, evaluation and CDK/development "
            "closure, including target Lambda wheels and the local CDK CLI."
        ),
        "python": platform.python_version(),
        "system": platform.system(),
        "machine": platform.machine(),
        "approval": (
            "User authorized necessary common implementation packages; "
            "all direct pins and required transitive distributions audited."
        ),
        "direct_requirements": roots,
        "lock_sha256": hashlib.sha256(lock_text.encode()).hexdigest(),
        "installed_count": len(records),
        "closure_matches_lock": True,
        "all_installed_packages_reachable_from_approved_roots": True,
        "records": records,
        "lambda_target": audit_lambda_wheels(
            roots=project["dependencies"],
            locked=locked,
            lock_text=lock_text,
            operation=args.operation,
        ),
        "node_tooling": audit_node_tooling(),
    }
    default_name = (
        "request-dependency-closure.json"
        if args.operation == "request"
        else "dependency-closure.json"
    )
    destination = (args.output or ROOT / ".artifacts" / default_name).resolve()
    if destination.is_relative_to(ROOT / "evidence"):
        raise SystemExit("Do not modify the historical tracked evidence directory.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n")
    print(f"PASS: {len(records)} installed distributions match the approved locked closure.")
    print(f"Inventory: {destination}")


def audit_lambda_wheels(
    *,
    roots: list[str],
    locked: dict[str, str],
    lock_text: str,
    operation: Literal["all", "request"] = "all",
) -> dict[str, object]:
    """Check hashes and reachability using target wheel metadata, not host imports."""
    # Keep build helpers importable both from `scripts/` and through pytest.
    from build_lambda import (
        load_lock,
        record_digest,
        select_wheels,
        sha256,
        validate_target_wheel,
        validate_wheel_closure,
        verify_hash,
        wheel_provenance,
    )

    lock_path = ROOT / "lambda-wheels.lock.json"
    original_lock = load_lock(lock_path)
    lock = select_wheels(original_lock, operation)
    selected = wheel_provenance(lock)
    selected_names = {wheel["name"] for wheel in selected}
    if operation == "request":
        roots = [
            root for root in roots if canonicalize_name(Requirement(root).name) in selected_names
        ]
    records = []
    paths = []
    for wheel in lock["wheels"]:
        if locked.get(canonicalize_name(wheel["name"])) != wheel["version"]:
            raise SystemExit("Target wheel version differs from the approved Python lock.")
        upstream_hash = wheel["source"]["sha256"] if "source" in wheel else wheel["sha256"]
        if f"--hash=sha256:{upstream_hash}" not in lock_text:
            raise SystemExit("Target artifact is not bound to the approved upstream hashes.")
        path = ROOT / ".artifacts" / "wheelhouse" / wheel["filename"]
        if not path.is_file():
            raise SystemExit("Run scripts/build_lambda.py before auditing target wheel artifacts.")
        verify_hash(path, wheel["sha256"])
        validate_target_wheel(path.name)
        paths.append(path)
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            metadata_name = next(
                name
                for name in names
                if name.endswith(".dist-info/METADATA") and len(PurePosixPath(name).parts) == 2
            )
            metadata = Parser().parsestr(archive.read(metadata_name).decode())
            wheel_name = next(
                name
                for name in names
                if name.endswith(".dist-info/WHEEL") and len(PurePosixPath(name).parts) == 2
            )
            name = canonicalize_name(metadata["Name"])
            records.append(
                {
                    "name": name,
                    "version": metadata["Version"],
                    "filename": path.name,
                    "sha256": sha256(path),
                    "license_expression": metadata.get("License-Expression"),
                    "license_text": metadata.get("License"),
                    "license_classifiers": [
                        value
                        for value in metadata.get_all("Classifier", [])
                        if value.startswith("License ::")
                    ],
                    "license_files": [
                        name
                        for name in names
                        if "license" in name.lower() or "copying" in name.lower()
                    ],
                    "requires_dist": metadata.get_all("Requires-Dist", []),
                    "wheel": archive.read(wheel_name).decode(),
                }
            )
    validate_wheel_closure(paths, roots)
    return {
        "operation": operation,
        "architecture": lock["architecture"],
        "python": lock["python"],
        "platform": lock["platform"],
        "lock_sha256": sha256(lock_path),
        "selected_wheels": selected,
        "excluded_wheels": [
            wheel
            for wheel in wheel_provenance(original_lock)
            if wheel["name"] not in selected_names
        ],
        "closure_sha256": record_digest(selected),
        "closure_matches_runtime_roots": True,
        "all_wheel_hashes_verified": True,
        "native_imports_on_target": "Pending Linux x86_64 / Lambda execution.",
        "records": records,
    }


def audit_node_tooling() -> dict[str, object]:
    """Inventory the single integrity-locked CLI tarball and its bundled notices."""
    lock_path = ROOT / "package-lock.json"
    lock = json.loads(lock_path.read_text())
    project = json.loads((ROOT / "package.json").read_text())
    entries = lock["packages"]
    if set(entries) != {"", "node_modules/aws-cdk"}:
        raise SystemExit("Unexpected local npm dependency; review and update the audit scope.")
    entry = entries["node_modules/aws-cdk"]
    installed_path = ROOT / "node_modules" / "aws-cdk"
    installed = json.loads((installed_path / "package.json").read_text())
    if (
        installed["version"] != entry["version"]
        or entry["version"] != project["devDependencies"]["aws-cdk"]
        or not re.fullmatch(r"sha512-[A-Za-z0-9+/]+=*", entry["integrity"])
        or installed.get("dependencies")
    ):
        raise SystemExit("Local CLI does not match its exact integrity-locked package.")
    notices = (installed_path / "THIRD_PARTY_LICENSES").read_text()
    bundled = []
    for line in notices.splitlines():
        match = re.fullmatch(r"\*\* (.+) - (https://\S+) \| (.+)", line)
        if match:
            bundled.append({"package": match[1], "source": match[2], "license": match[3]})
    if not bundled:
        raise SystemExit("CDK CLI bundled third-party license inventory is missing.")
    return {
        "name": installed["name"],
        "version": installed["version"],
        "license": installed["license"],
        "tarball_integrity": entry["integrity"],
        "integrity_enforcement": "npm ci verifies package-lock.json integrity during installation.",
        "lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "third_party_notices_sha256": hashlib.sha256(notices.encode()).hexdigest(),
        "bundled_records": bundled,
    }


if __name__ == "__main__":
    main()

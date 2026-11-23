"""Validate local Lambda archives before CDK can stage or upload an asset."""

from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path
from typing import Literal

_COMMON_MODULES = {
    "semantic_core/__init__.py",
    "semantic_service/__init__.py",
    "semantic_service/runtime.py",
}
_PACKAGE_MODULES = {
    "shared": _COMMON_MODULES
    | {
        "semantic_service/search.py",
        "semantic_service/indexer.py",
        "semantic_service/enhancer.py",
    },
    "request": _COMMON_MODULES | {"semantic_service/request_runtime.py"},
}
_PACKAGE_MODULES["ecs"] = _PACKAGE_MODULES["shared"] | {
    "semantic_service/facade.py",
    "semantic_service/facade_lambda.py",
    "semantic_service/fallback.py",
}


def request_asset_from_context(package: str, value: object) -> Path | None:
    """The committed package setting enables REQUEST; context overrides only its path."""
    if value is not None and (
        not isinstance(value, str) or not value.strip() or value != value.strip()
    ):
        raise ValueError("request_asset must be a nonempty path string")
    if package == "shared":
        if value is not None:
            raise ValueError("request_asset requires persistent request_package=request")
        return None
    if package != "request":
        raise ValueError("request_package must be shared or request")
    return Path(value if isinstance(value, str) else ".artifacts/request.zip")


def ecs_asset_from_context(enabled: bool, value: object) -> Path | None:
    """Keep the existing shared ZIP pinned while deploying new backend functions."""
    if not enabled:
        if value is not None:
            raise ValueError("ecs_asset requires ecs_search configuration")
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("ecs_asset must be an explicit nonempty path string")
    return Path(value)


def shared_asset_from_context(enabled: bool, value: object, ecs_asset: Path | None) -> Path:
    """ECS rollout must name the preserved legacy ZIP independently of the new ZIP."""
    if value is None:
        if enabled:
            raise ValueError("asset must be an explicit legacy ZIP path when ECS Search is enabled")
        return Path(".artifacts/lambda.zip")
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("asset must be an explicit nonempty path string")
    result = Path(value)
    if ecs_asset is not None and result.resolve() == ecs_asset.resolve():
        raise ValueError("asset and ecs_asset must use different ZIP paths")
    return result


def validate_lambda_asset(path: Path, operation: Literal["shared", "request", "ecs"]) -> Path:
    """Require a regular ZIP containing the selected entrypoint and matching provenance."""
    label = "REQUEST" if operation == "request" else operation
    if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".zip":
        raise ValueError(f"{label} Lambda asset must be an existing regular ZIP file")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if len(names) != len(entries):
                raise ValueError(f"{label} Lambda asset has duplicate ZIP members")
            if missing := _PACKAGE_MODULES[operation] - names:
                raise ValueError(f"{label} Lambda asset is missing modules: {sorted(missing)}")
            for member in _PACKAGE_MODULES[operation]:
                info = archive.getinfo(member)
                if (
                    info.is_dir()
                    or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG)
                    or not archive.read(member)
                ):
                    raise ValueError(f"{label} Lambda asset requires regular module files")
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ValueError(f"{label} Lambda asset must be a readable ZIP file") from error

    manifest_path = path.with_suffix(".manifest.json")
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"{label} Lambda manifest is unreadable") from error
        expected_operation = "request" if operation == "request" else "all"
        with path.open("rb") as source:
            checksum = hashlib.file_digest(source, "sha256").hexdigest()
        if (
            not isinstance(manifest, dict)
            or manifest.get("operation") != expected_operation
            or manifest.get("artifact_sha256") != checksum
        ):
            raise ValueError(f"{label} Lambda manifest must match the operation and ZIP checksum")
    return path

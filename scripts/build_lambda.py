"""Build a deterministic CPython 3.12 x86_64 Lambda ZIP from hash-locked wheels."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NotRequired, TypedDict, cast

from packaging.requirements import Requirement
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parents[1]
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
REQUEST_WHEELS = frozenset(
    canonicalize_name(name)
    for name in {
        "anyio",
        "attrs",
        "botocore",
        "certifi",
        "cffi",
        "cryptography",
        "h11",
        "httpcore",
        "httpx",
        "idna",
        "jmespath",
        "jsonschema",
        "jsonschema-specifications",
        "pycparser",
        "pyjwt",
        "python-dateutil",
        "referencing",
        "rpds-py",
        "six",
        "typing-extensions",
        "urllib3",
    }
)
REQUEST_SOURCE = {
    "semantic_core": ("__init__.py", "_timing.py", "catalog.py", "py.typed"),
    "semantic_service": (
        "__init__.py",
        "_mcp_id.py",
        "_request_prefetch.py",
        "_search_contract.py",
        "auth.py",
        "config.py",
        "deployment_config.py",
        "interceptor.py",
        "py.typed",
        "request_runtime.py",
        "runtime.py",
        "secrets.py",
        "storage.py",
    ),
}
# The reduced archive shares these source files with other operations. Each
# exception identifies the exact enclosing scope, its reviewed AST, and why the
# deferred import is outside REQUEST. A body change requires re-evaluating that
# exception; a new import in another scope never inherits the exception.
# These checks are conservative import guards, not a proof of dynamic reachability.
REQUEST_IMPORT_EXCEPTIONS = {
    ("semantic_core.catalog", "ToolRecord.fields"): (
        {"semantic_core.bm25"},
        "06e80e8de19f398e9edc21fc2adf12b2db16806d9b691a4d210d0625c3bebdad",
        "Document fields are consumed only by retrieval, never REQUEST authorization.",
    ),
    ("semantic_service.config", "parse_search_config"): (
        {"semantic_core.search"},
        "a2c6a929e53bb1c1bfc7e6a8d244fbda7f607d0acca3e477bcd100a16026fe61",
        "Search settings are parsed only by search, model and enhancer factories.",
    ),
    ("semantic_service.runtime", "_aws"): (
        {"semantic_service.sdk"},
        "42dc52f362e634534d339438fc67427f3f4ee89a6e7599b8e213bf364f375273",
        "The SDK adapter is guarded by bedrock-agentcore-control; REQUEST uses S3/DDB/Secrets.",
    ),
    ("semantic_service.runtime", "_models"): (
        {"semantic_service.models"},
        "2169db346100b0393f94310920529bc8afa4f729e1b0091e73365e3606d1c9fc",
        "Model providers are used only by search and enhancer.",
    ),
    ("semantic_service.runtime", "_search"): (
        {"semantic_service.search"},
        "b3233f6f319ed1061423aa21da85012478523a39afa86e0c014cc591e8e1788c",
        "Only search_handler constructs the search service.",
    ),
    ("semantic_service.runtime", "_registry"): (
        {"semantic_service.catalog"},
        "3340275a9b0bb5a09ad89d17dafc734c8daace60044f77b554d47c6fbf148171",
        "Only indexer and enhancer construct the target registry.",
    ),
    ("semantic_service.runtime", "_indexer"): (
        {"semantic_service.catalog", "semantic_service.indexer"},
        "cf8a5639afef801126facf120dd96cb9a55d8a9f2bddd60b3b9717b361f63917",
        "Only indexer_handler constructs the indexer.",
    ),
    ("semantic_service.runtime", "_enhancer"): (
        {"semantic_service.enhancer"},
        "4db79c68522f5e2e9763c887c601f539ca59825ba0e15f3595dff3a74401f825",
        "Only enhancer_handler constructs the enhancer.",
    ),
    ("semantic_service.runtime", "_context"): (
        {"semantic_core.providers"},
        "c081ca624625c688c716eb3e3a512a0f5f3041d5e3b6af87fb9cb7bcf1bb342c",
        "Only indexer_handler and enhancer_handler construct provider contexts.",
    ),
    ("semantic_service.runtime", "indexer_handler"): (
        {"semantic_service._indexer_telemetry"},
        "c6d1591edcdfc8bd430f635180903b0c33b36b2efb613ef76f56c139e9d63dda",
        "This is the separately deployed INDEXER handler.",
    ),
    ("semantic_service.storage", "_check_snapshot_sizes"): (
        {"semantic_core.snapshot"},
        "c854c84e6a92afc8e3e5eb189b2a04f38f8b754f83a0f4e1ff7a86a736129d6f",
        "Snapshot size checks run only when publishing a retrieval snapshot.",
    ),
    ("semantic_service.storage", "SnapshotStore._checkpoint"): (
        {"semantic_core.providers"},
        "3d5ad875d18296a2b867a0c493cbfe55f0510cc9ee8474ae583f9b29f608c7a6",
        "Provider checkpoints run only on snapshot write/retrieval paths with a context.",
    ),
    ("semantic_service.storage", "SnapshotStore._save"): (
        {"semantic_core.snapshot"},
        "0ae62e49e5411a00a953f9e32c0eb739883d3292acb0d42f01715da67df5ab89",
        "Snapshot serialization is an indexer write operation.",
    ),
    ("semantic_service.storage", "SnapshotStore.load_snapshot"): (
        {"semantic_core.snapshot"},
        "921266b7b20c67d7f762d1bc4ecc83399f191fb70e7828f9b066e43729c032f4",
        "Only retrieval loads the index snapshot; REQUEST reads control/policy/safety.",
    ),
}
# Covers every existing syntactic reference (module, enclosing scope and AST)
# to the functions/properties above, including call arguments. A new REQUEST
# caller of an old deferred-import helper must trigger reachability review too.
# Do not regenerate this fingerprint without re-evaluating those call paths.
REQUEST_EXCEPTION_REFERENCES_SHA256 = (
    "7f13abf763add5ebd54ebac0c83b0c87ca8cd44d13f6adb579de04a88a44075c"
)


class SourceArtifact(TypedDict):
    """Immutable source used only for the pure Python jieba wheel."""

    filename: str
    url: str
    sha256: str


class WheelArtifact(TypedDict):
    """One exact target wheel; source is present only for jieba."""

    name: str
    version: str
    filename: str
    sha256: str
    url: NotRequired[str]
    source: NotRequired[SourceArtifact]


class Backend(TypedDict):
    name: str
    version: str


class BuildLock(TypedDict):
    schema_version: int
    python: str
    architecture: str
    platform: str
    source_date_epoch: int
    build_backend: Backend
    wheels: list[WheelArtifact]


def sha256(path: Path) -> str:
    """Hash the artifact without depending on its filesystem metadata."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_hash(path: Path, expected: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or sha256(path) != expected:
        raise ValueError(f"SHA-256 mismatch: {path.name}")


def validate_target_wheel(filename: str) -> None:
    """Reject host-native wheels and glibc newer than Lambda's AL2023 2.34."""
    platforms = [f"manylinux_2_{minor}_x86_64" for minor in range(34, 16, -1)]
    platforms.extend(["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"])
    allowed = set(cpython_tags((3, 12), abis=["cp312"], platforms=platforms))
    allowed.update(compatible_tags((3, 12), interpreter="cp312", platforms=platforms))
    if not parse_wheel_filename(filename)[3] & allowed:
        raise ValueError(f"Wheel is incompatible with CPython 3.12 / AL2023 x86_64: {filename}")


def load_lock(path: Path) -> BuildLock:
    lock = cast(BuildLock, json.loads(path.read_text()))
    if (
        lock["schema_version"] != 1
        or lock["python"] != "3.12"
        or lock["architecture"] != "x86_64"
        or lock["platform"] != "manylinux_2_34_x86_64"
        or lock["source_date_epoch"] != 315532800
    ):
        raise ValueError("Unsupported Lambda build target.")
    names = set()
    for wheel in lock["wheels"]:
        validate_target_wheel(wheel["filename"])
        name, version, _, _ = parse_wheel_filename(wheel["filename"])
        if name != canonicalize_name(wheel["name"]) or str(version) != wheel["version"]:
            raise ValueError("Wheel identity does not match the lock.")
        if name in names:
            raise ValueError("Duplicate distribution in Lambda wheel lock.")
        names.add(name)
    return lock


def select_wheels(lock: BuildLock, operation: Literal["all", "request"]) -> BuildLock:
    """Select the reviewed operation without changing the dependency lock."""
    # This API is also called from Python and does not inherit argparse's choices.
    if operation not in ("all", "request"):
        raise ValueError("Unsupported Lambda operation.")
    if operation == "all":
        return lock
    requested = {canonicalize_name(name) for name in REQUEST_WHEELS}
    wheels = [wheel for wheel in lock["wheels"] if canonicalize_name(wheel["name"]) in requested]
    missing = sorted(requested.difference(canonicalize_name(wheel["name"]) for wheel in wheels))
    if missing:
        raise ValueError(f"Incomplete REQUEST wheel closure: {missing}")
    return {**lock, "wheels": wheels}


def target_environment() -> dict[str, str]:
    """Evaluate metadata for Lambda CPython 3.12, independently of the build host."""
    return {
        "implementation_name": "cpython",
        "implementation_version": "3.12.0",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_release": "",
        "platform_system": "Linux",
        "platform_version": "",
        "python_full_version": "3.12.0",
        "platform_python_implementation": "CPython",
        "python_version": "3.12",
        "sys_platform": "linux",
        "extra": "",
    }


def wheel_metadata(path: Path) -> tuple[str, str, list[str]]:
    """Read target dependency metadata without importing any target code."""
    with zipfile.ZipFile(path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/METADATA") and len(PurePosixPath(name).parts) == 2
        ]
        if len(names) != 1:
            raise ValueError(f"Expected exactly one wheel METADATA: {path.name}")
        metadata = Parser().parsestr(archive.read(names[0]).decode())
    name = canonicalize_name(metadata.get("Name", ""))
    version = metadata.get("Version", "")
    filename_name, filename_version, _, _ = parse_wheel_filename(path.name)
    if name != filename_name or version != str(filename_version):
        raise ValueError(f"Wheel METADATA identity mismatch: {path.name}")
    return name, version, metadata.get_all("Requires-Dist", [])


def validate_wheel_closure(paths: list[Path], roots: list[str] | None = None) -> None:
    """Require all active target dependencies and requested extras to be present."""
    distributions = {}
    for path in paths:
        name, version, requirements = wheel_metadata(path)
        if name in distributions:
            raise ValueError(f"Duplicate target wheel: {name}")
        distributions[name] = (version, requirements)
    pending = [Requirement(value) for value in (roots if roots is not None else distributions)]
    visited: set[tuple[str, str]] = set()
    reached = set()
    environment = target_environment()
    while pending:
        requirement = pending.pop()
        if requirement.marker is not None and not requirement.marker.evaluate(environment):
            continue
        name = canonicalize_name(requirement.name)
        if name not in distributions or distributions[name][0] not in requirement.specifier:
            raise ValueError(f"Missing or incompatible target wheel: {requirement}")
        reached.add(name)
        for extra in {"", *requirement.extras}:
            if (name, extra) in visited:
                continue
            visited.add((name, extra))
            for value in distributions[name][1]:
                dependency = Requirement(value)
                if dependency.marker is None or dependency.marker.evaluate(
                    environment | {"extra": extra}
                ):
                    # The marker was evaluated in the parent distribution's extras.
                    dependency.marker = None
                    pending.append(dependency)
    if roots is not None and reached != set(distributions):
        raise ValueError(f"Unreachable target wheels: {sorted(set(distributions) - reached)}")


def wheel_provenance(lock: BuildLock) -> list[dict[str, str]]:
    """Canonical, auditable wheel selection tied to the unchanged upstream lock."""
    return sorted(
        (
            {
                "name": canonicalize_name(wheel["name"]),
                "version": wheel["version"],
                "filename": wheel["filename"],
                "sha256": wheel["sha256"],
            }
            for wheel in lock["wheels"]
        ),
        key=lambda wheel: wheel["name"],
    )


def record_digest(records: list[dict[str, str]]) -> str:
    """Hash canonical JSON records so selection and source changes are distinguishable."""
    return hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class _NoPackageRedirect(urllib.request.HTTPRedirectHandler):
    """Pinned artifact URLs must never authorize a second network destination."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        fp.close()
        raise ValueError("Package URL must use the public PyPI artifact host without redirects.")


def fetch(url: str, destination: Path, expected: str, *, offline: bool) -> Path:
    """Only access the public package host; never resolve AWS credentials or APIs."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "files.pythonhosted.org":
        raise ValueError("Package URL must use the public PyPI artifact host.")
    if destination.exists():
        verify_hash(destination, expected)
        return destination
    if offline:
        raise ValueError(f"Missing cached artifact in offline mode: {destination.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".download")
    try:
        opener = urllib.request.build_opener(_NoPackageRedirect())
        # Exact HTTPS host checked above; redirects rejected before another request.
        with opener.open(url, timeout=60) as response, temporary.open("wb") as out:
            shutil.copyfileobj(response, out)
        verify_hash(temporary, expected)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def build_jieba(wheel: WheelArtifact, lock: BuildLock, wheelhouse: Path, *, offline: bool) -> Path:
    """Build the sole source-only dependency with the pinned local backend."""
    if wheel["name"] != "jieba" or wheel["version"] != "0.42.1":
        raise ValueError("Source builds are limited to the reviewed pure Python jieba release.")
    wheelhouse = wheelhouse.resolve()
    backend = lock["build_backend"]
    if (
        backend != {"name": "setuptools", "version": "84.2.1"}
        or importlib.metadata.version(backend["name"]) != backend["version"]
    ):
        raise ValueError("Install the exact hash-locked requirements-build.lock backend.")
    source = wheel["source"]
    archive_path = fetch(
        source["url"], wheelhouse / source["filename"], source["sha256"], offline=offline
    )
    destination = wheelhouse / wheel["filename"]
    with tempfile.TemporaryDirectory(prefix="jieba-build-", dir=wheelhouse) as directory:
        root = Path(directory)
        with tarfile.open(archive_path) as archive:
            for member in archive.getmembers():
                if member.issym() or member.islnk():
                    raise ValueError("Source archive links are not permitted.")
            archive.extractall(root, filter="data")
        source_root = root / "jieba-0.42.1"
        output = root / "wheel"
        output.mkdir()
        # The subprocess gets no inherited environment or user HOME/configuration.
        # SOURCE_DATE_EPOCH fixes wheel timestamps; the source and backend are pinned.
        # Fixed backend code, hash-verified source and separate output argument.
        subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            [
                sys.executable,
                "-c",
                "import sys; import setuptools.build_meta as b; b.build_wheel(sys.argv[1])",
                str(output.resolve()),
            ],
            cwd=source_root,
            env={
                "SOURCE_DATE_EPOCH": str(lock["source_date_epoch"]),
                "PYTHONHASHSEED": "0",
                "TZ": "UTC",
                "HOME": str(root),
            },
            check=True,
            shell=False,  # nosec B603
            stdout=subprocess.DEVNULL,
        )
        built = output / wheel["filename"]
        verify_hash(built, wheel["sha256"])
        shutil.copyfile(built, destination)
    return destination


def wheel_paths(lock: BuildLock, wheelhouse: Path, *, offline: bool) -> list[Path]:
    wheelhouse.mkdir(parents=True, exist_ok=True)
    result = []
    for wheel in lock["wheels"]:
        path = wheelhouse / wheel["filename"]
        if "source" in wheel:
            # Rebuild even with a cached wheel: a source/backend mismatch must fail.
            path = build_jieba(wheel, lock, wheelhouse, offline=offline)
        else:
            path = fetch(wheel["url"], path, wheel["sha256"], offline=offline)
        result.append(path)
    return result


def unpack_wheel(path: Path, destination: Path) -> None:
    """Install wheel library files while rejecting traversal, links and scripts."""
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            relative = PurePosixPath(member.filename)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or "\\" in member.filename
                or stat.S_ISLNK(member.external_attr >> 16)
            ):
                raise ValueError(f"Unsafe wheel path: {member.filename}")
            if member.is_dir():
                continue
            parts = relative.parts
            if parts[0].endswith(".data"):
                if len(parts) < 3 or parts[1] not in {"purelib", "platlib"}:
                    # CLI scripts are not used by any Lambda handler.
                    if len(parts) > 2 and parts[1] == "scripts":
                        continue
                    raise ValueError(f"Unsupported wheel data location: {member.filename}")
                relative = PurePosixPath(*parts[2:])
            target = destination.joinpath(*relative.parts)
            if target.exists():
                raise ValueError(f"Wheel file collision: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            contents = archive.read(member)
            if relative.name.endswith(".so") or ".so." in relative.name:
                validate_native_binary(contents, relative.name)
            target.write_bytes(contents)


def validate_native_binary(contents: bytes, filename: str) -> None:
    """Check ELF class, endianness and machine, independently of wheel filenames."""
    if (
        contents[:6] != b"\x7fELF\x02\x01"
        or len(contents) < 20
        or int.from_bytes(contents[18:20], "little") != 62
    ):
        raise ValueError(f"Native library is not a Linux x86_64 ELF binary: {filename}")


def deterministic_zip(source: Path, output: Path) -> None:
    """Use stored ZIP entries so output does not vary with the host zlib version."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError("Symlinks are not permitted in the Lambda artifact.")
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(path.relative_to(source).as_posix(), ZIP_TIME)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, path.read_bytes())


def application_sources(operation: Literal["all", "request"]) -> list[Path]:
    """Expand selected files/directories and reject links or source-path escapes."""
    if operation not in ("all", "request"):
        raise ValueError("Unsupported Lambda operation.")
    source = ROOT / "src"
    selections = (
        [(package, filename) for package, names in REQUEST_SOURCE.items() for filename in names]
        if operation == "request"
        else [("semantic_core", ""), ("semantic_service", "")]
    )
    paths = set()
    for package, filename in selections:
        relative = PurePosixPath(package) / filename
        if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative):
            raise ValueError("Unsafe application source path.")
        selected = source / relative
        for ancestor in (selected, *selected.parents):
            if ancestor == source:
                break
            if ancestor.is_symlink():
                raise ValueError("Symlinks are not permitted in application sources.")
        if not selected.exists():
            raise ValueError(f"Missing application source: {relative}")
        for path in selected.rglob("*") if selected.is_dir() else [selected]:
            # The dictionary is regenerated below from the verified target wheel,
            # so old ignored source-tree dictionaries must never enter the ZIP.
            # REQUEST's dedicated entry is also excluded from the shared ZIP.
            excluded: tuple[str, ...] = ("__pycache__", "*.pyc", "*.pyo")
            if operation == "all":
                excluded += (
                    "jieba-prefix-*.json",
                    "request_runtime.py",
                    "_request_prefetch.py",
                )
            if any(
                fnmatch.fnmatch(part, pattern)
                for part in path.relative_to(source).parts
                for pattern in excluded
            ):
                continue
            if path.is_symlink():
                raise ValueError("Symlinks are not permitted in application sources.")
            if path.is_file():
                paths.add(path)
    return sorted(paths)


def validate_request_source(paths: list[Path]) -> None:
    """Reject runtime imports outside the selected first-party source closure."""
    source = ROOT / "src"
    modules = {
        ".".join(path.relative_to(source).with_suffix("").parts).removesuffix(".__init__")
        for path in paths
        if path.suffix == ".py"
    }
    first_party = {"semantic_core", "semantic_service", "semantic_eval"}
    deferred_names = {scope.rsplit(".", 1)[-1] for _, scope in REQUEST_IMPORT_EXCEPTIONS}
    deferred_functions = {scope for _, scope in REQUEST_IMPORT_EXCEPTIONS if "." not in scope}
    deferred_symbols = {f"{module}.{scope}" for module, scope in REQUEST_IMPORT_EXCEPTIONS}
    references: list[dict[str, str]] = []

    class ImportGuard(ast.NodeVisitor):
        def __init__(self, path: Path) -> None:
            self.module = ".".join(path.relative_to(source).with_suffix("").parts)
            self.package = self.module.rsplit(".", 1)[0]
            self.module = self.module.removesuffix(".__init__")
            self.scopes: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef] = []
            self.bindings: list[dict[str, str]] = [{}]

        def visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
            self.scopes.append(node)
            self.bindings.append({})
            self.generic_visit(node)
            self.bindings.pop()
            self.scopes.pop()

        visit_FunctionDef = visit_scope
        visit_AsyncFunctionDef = visit_scope
        visit_ClassDef = visit_scope

        def resolve_reference(self, node: ast.AST) -> str:
            """Resolve import aliases before matching a deferred function/property."""
            if isinstance(node, ast.Name):
                for bindings in reversed(self.bindings):
                    if node.id in bindings:
                        return bindings[node.id]
                return node.id
            if isinstance(node, ast.Attribute):
                prefix = self.resolve_reference(node.value)
                return f"{prefix}.{node.attr}" if prefix else node.attr
            return ""

        def bind_import(self, alias: ast.alias, target: str) -> None:
            self.bindings[-1][alias.asname or alias.name] = target
            if target in deferred_symbols:
                # Importing a callable is itself a reference, even if a callback
                # or a later-defined function invokes it under a different name.
                self.record_reference(alias, target.rsplit(".", 1)[-1])

        def record_reference(self, node: ast.AST, name: str) -> None:
            if name in deferred_names:
                references.append(
                    {
                        "module": self.module,
                        "scope": ".".join(scope.name for scope in self.scopes),
                        "ast": ast.dump(node, include_attributes=False),
                    }
                )

        def visit_Call(self, node: ast.Call) -> None:
            name = self.resolve_reference(node.func).rsplit(".", 1)[-1]
            if isinstance(node.func, ast.Attribute) or name in deferred_functions:
                self.record_reference(node, name)
            self.generic_visit(node)

        def visit_Name(self, node: ast.Name) -> None:
            name = self.resolve_reference(node).rsplit(".", 1)[-1]
            if isinstance(node.ctx, ast.Load) and name in deferred_functions:
                self.record_reference(node, name)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if isinstance(node.ctx, ast.Load):
                self.record_reference(node, self.resolve_reference(node).rsplit(".", 1)[-1])
            self.generic_visit(node)

        def visit_If(self, node: ast.If) -> None:
            type_only = (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING") or (
                isinstance(node.test, ast.Attribute)
                and isinstance(node.test.value, ast.Name)
                and node.test.value.id == "typing"
                and node.test.attr == "TYPE_CHECKING"
            )
            if type_only:
                for child in node.orelse:
                    self.visit(child)
            else:
                self.generic_visit(node)

        def check(self, target: str) -> None:
            if target.split(".", 1)[0] not in first_party or target in modules:
                return
            scope = ".".join(node.name for node in self.scopes)
            exception = REQUEST_IMPORT_EXCEPTIONS.get((self.module, scope))
            if exception is not None and self.scopes:
                imports, fingerprint, reason = exception
                actual = hashlib.sha256(
                    ast.dump(self.scopes[-1], include_attributes=False).encode()
                ).hexdigest()
                if target in imports and actual == fingerprint and reason:
                    return
            raise ValueError(f"REQUEST source closure excludes {target} in {self.module}:{scope}")

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                self.check(alias.name)
                # `import a.b` binds `a`; `import a.b as c` binds `c` to `a.b`.
                if alias.asname:
                    self.bind_import(alias, alias.name)
                else:
                    name = alias.name.split(".", 1)[0]
                    self.bindings[-1][name] = name

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            module = node.module or ""
            if node.level:
                module = importlib.util.resolve_name("." * node.level + module, self.package)
            self.check(module)
            for alias in node.names:
                candidate = f"{module}.{alias.name}"
                self.bind_import(alias, candidate)
                candidate_path = source.joinpath(*candidate.split("."))
                if candidate_path.with_suffix(".py").exists() or candidate_path.is_dir():
                    self.check(candidate)

    for path in paths:
        if path.suffix == ".py":
            ImportGuard(path).visit(ast.parse(path.read_text(), filename=str(path)))
    fingerprint = record_digest(references)
    if references and fingerprint != REQUEST_EXCEPTION_REFERENCES_SHA256:
        raise ValueError(
            "REQUEST source closure exception references changed; "
            f"review reachability: {fingerprint}"
        )


def copy_application(destination: Path, operation: Literal["all", "request"]) -> None:
    """Copy into a new/existing directory; reject all file/link collisions before writing."""
    # Preserve symlinks until the collision guard below rejects them.
    destination = destination.absolute()
    paths = application_sources(operation)
    if operation == "request":
        validate_request_source(paths)
    targets = [(path, destination / path.relative_to(ROOT / "src")) for path in paths]
    for _, target in targets:
        if target.exists() or target.is_symlink():
            raise ValueError(f"Application file collision: {target.relative_to(destination)}")
        for parent in target.parents:
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise ValueError("Application directory collision.")
            if parent == destination:
                break
    destination.mkdir(parents=True, exist_ok=True)
    for path, target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    if operation == "request":
        return
    # Build from the verified target wheel's text resource. The host must not
    # import target-native modules or depend on a warm runtime dictionary cache.
    # Fixed dictionary builder; absolute paths are separate argv entries.
    subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
        [
            sys.executable,
            str(ROOT / "scripts" / "build_dictionary.py"),
            "--dictionary",
            str(destination / "jieba" / "dict.txt"),
            "--output-dir",
            str(destination / "semantic_core" / "data"),
        ],
        env={
            "PATH": os.defpath,
            "PYTHONHASHSEED": "0",
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_EC2_METADATA_DISABLED": "true",
        },
        check=True,
        shell=False,  # nosec B603
    )


def build(
    lock_path: Path,
    wheelhouse: Path,
    output: Path,
    *,
    offline: bool,
    operation: Literal["all", "request"] = "all",
) -> dict[str, object]:
    """Materialize a function closure from the unchanged hash-locked target wheels."""
    # Runtime callers of this public helper do not inherit argparse's validation.
    if operation not in ("all", "request"):
        raise ValueError("Unsupported Lambda operation.")
    output = output.resolve()
    reserved = {"lambda.zip": "all", "request.zip": "request"}
    reserved_operation = reserved.get(output.name.casefold())
    if reserved_operation is not None and reserved_operation != operation:
        raise ValueError(f"Output {output.name} is reserved for operation {reserved_operation}.")
    if output.is_file() and output.stat().st_nlink > 1:
        raise ValueError("Lambda output has multiple hard links; choose a separate output file.")
    previous_manifest = output.with_suffix(".manifest.json")
    if previous_manifest.exists():
        previous = json.loads(previous_manifest.read_text())
        if not isinstance(previous, dict) or previous.get("operation", "all") != operation:
            raise ValueError("Existing output manifest belongs to a different operation.")
    original_lock = load_lock(lock_path)
    lock = select_wheels(original_lock, operation)
    paths = wheel_paths(lock, wheelhouse, offline=offline)
    if operation == "request":
        validate_wheel_closure(paths)
    sources = [
        {"path": path.relative_to(ROOT / "src").as_posix(), "sha256": sha256(path)}
        for path in application_sources(operation)
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lambda-build-", dir=output.parent) as directory:
        root = Path(directory)
        for path in paths:
            unpack_wheel(path, root)
        copy_application(root, operation)
        uncompressed = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
        if uncompressed >= 250 * 1024 * 1024:
            raise ValueError("Lambda package exceeds the 250 MiB uncompressed quota.")
        deterministic_zip(root, output)
    selected = wheel_provenance(lock)
    selected_names = {wheel["name"] for wheel in selected}
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
        "application_sources": sources,
        "source_sha256": record_digest(sources),
        "artifact_sha256": sha256(output),
        "artifact_bytes": output.stat().st_size,
        "uncompressed_bytes": uncompressed,
        "wheel_count": len(paths),
        "wheels": [{"filename": path.name, "sha256": sha256(path)} for path in paths],
        "live_runtime_import": "Pending Linux x86_64 / Lambda verification; host is not target.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=ROOT / "lambda-wheels.lock.json")
    parser.add_argument("--wheelhouse", type=Path, default=ROOT / ".artifacts" / "wheelhouse")
    parser.add_argument("--operation", choices=("all", "request"), default="all")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--offline", action="store_true", help="Use only verified cached artifacts."
    )
    args = parser.parse_args()
    if Path(sys.prefix).resolve() != (ROOT / ".venv").resolve() or sys.version_info[:2] != (3, 12):
        raise SystemExit("Run with this worktree's Python 3.12 .venv/bin/python.")
    default_name = "request.zip" if args.operation == "request" else "lambda.zip"
    output = (args.output or ROOT / ".artifacts" / default_name).resolve()
    if output.is_relative_to(ROOT / "evidence"):
        raise SystemExit("Do not modify the historical tracked evidence directory.")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = build(
        args.lock.resolve(),
        args.wheelhouse.resolve(),
        output,
        offline=args.offline,
        operation=args.operation,
    )
    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"PASS: {manifest['wheel_count']} locked wheels; SHA-256 {manifest['artifact_sha256']}")
    print(f"Artifact: {output}")


if __name__ == "__main__":
    main()

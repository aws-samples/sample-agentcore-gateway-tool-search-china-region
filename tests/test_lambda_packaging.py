"""Artifact boundary tests: incompatible binaries and archive tampering fail closed."""

from __future__ import annotations

import os
import stat
import subprocess  # nosec B404
import sys
import zipfile
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import build_lambda
from scripts.build_lambda import (
    deterministic_zip,
    fetch,
    unpack_wheel,
    validate_native_binary,
    validate_target_wheel,
    verify_hash,
)


@pytest.mark.parametrize(
    "filename",
    [
        "numpy-2.3.4-cp312-cp312-macosx_14_0_arm64.whl",
        "numpy-2.3.4-cp312-cp312-manylinux_2_28_aarch64.whl",
        "numpy-2.3.4-cp313-cp313-manylinux_2_28_x86_64.whl",
        "numpy-2.3.4-cp312-cp312-manylinux_2_35_x86_64.whl",
    ],
)
def test_reject_incompatible_native_wheel(filename: str) -> None:
    with pytest.raises(ValueError, match="incompatible"):
        validate_target_wheel(filename)


@pytest.mark.parametrize(
    "filename",
    [
        "numpy-2.3.4-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
        "cryptography-50.0.2-cp311-abi3-manylinux_2_34_x86_64.whl",
        "jieba-0.42.1-py3-none-any.whl",
    ],
)
def test_accept_target_wheels(filename: str) -> None:
    validate_target_wheel(filename)


def test_corrupt_cached_artifact_is_rejected_without_download(tmp_path: Path) -> None:
    artifact = tmp_path / "example.whl"
    artifact.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        fetch(
            "https://files.pythonhosted.org/packages/example.whl",
            artifact,
            "0" * 64,
            offline=True,
        )


def test_offline_missing_artifact_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Missing cached artifact"):
        fetch(
            "https://files.pythonhosted.org/packages/example.whl",
            tmp_path / "example.whl",
            "0" * 64,
            offline=True,
        )


def test_refuse_nonpackage_network_destination(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="public PyPI artifact host"):
        fetch("https://example.invalid/file", tmp_path / "file", "0" * 64, offline=False)


def test_application_copy_rejects_symlink_destination_root(monkeypatch, tmp_path):
    source = tmp_path / "project" / "src" / "semantic_service" / "__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text("# fixture\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "linked-root"
    destination.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(build_lambda, "ROOT", tmp_path / "project")
    monkeypatch.setattr(build_lambda, "application_sources", lambda operation: [source])
    monkeypatch.setattr(build_lambda, "validate_request_source", lambda paths: None)
    with pytest.raises(ValueError, match="directory collision"):
        build_lambda.copy_application(destination, "request")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "redirect",
    [
        "https://example.invalid/package.whl",
        "http://files.pythonhosted.org/package.whl",
        "https://files.pythonhosted.org@127.0.0.1/package.whl",
        "https://files.pythonhosted.org/packages/other.whl",
    ],
)
def test_package_download_rejects_redirect_before_opening_destination(
    monkeypatch, tmp_path, redirect
):
    """Exercise urllib's redirect dispatch without permitting any network."""
    import io
    import urllib.request
    from email.message import Message

    opened = []
    responses = []

    class RedirectResponse(urllib.request.HTTPSHandler, urllib.request.HTTPHandler):
        def https_open(self, request):
            opened.append(request.full_url)
            if len(opened) > 1:
                raise AssertionError("unapproved redirect was requested")
            headers = Message()
            headers["Location"] = redirect
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
            response.msg = "Found"
            responses.append(response)
            return response

        http_open = https_open

    original = urllib.request.build_opener
    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *handlers: original(RedirectResponse(), *handlers)
    )
    monkeypatch.setattr(urllib.request, "_opener", None)
    with pytest.raises(ValueError, match="public PyPI artifact host"):
        fetch(
            "https://files.pythonhosted.org/packages/example.whl",
            tmp_path / "file",
            "0" * 64,
            offline=False,
        )
    assert opened == ["https://files.pythonhosted.org/packages/example.whl"]
    assert responses[0].closed


@pytest.mark.parametrize("path", ["../outside.py", "/outside.py", "pkg\\outside.py"])
def test_wheel_cannot_escape_staging_directory(tmp_path: Path, path: str) -> None:
    artifact = tmp_path / "bad.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(path, b"code")
    with pytest.raises(ValueError, match="Unsafe wheel path"):
        unpack_wheel(artifact, tmp_path / "target")
    assert not (tmp_path / "outside.py").exists()


def test_wheel_cannot_introduce_symlink(tmp_path: Path) -> None:
    artifact = tmp_path / "bad.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        info = zipfile.ZipInfo("pkg/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, b"/outside.py")
    with pytest.raises(ValueError, match="Unsafe wheel path"):
        unpack_wheel(artifact, tmp_path / "target")


def test_zip_is_independent_of_input_timestamps_and_modes(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    program = source / "handler.py"
    program.write_text("answer = 42\n")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    deterministic_zip(source, first)
    os.utime(program, (1700000000, 1700000000))
    program.chmod(0o755)
    deterministic_zip(source, second)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        info = archive.getinfo("handler.py")
        assert info.date_time == (1980, 1, 1, 0, 0, 0)
        assert info.external_attr >> 16 == stat.S_IFREG | 0o644
        assert info.compress_type == zipfile.ZIP_STORED


def test_hash_validation_rejects_malformed_pin(tmp_path: Path) -> None:
    artifact = tmp_path / "file"
    artifact.write_bytes(b"data")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_hash(artifact, "not-a-hash")


def test_native_library_architecture_is_verified_from_elf_header() -> None:
    contents = bytearray(64)
    contents[:6] = b"\x7fELF\x02\x01"
    contents[18:20] = (183).to_bytes(2, "little")  # AArch64, despite a possible x86 wheel tag.
    with pytest.raises(ValueError, match="Linux x86_64"):
        validate_native_binary(bytes(contents), "library.so")
    contents[18:20] = (62).to_bytes(2, "little")
    validate_native_binary(bytes(contents), "library.so")


def test_lambda_build_regenerates_dictionary_without_copying_stale_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧工作树生成物不能改变同一提交的 Lambda 包内容。"""
    lock_path = build_lambda.ROOT / "lambda-wheels.lock.json"
    project = tmp_path / "project"
    for package in ("semantic_core", "semantic_service"):
        source = project / "src" / package
        source.mkdir(parents=True)
        (source / "__init__.py").write_text('"""固定源码。"""\n')
    data = project / "src/semantic_core/data"
    data.mkdir()
    (data / "jieba-prefix-v1.json").write_bytes(b"stale v1")
    (data / "jieba-prefix-v2.json").write_bytes(b"stale v2")
    (data / "other-resource.json").write_bytes(b"preserved resource")
    monkeypatch.setattr(build_lambda, "ROOT", project)
    monkeypatch.setattr(build_lambda, "wheel_paths", lambda *args, **kwargs: [])

    def generate_dictionary(command, **kwargs):
        destination = Path(command[command.index("--output-dir") + 1])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "jieba-prefix-v2.json").write_bytes(b"fresh dictionary")

    monkeypatch.setattr(build_lambda, "subprocess", SimpleNamespace(run=generate_dictionary))
    output = tmp_path / "lambda.zip"
    build_lambda.build(lock_path, tmp_path / "wheelhouse", output, offline=True)
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert "semantic_core/data/jieba-prefix-v1.json" not in names
        assert archive.read("semantic_core/data/jieba-prefix-v2.json") == b"fresh dictionary"
        assert archive.read("semantic_core/data/other-resource.json") == b"preserved resource"


@pytest.mark.parametrize(
    "filename",
    [
        "request_runtime.py",
        "_request_prefetch.py",
    ],
)
def test_request_only_source_does_not_change_the_shared_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    """Adding the REQUEST entry point must not redeploy the other three functions."""
    lock_path = build_lambda.ROOT / "lambda-wheels.lock.json"
    project = tmp_path / "project"
    for package in ("semantic_core", "semantic_service"):
        source = project / "src" / package
        source.mkdir(parents=True)
        (source / "__init__.py").write_text('"""Application package."""\n')
    monkeypatch.setattr(build_lambda, "ROOT", project)
    monkeypatch.setattr(build_lambda, "wheel_paths", lambda *args, **kwargs: [])
    monkeypatch.setattr(build_lambda, "subprocess", SimpleNamespace(run=lambda *a, **kw: None))
    before, after = tmp_path / "before.zip", tmp_path / "after.zip"
    build_lambda.build(lock_path, tmp_path / "wheelhouse", before, offline=True)
    (project / "src/semantic_service" / filename).write_text('"""REQUEST-only initialization."""\n')
    build_lambda.build(lock_path, tmp_path / "wheelhouse", after, offline=True)
    assert before.read_bytes() == after.read_bytes()


def test_request_artifact_has_only_its_locked_dependencies_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQUEST does not ship retrieval dependencies or generate a tokenizer dictionary."""
    lock_path = build_lambda.ROOT / "lambda-wheels.lock.json"
    project = tmp_path / "project"
    source_files = {
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
    expected_source = set()
    for package, files in source_files.items():
        directory = project / "src" / package
        directory.mkdir(parents=True)
        for filename in files:
            (directory / filename).write_text("# reviewed REQUEST source\n")
            expected_source.add(f"{package}/{filename}")
        (directory / "unused.py").write_text("import numpy\nimport jieba\n")
        (directory / "__pycache__").mkdir()
        (directory / "__pycache__/stale.pyc").write_bytes(b"host cache")
    data = project / "src/semantic_core/data"
    data.mkdir()
    (data / "jieba-prefix-v2.json").write_bytes(b"unneeded dictionary")
    selected = []

    def cached_wheels(lock, directory, *, offline):
        assert offline is True
        directory.mkdir()
        paths = []
        for wheel in lock["wheels"]:
            selected.append(wheel["name"])
            path = directory / wheel["filename"]
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(f"{wheel['name']}/dependency.txt", wheel["version"])
                archive.writestr(
                    f"{wheel['name']}-{wheel['version']}.dist-info/METADATA",
                    f"Name: {wheel['name']}\nVersion: {wheel['version']}\n",
                )
            paths.append(path)
        return paths

    def no_dictionary(*args, **kwargs):
        raise AssertionError("REQUEST must not run the tokenizer dictionary builder")

    monkeypatch.setattr(build_lambda, "ROOT", project)
    monkeypatch.setattr(build_lambda, "wheel_paths", cached_wheels)
    monkeypatch.setattr(build_lambda, "subprocess", SimpleNamespace(run=no_dictionary))
    output = tmp_path / "request.zip"
    manifest = build_lambda.build(
        lock_path, tmp_path / "wheelhouse", output, offline=True, operation="request"
    )
    assert set(selected) == {
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
    assert manifest["wheel_count"] == 21
    assert manifest["operation"] == "request"
    assert manifest["lock_sha256"] == build_lambda.sha256(lock_path)
    with zipfile.ZipFile(output) as archive:
        source = {
            name
            for name in archive.namelist()
            if name.startswith(("semantic_core/", "semantic_service/"))
        }
    assert source == expected_source


@pytest.mark.parametrize("initialization_type", ("on-demand", "provisioned-concurrency"))
def test_request_package_runs_with_only_its_dependency_closure(
    tmp_path: Path, initialization_type: str
) -> None:
    """Use host-native locked wheels; Lambda's Linux native import is a separate gate."""
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    build_lambda.copy_application(isolated, "request")
    for name in build_lambda.REQUEST_WHEELS:
        distribution = metadata.distribution(name)
        entries = {Path(str(file)).parts[0] for file in distribution.files or ()}
        for entry in entries - {"..", "__pycache__"}:
            source = Path(distribution.locate_file(entry))
            target = isolated / entry
            if source.exists() and not target.exists():
                target.symlink_to(source, target_is_directory=source.is_dir())
    script = Path("tests/fixtures/request-package-smoke.py").resolve()
    # Fixed local test program and fixture paths; argv is not request-controlled.
    # nosemgrep: dangerous-subprocess-use-audit
    result = subprocess.run(  # nosec B603
        [sys.executable, "-I", "-S", str(script), str(isolated)],
        env={
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_ACCESS_KEY_ID": "isolated-fixture",
            "AWS_SECRET_ACCESS_KEY": "isolated-fixture",
            "AWS_REGION": "cn-north-1",
            "AWS_LAMBDA_INITIALIZATION_TYPE": initialization_type,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: isolated REQUEST factory, JWT and handler" in result.stdout
    if initialization_type == "provisioned-concurrency":
        assert "PASS: parent-process metadata with independent HTTP client" in result.stdout


def _request_build_fixture(tmp_path, monkeypatch, *, requirements=(), names=("root",)):
    """Use real wheel METADATA so closure tests exercise the artifact boundary."""
    import json

    project = tmp_path / "project"
    source = project / "src/semantic_service"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("")
    (source / "request_runtime.py").write_text("def request_handler():\n    return None\n")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheels = []
    for name in names:
        filename = f"{name}-1.0-py3-none-any.whl"
        path = wheelhouse / filename
        with zipfile.ZipFile(path, "w") as archive:
            dependencies = requirements if name == names[0] else ()
            text = f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
            text += "".join(f"Requires-Dist: {value}\n" for value in dependencies)
            archive.writestr(f"{name}-1.0.dist-info/METADATA", text)
            archive.writestr(f"{name}-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        wheels.append(
            {
                "name": name,
                "version": "1.0",
                "filename": filename,
                "sha256": build_lambda.sha256(path),
                "url": f"https://files.pythonhosted.org/packages/{filename}",
            }
        )
    lock = {
        "schema_version": 1,
        "python": "3.12",
        "architecture": "x86_64",
        "platform": "manylinux_2_34_x86_64",
        "source_date_epoch": 315532800,
        "build_backend": {"name": "setuptools", "version": "84.2.1"},
        "wheels": wheels,
    }
    lock_path = project / "lambda-wheels.lock.json"
    lock_path.write_text(json.dumps(lock))
    monkeypatch.setattr(build_lambda, "ROOT", project)
    monkeypatch.setattr(build_lambda, "REQUEST_WHEELS", frozenset(names))
    monkeypatch.setattr(
        build_lambda, "REQUEST_SOURCE", {"semantic_service": ("__init__.py", "request_runtime.py")}
    )
    return lock_path, wheelhouse, tmp_path / "request.zip"


@pytest.mark.parametrize(
    "requirement",
    [
        "missing>=1",
        "missing; sys_platform == 'linux'",
        "missing; python_version == '3.12'",
        "missing; platform_machine == 'x86_64'",
        "missing; implementation_name == 'cpython'",
    ],
)
def test_request_build_rejects_unclosed_target_metadata(tmp_path, monkeypatch, requirement):
    arguments = _request_build_fixture(tmp_path, monkeypatch, requirements=(requirement,))
    with pytest.raises(ValueError, match="Missing or incompatible target wheel"):
        build_lambda.build(*arguments, offline=True, operation="request")
    assert not arguments[2].exists()


def test_request_build_rejects_incompatible_transitive_version(tmp_path, monkeypatch):
    arguments = _request_build_fixture(
        tmp_path, monkeypatch, requirements=("dependency>=2",), names=("root", "dependency")
    )
    with pytest.raises(ValueError, match="Missing or incompatible target wheel"):
        build_lambda.build(*arguments, offline=True, operation="request")


@pytest.mark.parametrize(
    "requirement", ["missing; sys_platform == 'darwin'", "missing; extra == 'test'"]
)
def test_request_closure_uses_target_markers_without_optional_extras(
    tmp_path, monkeypatch, requirement
):
    arguments = _request_build_fixture(tmp_path, monkeypatch, requirements=(requirement,))
    build_lambda.build(*arguments, offline=True, operation="request")


@pytest.mark.parametrize(
    "source",
    [
        "from semantic_service.unshipped import work\n",
        # Adjacent literals intentionally form one code snippet or ARN value.
        # nosemgrep: string-concat-in-list
        "def request_handler():\n"
        "    from semantic_service.unshipped import work\n    return work()\n",
        "from . import unshipped\n",
    ],
)
def test_request_build_rejects_new_unshipped_runtime_import(tmp_path, monkeypatch, source):
    arguments = _request_build_fixture(tmp_path, monkeypatch)
    package = build_lambda.ROOT / "src/semantic_service"
    (package / "request_runtime.py").write_text(source)
    (package / "unshipped.py").write_text("def work():\n    return None\n")
    with pytest.raises(ValueError, match="REQUEST source closure"):
        build_lambda.build(*arguments, offline=True, operation="request")


def test_request_source_guard_accepts_type_only_import(tmp_path, monkeypatch):
    arguments = _request_build_fixture(tmp_path, monkeypatch)
    package = build_lambda.ROOT / "src/semantic_service"
    (package / "request_runtime.py").write_text(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
        "    from semantic_service.unshipped import work\n"
    )
    build_lambda.build(*arguments, offline=True, operation="request")


@pytest.mark.parametrize(
    "operation,filename",
    [("request", "lambda.zip"), ("all", "request.zip"), ("request", "LAMBDA.ZIP")],
)
def test_operation_cannot_overwrite_the_other_reserved_artifact(
    tmp_path, monkeypatch, operation, filename
):
    arguments = _request_build_fixture(tmp_path, monkeypatch)
    destination = tmp_path / filename
    destination.write_bytes(b"keep deployed artifact")
    with pytest.raises(ValueError, match="reserved"):
        build_lambda.build(*arguments[:2], destination, offline=True, operation=operation)
    assert destination.read_bytes() == b"keep deployed artifact"


def test_operation_cannot_replace_an_existing_other_operation_manifest(tmp_path, monkeypatch):
    arguments = _request_build_fixture(tmp_path, monkeypatch)
    destination = tmp_path / "custom.zip"
    destination.write_bytes(b"keep deployed artifact")
    destination.with_suffix(".manifest.json").write_text('{"operation": "all"}')
    with pytest.raises(ValueError, match="operation"):
        build_lambda.build(*arguments[:2], destination, offline=True, operation="request")
    assert destination.read_bytes() == b"keep deployed artifact"


def test_build_dynamic_api_rejects_invalid_operation_before_io(tmp_path):
    """Python callers can supply runtime strings despite the Literal type annotation."""
    with pytest.raises(ValueError, match="Unsupported Lambda operation"):
        build_lambda.build(
            tmp_path / "missing.lock",
            tmp_path,
            tmp_path / "custom.zip",
            offline=True,
            operation="unsupported",
        )


def test_request_copy_supports_new_destination_and_nested_resources(tmp_path, monkeypatch):
    _request_build_fixture(tmp_path, monkeypatch)
    package = build_lambda.ROOT / "src/semantic_service"
    (package / "schemas/nested").mkdir(parents=True)
    (package / "schemas/nested/schema.json").write_text('{"type": "string"}')
    monkeypatch.setattr(
        build_lambda, "REQUEST_SOURCE", {"semantic_service": ("__init__.py", "schemas")}
    )
    destination = tmp_path / "new/target"
    build_lambda.copy_application(destination, "request")
    assert (
        destination / "semantic_service/schemas/nested/schema.json"
    ).read_text() == '{"type": "string"}'


def test_request_copy_preserves_existing_dependency_directory(tmp_path, monkeypatch):
    _request_build_fixture(tmp_path, monkeypatch)
    destination = tmp_path / "target"
    package = destination / "semantic_service"
    package.mkdir(parents=True)
    (package / "wheel-resource.txt").write_text("keep")
    build_lambda.copy_application(destination, "request")
    assert (package / "wheel-resource.txt").read_text() == "keep"
    assert (package / "request_runtime.py").is_file()


def test_request_copy_rejects_collisions_before_copying_any_file(tmp_path, monkeypatch):
    _request_build_fixture(tmp_path, monkeypatch)
    destination = tmp_path / "target"
    package = destination / "semantic_service"
    package.mkdir(parents=True)
    (package / "request_runtime.py").write_text("keep")
    with pytest.raises(ValueError, match="collision"):
        build_lambda.copy_application(destination, "request")
    assert sorted(path.name for path in package.iterdir()) == ["request_runtime.py"]
    assert (package / "request_runtime.py").read_text() == "keep"


def test_request_manifest_distinguishes_selected_closure_and_source(tmp_path, monkeypatch):
    arguments = _request_build_fixture(tmp_path, monkeypatch, names=("root", "excluded"))
    monkeypatch.setattr(build_lambda, "REQUEST_WHEELS", frozenset({"root"}))
    manifest = build_lambda.build(*arguments, offline=True, operation="request")
    assert [wheel["name"] for wheel in manifest["selected_wheels"]] == ["root"]
    assert [wheel["name"] for wheel in manifest["excluded_wheels"]] == ["excluded"]
    assert manifest["closure_sha256"] != manifest["lock_sha256"]
    assert {item["path"] for item in manifest["application_sources"]} == {
        "semantic_service/__init__.py",
        "semantic_service/request_runtime.py",
    }
    assert manifest["source_sha256"]


def test_request_distribution_allowlist_is_normalized(tmp_path, monkeypatch):
    arguments = _request_build_fixture(tmp_path, monkeypatch, names=("pyjwt", "typing_extensions"))
    monkeypatch.setattr(build_lambda, "REQUEST_WHEELS", frozenset({"PyJWT", "typing_extensions"}))
    manifest = build_lambda.build(*arguments, offline=True, operation="request")
    assert manifest["wheel_count"] == 2


def test_request_source_exception_does_not_allow_changed_execution_guard(tmp_path, monkeypatch):
    """A formerly unreachable SDK import must not become reachable under its old waiver."""
    import shutil

    project = tmp_path / "project"
    for path in build_lambda.application_sources("request"):
        target = project / "src" / path.relative_to(build_lambda.ROOT / "src")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    runtime = project / "src/semantic_service/runtime.py"
    original = runtime.read_text()
    assert 'if service == "bedrock-agentcore-control":' in original
    runtime.write_text(original.replace('if service == "bedrock-agentcore-control":', "if True:"))
    monkeypatch.setattr(build_lambda, "ROOT", project)
    with pytest.raises(ValueError, match="REQUEST source closure.*semantic_service.sdk"):
        build_lambda.copy_application(tmp_path / "target", "request")
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("kind", ["source", "destination"])
def test_request_copy_rejects_links_without_modifying_the_target(tmp_path, monkeypatch, kind):
    _request_build_fixture(tmp_path, monkeypatch)
    destination = tmp_path / "target"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if kind == "source":
        source = build_lambda.ROOT / "src/semantic_service/request_runtime.py"
        source.unlink()
        source.symlink_to(outside / "secret.py")
    else:
        (destination / "semantic_service").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlinks|collision"):
        build_lambda.copy_application(destination, "request")
    assert list(outside.iterdir()) == []


def test_request_copy_repeated_call_fails_before_writing(tmp_path, monkeypatch):
    _request_build_fixture(tmp_path, monkeypatch)
    destination = tmp_path / "target"
    build_lambda.copy_application(destination, "request")
    before = {str(path): path.read_bytes() for path in destination.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="collision"):
        build_lambda.copy_application(destination, "request")
    after = {str(path): path.read_bytes() for path in destination.rglob("*") if path.is_file()}
    assert after == before


def test_request_output_symlink_cannot_bypass_reserved_artifact(tmp_path, monkeypatch):
    arguments = _request_build_fixture(tmp_path, monkeypatch)
    shared = tmp_path / "lambda.zip"
    shared.write_bytes(b"keep")
    indirect = tmp_path / "custom.zip"
    indirect.symlink_to(shared)
    with pytest.raises(ValueError, match="reserved"):
        build_lambda.build(*arguments[:2], indirect, offline=True, operation="request")
    assert shared.read_bytes() == b"keep"


def test_requested_dependency_extra_is_part_of_the_target_closure(tmp_path):
    artifacts = []
    for name, requirements in (
        ("root", ["dependency[tls]"]),
        ("dependency", ["missing; extra == 'tls'"]),
    ):
        artifact = tmp_path / f"{name}-1.0-py3-none-any.whl"
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr(
                f"{name}-1.0.dist-info/METADATA",
                f"Name: {name}\nVersion: 1.0\n"
                + "".join(f"Requires-Dist: {value}\n" for value in requirements),
            )
        artifacts.append(artifact)
    with pytest.raises(ValueError, match="Missing or incompatible target wheel: missing"):
        build_lambda.validate_wheel_closure(artifacts)


def test_target_metadata_identity_must_match_the_wheel_filename(tmp_path):
    artifact = tmp_path / "root-1.0-py3-none-any.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("root-1.0.dist-info/METADATA", "Name: unrelated\nVersion: 1.0\n")
    with pytest.raises(ValueError, match="METADATA identity mismatch"):
        build_lambda.validate_wheel_closure([artifact])


def test_target_closure_ignores_vendored_distribution_metadata(tmp_path):
    """The locked setuptools wheel also contains nested vendor dist-info directories."""
    artifact = tmp_path / "root-1.0-py3-none-any.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(
            "root/_vendor/vendor-2.0.dist-info/METADATA", "Name: vendor\nVersion: 2.0\n"
        )
        archive.writestr("root-1.0.dist-info/METADATA", "Name: root\nVersion: 1.0\n")
    build_lambda.validate_wheel_closure([artifact], ["root==1.0"])


def test_request_source_guard_rejects_a_new_call_to_a_deferred_dependency(tmp_path, monkeypatch):
    """Adding a REQUEST read-path call must not inherit an unrelated lazy-import exception."""
    import shutil

    project = tmp_path / "project"
    for path in build_lambda.application_sources("request"):
        target = project / "src" / path.relative_to(build_lambda.ROOT / "src")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    storage = project / "src/semantic_service/storage.py"
    original = storage.read_text()
    marker = "    def _get(self, reference: ObjectRef, *, limit: int = _OBJECT_LIMIT) -> bytes:\n"
    assert marker in original
    storage.write_text(original.replace(marker, marker + "        self._checkpoint(object())\n"))
    monkeypatch.setattr(build_lambda, "ROOT", project)
    with pytest.raises(ValueError, match="REQUEST source closure.*references"):
        build_lambda.copy_application(tmp_path / "target", "request")
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize(
    "import_statement,reference",
    [
        ("from semantic_service.runtime import _context as hidden_context", "hidden_context"),
        ("import semantic_service.runtime as hidden_runtime", "hidden_runtime._context"),
        ("from semantic_service import runtime as hidden_runtime", "hidden_runtime._context"),
    ],
)
def test_request_source_guard_resolves_import_aliases(
    tmp_path, monkeypatch, import_statement, reference
):
    import shutil

    project = tmp_path / "project"
    for path in build_lambda.application_sources("request"):
        target = project / "src" / path.relative_to(build_lambda.ROOT / "src")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    entry = project / "src/semantic_service/request_runtime.py"
    entry.write_text(
        entry.read_text()
        + f"\n{import_statement}\n"
        + f"def request_handler(event, context):\n    return {reference}(context, 1.0)\n"
    )
    monkeypatch.setattr(build_lambda, "ROOT", project)
    with pytest.raises(ValueError, match="REQUEST source closure.*references"):
        build_lambda.copy_application(tmp_path / "target", "request")
    assert not (tmp_path / "target").exists()


def test_request_output_hardlink_cannot_overwrite_shared_inode(tmp_path, monkeypatch):
    arguments = _request_build_fixture(tmp_path, monkeypatch)
    shared = tmp_path / "lambda.zip"
    shared.write_bytes(b"keep deployed shared archive")
    indirect = tmp_path / "custom.zip"
    os.link(shared, indirect)
    with pytest.raises(ValueError, match="hard link"):
        build_lambda.build(*arguments[:2], indirect, offline=True, operation="request")
    assert shared.read_bytes() == b"keep deployed shared archive"
    assert indirect.read_bytes() == shared.read_bytes()

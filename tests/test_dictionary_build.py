"""构建产物必须自包含，不能依赖运行时重新生成分词字典。"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import shutil
import subprocess  # nosec B404
import sys
import tarfile
import tomllib
import venv
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = "semantic_core/data/jieba-prefix-v2.json"
EXPECTED_SHA256 = "5556137584bb76827f0aee36254839e2f4efb43ea894f0cba94968007d8b5521"
LICENSE_PATH = "build_inputs/jieba-0.42.1/LICENSE"
LICENSE_SHA256 = "18ba0984839f85853b29fadaf992f7dba8fd0ca0fbeae34de2b8735222dc7a37"


def assert_upstream_license(raw: bytes) -> None:
    assert hashlib.sha256(raw).hexdigest() == LICENSE_SHA256
    assert b"Copyright (c) 2013 Sun Junyi" in raw
    assert b"Permission is hereby granted, free of charge" in raw
    assert b"THE SOFTWARE IS PROVIDED" in raw


def assert_wheel_license(archive: zipfile.ZipFile) -> None:
    matches = [
        name for name in archive.namelist() if name.endswith(f".dist-info/licenses/{LICENSE_PATH}")
    ]
    assert len(matches) == 1, "wheel 缺少上游完整 MIT 许可证"
    assert_upstream_license(archive.read(matches[0]))
    metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    assert len(metadata) == 1
    assert f"License-File: {LICENSE_PATH}\n".encode() in archive.read(metadata[0])


def isolated_environment(directory: Path) -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "HOME": str(directory),
        "TMPDIR": str(directory),
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "SOURCE_DATE_EPOCH": "315532800",
        "PYTHONHASHSEED": "0",
    }


def copy_build_project(project: Path) -> None:
    shutil.copytree(
        ROOT / "src",
        project / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "data", "*.egg-info"),
    )
    (project / "scripts").mkdir()
    for name in ("pyproject.toml", "README.md", "setup.py", "MANIFEST.in"):
        if (ROOT / name).exists():
            shutil.copyfile(ROOT / name, project / name)
    for name in ("build_dictionary.py", "verify_wheel.py"):
        if (ROOT / "scripts" / name).exists():
            shutil.copyfile(ROOT / "scripts" / name, project / "scripts" / name)
    if (ROOT / "build_inputs").exists():
        shutil.copytree(ROOT / "build_inputs", project / "build_inputs")
    (project / ".uv-cache").mkdir()


@pytest.fixture(scope="module")
def packaged_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = tmp_path_factory.mktemp("dictionary-package")
    copy_build_project(project)
    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-c",
            "import setuptools.build_meta as backend; backend.build_wheel('dist')",
        ],
        cwd=project,
        env=isolated_environment(project),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return project


@pytest.fixture(scope="module")
def setuptools_only_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """真正独立的虚拟环境，只复制已声明且已安装的 setuptools 分发物。"""
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["build-system"]["requires"]
    assert declared == ["setuptools==80.9.0"]
    directory = tmp_path_factory.mktemp("declared-build-environment")
    venv.EnvBuilder(with_pip=False, symlinks=True).create(directory)
    python = directory / "bin" / "python"
    site_packages = directory / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_packages /= "site-packages"
    distribution = importlib.metadata.distribution("setuptools")
    assert distribution.version == "80.9.0"
    for name in sorted({str(file).split("/")[0] for file in distribution.files or ()}):
        assert name not in {"", ".", ".."}
        source = Path(str(distribution.locate_file(name)))
        if source.is_dir():
            shutil.copytree(source, site_packages / name)
        else:
            shutil.copyfile(source, site_packages / name)
    result = subprocess.run(  # nosec B603
        [
            str(python),
            "-c",
            "import importlib.metadata as m, importlib.util as u, json; "
            "installed = {d.metadata['Name']: d.version for d in m.distributions()}; "
            "assert installed == {'setuptools': '80.9.0'}, installed; "
            "assert u.find_spec('jieba') is None; print(json.dumps(installed))",
        ],
        env=isolated_environment(directory),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    print(f"真实构建隔离环境：{result.stdout.strip()}；jieba 不可导入。")
    return python


@pytest.mark.parametrize("source_kind", ["source", "sdist"])
def test_declared_build_dependencies_are_sufficient(
    setuptools_only_python: Path, tmp_path: Path, source_kind: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    copy_build_project(project)
    if source_kind == "sdist":
        result = subprocess.run(  # nosec B603
            [
                str(setuptools_only_python),
                "-c",
                "import setuptools.build_meta as b; "
                "assert b.get_requires_for_build_sdist({}) == []; b.build_sdist('sdist')",
            ],
            cwd=project,
            env=isolated_environment(project),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        source_archive = next((project / "sdist").glob("*.tar.gz"))
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        with tarfile.open(source_archive) as archive:
            archive.extractall(extracted, filter="data")
        project = next(extracted.iterdir())
    fixed_input = project / "build_inputs" / "jieba-0.42.1"
    assert hashlib.sha256((fixed_input / "dict.txt").read_bytes()).hexdigest() == (
        "7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8"
    )
    assert "License: MIT" in (fixed_input / "PKG-INFO").read_text()
    assert_upstream_license((fixed_input / "LICENSE").read_bytes())
    result = subprocess.run(  # nosec B603
        [
            str(setuptools_only_python),
            "-c",
            "import setuptools.build_meta as b; "
            "assert b.get_requires_for_build_wheel({}) == []; b.build_wheel('dist')",
        ],
        cwd=project,
        env=isolated_environment(project),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with zipfile.ZipFile(next((project / "dist").glob("*.whl"))) as archive:
        assert hashlib.sha256(archive.read(ARTIFACT)).hexdigest() == EXPECTED_SHA256
        assert_wheel_license(archive)


def test_upstream_license_is_exact_pinned_copy() -> None:
    assert (ROOT / LICENSE_PATH).is_file(), "构建输入缺少上游完整 MIT 许可证"
    assert_upstream_license((ROOT / LICENSE_PATH).read_bytes())


def test_wheel_contains_upstream_license(packaged_project: Path) -> None:
    with zipfile.ZipFile(next((packaged_project / "dist").glob("*.whl"))) as archive:
        assert_wheel_license(archive)


def test_source_distribution_contains_upstream_license(packaged_project: Path) -> None:
    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-c",
            "import setuptools.build_meta as backend; backend.build_sdist('license-sdist')",
        ],
        cwd=packaged_project,
        env=isolated_environment(packaged_project),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with tarfile.open(next((packaged_project / "license-sdist").glob("*.tar.gz"))) as archive:
        matches = [name for name in archive.getnames() if name.endswith(f"/{LICENSE_PATH}")]
        assert len(matches) == 1, "sdist 缺少上游完整 MIT 许可证"
        stream = archive.extractfile(matches[0])
        assert stream is not None
        assert_upstream_license(stream.read())


def test_builder_creates_exact_dictionary_without_importing_jieba(tmp_path: Path) -> None:
    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-c",
            "import runpy, sys; "
            "sys.modules['jieba'] = None; "
            f"sys.argv = ['build_dictionary.py', '--output-dir', {str(tmp_path)!r}]; "
            f"runpy.run_path({str(ROOT / 'scripts/build_dictionary.py')!r}, run_name='__main__')",
        ],
        env=isolated_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    artifact = tmp_path / Path(ARTIFACT).name
    raw = artifact.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_SHA256
    assert len(raw) == 6914088
    document = json.loads(raw)
    assert document["schema_version"] == 2
    assert document["total"] == 60101967
    assert len(document["words"]) == len(document["frequencies"]) == 498113
    assert len(set(document["words"])) == 498113
    assert not raw.endswith(b"\n")


def test_explicit_dictionary_source_is_validated_before_output(tmp_path: Path) -> None:
    builder = importlib.import_module("scripts.build_dictionary")
    source = tmp_path / "dict.txt"
    source.write_text("changed 1\n")
    destination = tmp_path / "output"
    with pytest.raises(ValueError, match="SHA-256"):
        builder.build_dictionary(destination, dictionary_path=source)
    assert not destination.exists()


def test_dictionary_generation_is_byte_reproducible(tmp_path: Path) -> None:
    builder = importlib.import_module("scripts.build_dictionary")
    source = Path(importlib.metadata.distribution("jieba").locate_file("jieba/dict.txt"))
    first = builder.build_dictionary(tmp_path / "first")
    second = builder.build_dictionary(tmp_path / "second", dictionary_path=source)
    assert first.read_bytes() == second.read_bytes()
    assert hashlib.sha256(first.read_bytes()).hexdigest() == EXPECTED_SHA256


def test_generated_hash_mismatch_never_publishes_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    builder = importlib.import_module("scripts.build_dictionary")
    monkeypatch.setattr(builder, "ARTIFACT_SHA256", "0" * 64)
    destination = tmp_path / "output"
    with pytest.raises(ValueError, match="Generated dictionary SHA-256"):
        builder.build_dictionary(destination)
    assert not destination.exists()


def test_clean_project_wheel_contains_pinned_dictionary(packaged_project: Path) -> None:
    wheel = next((packaged_project / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert ARTIFACT in archive.namelist()
        assert hashlib.sha256(archive.read(ARTIFACT)).hexdigest() == EXPECTED_SHA256
    assert not (packaged_project / "src" / ARTIFACT).exists()


def test_wheel_verifier_uses_packaged_dictionary_without_runtime_builder(
    packaged_project: Path,
) -> None:
    result = subprocess.run(  # nosec B603
        [sys.executable, str(packaged_project / "scripts" / "verify_wheel.py")],
        cwd=packaged_project,
        env=isolated_environment(packaged_project),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "预构建字典" in result.stdout


def test_wheel_verifier_ignores_generated_source_dictionary_caches(
    packaged_project: Path, tmp_path: Path
) -> None:
    """旧版或损坏的源码缓存都不能替代 wheel 内的固定摘要验证。"""
    isolated = tmp_path / "project"
    for name in ("src", "scripts", "dist"):
        shutil.copytree(packaged_project / name, isolated / name)
    (isolated / ".uv-cache").mkdir()
    source_data = isolated / "src/semantic_core/data"
    source_data.mkdir()
    (source_data / "jieba-prefix-v1.json").write_bytes(b"stale previous version")
    (source_data / "jieba-prefix-v2.json").write_bytes(b"corrupt current cache")
    result = subprocess.run(  # nosec B603
        [sys.executable, str(isolated / "scripts/verify_wheel.py")],
        cwd=isolated,
        env=isolated_environment(isolated),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "预构建字典" in result.stdout


def test_wheel_rebuild_removes_only_generated_dictionary_caches(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    copy_build_project(project)
    source_data = project / "src/semantic_core/data"
    source_data.mkdir()
    cached_source = {
        "jieba-prefix-v1.json": b"stale source cache",
        "jieba-prefix-v2.json": b"corrupt source cache",
    }
    for name, data in cached_source.items():
        (source_data / name).write_bytes(data)
    build_data = project / "build/lib/semantic_core/data"
    build_data.mkdir(parents=True)
    (build_data / "jieba-prefix-v1.json").write_bytes(b"stale previous version")
    (build_data / "jieba-prefix-v2.json").write_bytes(b"corrupt previous build")
    (build_data / "other-resource.json").write_bytes(b"keep unrelated resource")
    built = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-c",
            "import setuptools.build_meta as backend; backend.build_wheel('dist')",
        ],
        cwd=project,
        env=isolated_environment(project),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    verified = subprocess.run(  # nosec B603
        [sys.executable, str(project / "scripts/verify_wheel.py")],
        cwd=project,
        env=isolated_environment(project),
        capture_output=True,
        text=True,
        timeout=60,
    )
    with zipfile.ZipFile(next((project / "dist").glob("*.whl"))) as archive:
        dictionaries = [
            entry for entry in archive.namelist() if Path(entry).match("jieba-prefix-*.json")
        ]
        print(f"残留构建缓存后的 wheel 词典条目：{dictionaries}")
        print(f"当前校验器退出码：{verified.returncode}\n{verified.stdout}{verified.stderr}")
        assert dictionaries == [ARTIFACT], "wheel 携带未校验的旧构建词典"
        assert hashlib.sha256(archive.read(ARTIFACT)).hexdigest() == EXPECTED_SHA256
        assert archive.read("semantic_core/data/other-resource.json") == b"keep unrelated resource"
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert not (build_data / "jieba-prefix-v1.json").exists()
    assert (build_data / "other-resource.json").read_bytes() == b"keep unrelated resource"
    for name, data in cached_source.items():
        assert (source_data / name).read_bytes() == data, "构建不得删除或修改源码缓存"


@pytest.mark.parametrize(
    "extra_name",
    [
        "semantic_core/data/jieba-prefix-v1.json",
        "semantic_core/data/jieba-prefix-unexpected.json",
        "semantic_core/data/nested/jieba-prefix-v1.json",
        ARTIFACT,
        "semantic_core/_dictionary.py",
    ],
)
def test_wheel_verifier_rejects_extra_dictionaries_and_duplicate_entries(
    packaged_project: Path, tmp_path: Path, extra_name: str
) -> None:
    isolated = tmp_path / "project"
    for name in ("src", "scripts", "dist"):
        shutil.copytree(packaged_project / name, isolated / name)
    (isolated / ".uv-cache").mkdir()
    wheel = next((isolated / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel, "a") as archive:
        if extra_name in archive.namelist():
            data = archive.read(extra_name)
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr(extra_name, data)
        else:
            archive.writestr(extra_name, b"unverified dictionary content")
    result = subprocess.run(  # nosec B603
        [sys.executable, str(isolated / "scripts/verify_wheel.py")],
        cwd=isolated,
        env=isolated_environment(isolated),
        capture_output=True,
        text=True,
        timeout=60,
    )
    print(f"伪造条目：{extra_name}；校验器退出码：{result.returncode}")
    print(result.stdout + result.stderr)
    assert result.returncode != 0, "校验器放过额外词典或重复 ZIP 条目"
    assert "词典" in result.stdout + result.stderr or "重复" in result.stdout + result.stderr


@pytest.mark.parametrize("cached_manifest", [False, True])
def test_sdist_excludes_generated_caches_and_rebuilds_wheel_offline(
    setuptools_only_python: Path, tmp_path: Path, cached_manifest: bool
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    copy_build_project(project)
    source_data = project / "src/semantic_core/data"
    source_data.mkdir()
    for name in ("jieba-prefix-v1.json", "jieba-prefix-v2.json"):
        (source_data / name).write_bytes(b"untrusted generated source cache")
    if cached_manifest:
        metadata = project / "src/gateway_semantic_core.egg-info"
        metadata.mkdir()
        (metadata / "SOURCES.txt").write_text(
            "src/semantic_core/data/jieba-prefix-v1.json\n"
            "src/semantic_core/data/jieba-prefix-v2.json\n"
        )
    built = subprocess.run(  # nosec B603
        [
            str(setuptools_only_python),
            "-c",
            "import setuptools.build_meta as backend; backend.build_sdist('sdist')",
        ],
        cwd=project,
        env=isolated_environment(project),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(next((project / "sdist").glob("*.tar.gz"))) as archive:
        names = archive.getnames()
        dictionaries = [name for name in names if Path(name).match("jieba-prefix-*.json")]
        print(f"sdist 源码缓存条目（已有 manifest={cached_manifest}）：{dictionaries}")
        assert not dictionaries, "sdist 不得携带开发者生成词典"
        for required in (
            "scripts/build_dictionary.py",
            "build_inputs/jieba-0.42.1/dict.txt",
            "src/semantic_core/schemas/snapshot-v1.json",
        ):
            member = next(name for name in names if name.endswith(f"/{required}"))
            stream = archive.extractfile(member)
            assert stream is not None
            assert stream.read() == (project / required).read_bytes()
        archive.extractall(extracted, filter="data")
    restored = next(extracted.iterdir())
    assert not (restored / ".git").exists()
    rebuilt = subprocess.run(  # nosec B603
        [
            str(setuptools_only_python),
            "-c",
            "import setuptools.build_meta as backend; backend.build_wheel('dist')",
        ],
        cwd=restored,
        env=isolated_environment(restored),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert rebuilt.returncode == 0, rebuilt.stdout + rebuilt.stderr
    with zipfile.ZipFile(next((restored / "dist").glob("*.whl"))) as archive:
        dictionaries = [
            name for name in archive.namelist() if Path(name).match("jieba-prefix-*.json")
        ]
        assert dictionaries == [ARTIFACT]
        assert hashlib.sha256(archive.read(ARTIFACT)).hexdigest() == EXPECTED_SHA256


@pytest.mark.parametrize("replacement", [None, b"corrupt dictionary"])
def test_wheel_verifier_rejects_missing_or_corrupt_dictionary(
    packaged_project: Path, tmp_path: Path, replacement: bytes | None
) -> None:
    wheel = next((packaged_project / "dist").glob("*.whl"))
    isolated = tmp_path / "project"
    shutil.copytree(packaged_project / "src", isolated / "src")
    shutil.copytree(packaged_project / "scripts", isolated / "scripts")
    (isolated / "dist").mkdir()
    (isolated / ".uv-cache").mkdir()
    with (
        zipfile.ZipFile(wheel) as original,
        zipfile.ZipFile(isolated / "dist" / wheel.name, "w") as altered,
    ):
        for entry in original.infolist():
            if entry.filename == ARTIFACT:
                if replacement is not None:
                    altered.writestr(entry, replacement)
            else:
                altered.writestr(entry, original.read(entry.filename))
    result = subprocess.run(  # nosec B603
        [sys.executable, str(isolated / "scripts" / "verify_wheel.py")],
        cwd=isolated,
        env=isolated_environment(isolated),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "字典" in result.stdout + result.stderr


def test_source_distribution_can_build_the_dictionary_offline(
    packaged_project: Path, tmp_path: Path
) -> None:
    built = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-c",
            "import setuptools.build_meta as backend; backend.build_sdist('sdist')",
        ],
        cwd=packaged_project,
        env=isolated_environment(packaged_project),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    source = next((packaged_project / "sdist").glob("*.tar.gz"))
    with tarfile.open(source) as archive:
        assert any(name.endswith("/scripts/build_dictionary.py") for name in archive.getnames())
        archive.extractall(tmp_path, filter="data")
    project = next(path for path in tmp_path.iterdir() if path.is_dir())
    rebuilt = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-c",
            "import setuptools.build_meta as backend; backend.build_wheel('dist')",
        ],
        cwd=project,
        env=isolated_environment(project),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert rebuilt.returncode == 0, rebuilt.stdout + rebuilt.stderr
    with zipfile.ZipFile(next((project / "dist").glob("*.whl"))) as archive:
        assert hashlib.sha256(archive.read(ARTIFACT)).hexdigest() == EXPECTED_SHA256


def test_lambda_build_generates_dictionary_in_target_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts import build_lambda

    source = Path(importlib.metadata.distribution("jieba").locate_file("jieba/dict.txt"))
    package_root = tmp_path / "project"
    shutil.copytree(
        ROOT / "src" / "semantic_core",
        package_root / "src" / "semantic_core",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "data"),
    )
    service = package_root / "src" / "semantic_service"
    service.mkdir(parents=True)
    (service / "__init__.py").write_text("")
    shutil.copytree(ROOT / "scripts", package_root / "scripts")
    monkeypatch.setattr(build_lambda, "ROOT", package_root)
    monkeypatch.setattr(
        build_lambda,
        "load_lock",
        lambda _: {"architecture": "x86_64", "python": "3.12", "platform": "test", "wheels": []},
    )
    monkeypatch.setattr(build_lambda, "wheel_paths", lambda *_args, **_kwargs: [source])

    def unpack(_path: Path, destination: Path) -> None:
        (destination / "jieba").mkdir()
        shutil.copyfile(source, destination / "jieba" / "dict.txt")
        # A target-only module must never be imported by the host generator.
        (destination / "jieba" / "__init__.py").write_text(
            "raise AssertionError('target dependency imported on build host')\n"
        )

    monkeypatch.setattr(build_lambda, "unpack_wheel", unpack)
    lock = tmp_path / "lock.json"
    lock.write_text("{}")
    output = tmp_path / "lambda.zip"
    build_lambda.build(lock, tmp_path / "wheelhouse", output, offline=True)
    with zipfile.ZipFile(output) as archive:
        assert ARTIFACT in archive.namelist()
        assert hashlib.sha256(archive.read(ARTIFACT)).hexdigest() == EXPECTED_SHA256

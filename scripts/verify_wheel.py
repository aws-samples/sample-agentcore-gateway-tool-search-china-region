"""Verify packaged files and exercise the built wheel without installing it."""

from __future__ import annotations

import hashlib
import importlib

# Python requirement is >=3.12,<3.13; no pre-3.7 compatibility.
# nosemgrep: python37-compatibility-importlib2
import importlib.resources
import json
import os
import socket
import subprocess  # nosec B404
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _require_wheel_module(module: ModuleType, wheel: Path) -> None:
    if not str(module.__file__).startswith(str(wheel) + "/"):
        raise SystemExit(f"Module imported outside the wheel: {module.__name__}")


def import_packaged_module(name: str, wheel: Path) -> ModuleType:
    """Import real wheel code; isolate only the facade entry's configured factory.

    The dedicated Lambda entry deliberately initializes during import. Its real
    source still executes, with a sentinel replacing the factory that needs live
    configuration. This gate requires exactly one eager call and retains the
    global environment/network deny rules used by the rest of wheel inspection.
    """
    parts = name.split(".")
    if parts[0] not in {"semantic_core", "semantic_service", "semantic_eval"} or not all(
        part.isidentifier() for part in parts
    ):
        raise ValueError("Only project wheel modules may be imported")
    source = ROOT.joinpath("src", *parts)
    if not source.with_suffix(".py").is_file() and not (source / "__init__.py").is_file():
        raise ValueError("Module is not in the reviewed project source tree")
    if name == "semantic_service.facade_lambda":
        facade = importlib.import_module("semantic_service.facade")
        _require_wheel_module(facade, wheel)
        router = SimpleNamespace(initialization_timings={})
        with patch.object(facade, "_router", return_value=router) as factory:
            module = importlib.import_module(name)  # nosemgrep: non-literal-import
            try:
                factory.assert_called_once_with()
            except AssertionError:
                raise SystemExit(
                    "Facade entry must call its factory exactly once during module initialization."
                ) from None
        if getattr(module, "_ROUTER", None) is not router:
            raise SystemExit("Facade entry must retain the eagerly initialized router.")
        print("PASS: facade entry eagerly initializes exactly once with an isolated factory.")
    else:
        module = importlib.import_module(name)  # nosemgrep: non-literal-import
    _require_wheel_module(module, wheel)
    return module


def verify() -> None:
    """验证真实 wheel 资源，并禁止运行时字典构建后完成中文快照往返。"""
    wheels = sorted((ROOT / "dist").glob("gateway_semantic_core-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Expected exactly one built project wheel in dist/.")
    wheel = wheels[0]
    expected = sorted(
        file
        for package in ("semantic_core", "semantic_service", "semantic_eval")
        for file in (ROOT / "src" / package).rglob("*")
        if file.is_file()
        and (file.suffix in {".py", ".json"} or file.name == "py.typed")
        # 生成词典可能残留旧版或损坏缓存；下方按 wheel 内代码的固定摘要独立验证。
        and not (
            file.parent == ROOT / "src/semantic_core/data" and file.match("jieba-prefix-*.json")
        )
    )
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise SystemExit("项目 wheel 包含重复 ZIP 条目。")
        for source in expected:
            name = str(source.relative_to(ROOT / "src"))
            if archive.read(name) != source.read_bytes():
                raise SystemExit(f"Packaged file differs from current source: {name}")
        if any(
            name.startswith(("tests/", "evidence/", ".artifacts/", ".local/")) for name in names
        ):
            raise SystemExit("Development-only files unexpectedly packaged.")
    sys.path.insert(0, str(wheel))
    dictionary = importlib.import_module("semantic_core._dictionary")
    dictionary_name = f"semantic_core/data/{dictionary.ARTIFACT_NAME}"
    with zipfile.ZipFile(wheel) as archive:
        dictionaries = [
            name for name in archive.namelist() if PurePosixPath(name).match("jieba-prefix-*.json")
        ]
        if dictionaries != [dictionary_name]:
            raise SystemExit("项目 wheel 词典集合异常：必须恰好包含一个当前版本的预构建字典。")
        if hashlib.sha256(archive.read(dictionary_name)).hexdigest() != dictionary.ARTIFACT_SHA256:
            raise SystemExit("项目 wheel 的预构建字典摘要不匹配。")
    print(f"PASS: wheel contains {len(expected)} exact source/resource files.")
    print(f"Wheel: {wheel.relative_to(ROOT)}")
    print(f"SHA256: {hashlib.sha256(wheel.read_bytes()).hexdigest()}")
    for source in expected:
        if source.suffix != ".py":
            continue
        relative = source.relative_to(ROOT / "src").with_suffix("")
        parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
        module = import_packaged_module(".".join(parts), wheel)
        print(f"PASS: imported {module.__name__} from wheel.")
    schema = json.loads(
        importlib.resources.files("semantic_core")
        .joinpath("schemas", "snapshot-v1.json")
        .read_text()
    )
    if schema["properties"]["schema_version"]["const"] != 1:
        raise SystemExit("Unexpected packaged snapshot schema version.")

    catalog = importlib.import_module("semantic_core.catalog")
    snapshot_module = importlib.import_module("semantic_core.snapshot")
    gateway = catalog.GatewayIdentity("fixture-account", "fixture-region", "fixture-gateway")
    identity = catalog.ToolIdentity(gateway, "fixture-target", "v1", "lookup_tool")
    tool = catalog.ToolRecord.from_descriptor(
        identity,
        {"name": "lookup_tool", "description": "查询订单状态和客户信息", "inputSchema": {}},
    )
    jieba = importlib.import_module("jieba")
    tokenizer_module = importlib.import_module("semantic_core.tokenization")
    with (
        patch.object(jieba.Tokenizer, "initialize", side_effect=AssertionError("运行时字典重建")),
        patch.object(jieba.Tokenizer, "gen_pfdict", side_effect=AssertionError("运行时词典解析")),
    ):
        tokenizer = tokenizer_module.ChineseTokenizer()
        tokens = tokenizer.tokenize("查询订单状态")
        if not tokens or not any("订单" in token for token in tokens):
            raise SystemExit("项目 wheel 的预构建字典未完成中文分词。")
        snapshot = snapshot_module.IndexSnapshot.build(gateway, [tool])
        with tempfile.TemporaryDirectory(
            prefix="wheel-smoke-", dir=ROOT / ".uv-cache"
        ) as temporary:
            destination = Path(temporary) / "snapshot"
            snapshot_module.save_snapshot(snapshot, destination)
            loaded = snapshot_module.load_snapshot(destination, expected_gateway=gateway)
            if loaded.tools != snapshot.tools:
                raise SystemExit("Snapshot wheel smoke round-trip changed tools.")
    print("PASS: 预构建字典支持中文分词；未调用运行时 initialize/gen_pfdict。")
    print("PASS: packaged snapshot schema validated a save/load round-trip.")


def verification_environment() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "PYTHONNOUSERSITE": "1",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "BOTO_CONFIG": os.devnull,
    }


def verify_isolated() -> None:
    """No ambient Lambda activation, configuration, credentials or network."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise RuntimeError("wheel verification forbids network access")

    with (
        patch.dict(os.environ, verification_environment(), clear=True),
        patch.object(socket, "getaddrinfo", forbidden),
        patch.object(socket.socket, "connect", forbidden),
        patch.object(socket.socket, "connect_ex", forbidden),
    ):
        verify()


def main() -> None:
    if sys.argv[1:] == ["--isolated"]:
        verify_isolated()
        return
    if sys.argv[1:]:
        raise SystemExit("verify_wheel.py takes no public arguments")
    # Fixed interpreter and self path; public argv is rejected above.
    subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
        [sys.executable, "-I", str(Path(__file__).resolve()), "--isolated"],
        env=verification_environment(),
        check=True,
        shell=False,  # nosec B603
    )


if __name__ == "__main__":
    main()

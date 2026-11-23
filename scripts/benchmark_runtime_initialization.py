#!/usr/bin/env python3
"""历史 runtime/Boto3 导入与 factory 对照；不代表新的 REQUEST 部署入口或 Lambda Init。

当前 REQUEST 部署入口使用 benchmark_request_initialization.py 单独测量。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.machinery
import io
import json
import math
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess  # nosec B404
import sys
import time
from pathlib import Path
from types import CodeType
from typing import Any, NoReturn, cast
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

RUNTIME = "src/semantic_service/runtime.py"
ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / RUNTIME).is_file() and (parent / ".git").exists()
)
EXPECTED_UNUSED_MODULES = {"baseline": ["boto3", "s3transfer"], "current": []}


def _offline_environment() -> dict[str, str]:
    """不用调用者的 AWS 配置、真实凭证或自定义 Python 搜索路径。"""
    return {
        "PATH": os.defpath,
        "AWS_ACCESS_KEY_ID": "synthetic-access",
        "AWS_SECRET_ACCESS_KEY": "synthetic-secret",  # nosec B105
        "AWS_SESSION_TOKEN": "synthetic-token",  # nosec B105
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "BOTO_CONFIG": os.devnull,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }


def _configuration() -> bytes:
    gateway = {
        "account": "000000000000",
        "region": "cn-north-1",
        "gateway_id": "offline-benchmark",
    }
    document = {
        "version": 1,
        "gateway": gateway,
        "operation": "request",
        "settings": {
            "SEMSEARCH_MACHINE_PRINCIPALS_JSON": "[]",
            "SEMSEARCH_SEARCH_DESCRIPTOR_JSON": json.dumps(
                {
                    "name": "offline___search_tools",
                    "description": "离线初始化测量",
                    "inputSchema": {"type": "object"},
                }
            ),
            "OIDC_ISSUER": "https://identity.invalid",
            "OIDC_JWKS_URI": "https://identity.invalid/jwks",
            "OIDC_DISCOVERY_URI": "https://identity.invalid/discovery",
            "OIDC_AUDIENCE": "offline",
            "OIDC_ALLOWED_HOSTS": "identity.invalid",
        },
    }
    data = json.dumps(document, sort_keys=True).encode()
    digest = hashlib.sha256(data).hexdigest()
    os.environ.update(
        SEMSEARCH_ACCOUNT=gateway["account"],
        SEMSEARCH_REGION=gateway["region"],
        SEMSEARCH_GATEWAY_ID=gateway["gateway_id"],
        SEMSEARCH_SEARCH_TARGET_ID="offline-target",
        SEMSEARCH_SEARCH_TOOL_NAME="offline___search_tools",
        SEMSEARCH_SEARCH_INCARNATION="offline-v1",
        SEMSEARCH_BUCKET="offline-benchmark",
        SEMSEARCH_TABLE="offline-state",
        SEMSEARCH_PROTECTED_TABLE="offline-protected",
        SEMSEARCH_CONFIG_BUCKET="offline-benchmark",
        SEMSEARCH_CONFIG_KEY=(
            "000000000000/cn-north-1/offline-benchmark/configuration/request/" + digest + ".json"
        ),
        SEMSEARCH_CONFIG_VERSION_ID="fixture-v1",
        SEMSEARCH_CONFIG_SHA256=digest,
        SEMSEARCH_CONFIG_BYTES=str(len(data)),
    )
    return data


def _child(source: str, mode: str) -> dict[str, object]:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("benchmark forbids network access")

    patch.object(socket, "getaddrinfo", forbidden).start()
    patch.object(socket.socket, "connect", forbidden).start()
    patch.object(socket.socket, "connect_ex", forbidden).start()
    data = _configuration()
    original = importlib.machinery.SourceFileLoader.get_code
    source_compiles: list[str] = []

    def get_code(loader: importlib.machinery.SourceFileLoader, fullname: str) -> CodeType | None:
        if fullname == "semantic_service.runtime":
            return compile(source, str(ROOT / RUNTIME), "exec", dont_inherit=True)
        path = Path(loader.path)
        if mode == "source_compile" and (
            path.is_relative_to(ROOT / "src") or "site-packages" in path.parts
        ):
            source_compiles.append(fullname)
            return compile(loader.get_data(loader.path), loader.path, "exec", dont_inherit=True)
        return original(loader, fullname)

    # 两边 runtime 都从固定源码加载；source_compile 额外模拟部署包没有依赖 pyc。
    # 标准库保持解释器原有加载方式，这不是目标 Lambda 文件系统的仿真。
    patch.object(importlib.machinery.SourceFileLoader, "get_code", get_code).start()
    started = time.perf_counter()
    runtime = importlib.import_module("semantic_service.runtime")
    import_ms = (time.perf_counter() - started) * 1000
    endpoint = importlib.import_module("botocore.endpoint")
    response_module = importlib.import_module("botocore.awsrequest")
    loaders = importlib.import_module("botocore.loaders")
    timings_module = importlib.import_module("semantic_core._timing")
    # 只隔离 SDK 的用户模型目录，不重定义 HOME；此时尚未创建 AWS Session。
    patch.object(
        loaders.Loader, "CUSTOMER_DATA_PATH", str(Path(os.devnull) / ".aws/models")
    ).start()
    requests = 0

    class Raw:
        def __init__(self) -> None:
            self.body = io.BytesIO(data)

        def read(self, size: int = -1) -> bytes:
            return self.body.read(size)

        def stream(self, *args: object, **kwargs: object) -> list[bytes]:
            return [data]

        def close(self) -> None:
            self.body.close()

    def respond(self: object, request: Any) -> object:
        nonlocal requests
        assert request.method == "GET"
        assert urlsplit(request.url).hostname == "offline-benchmark.s3.cn-north-1.amazonaws.com.cn"
        assert parse_qs(urlsplit(request.url).query) == {"versionId": ["fixture-v1"]}
        assert "Authorization" in request.headers
        requests += 1
        return cast(
            object,
            response_module.AWSResponse(
                request.url,
                200,
                {"content-length": str(len(data)), "x-amz-version-id": "fixture-v1"},
                Raw(),
            ),
        )

    # 实际执行低层 SDK 的请求生成、签名与响应解析，仅用内存配置代替网络发送。
    # 不允许任何其它 AWS 请求，也不会调用认证验证或读取 secret。
    endpoint.Endpoint._send = respond
    with timings_module.collect() as timings:
        started = time.perf_counter()
        with timings_module.stage("handler.factory"):
            handler = runtime._request()
        factory_ms = (time.perf_counter() - started) * 1000
        assert runtime._request() is handler
    assert requests == 1
    return {
        "mode": mode,
        "import_ms": import_ms,
        "factory_ms": factory_ms,
        "total_ms": import_ms + factory_ms,
        "stages": timings.snapshot(),
        "source_compile_count": len(source_compiles),
        "unused_modules": [name for name in ("boto3", "s3transfer") if name in sys.modules],
        "configuration_requests": requests,
    }


def _summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50": statistics.median(ordered),
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "min": min(ordered),
        "max": max(ordered),
    }


def _validate_measurement(row: dict[str, object], *, variant: str, mode: str, pair: int) -> None:
    """逐样本验证高层 SDK 导入的实验前提，不对速度设阈值。"""
    context = f"benchmark precondition failed ({mode}, {variant}, pair {pair})"
    if row.get("mode") != mode:
        raise RuntimeError(f"{context}: unexpected measurement mode")
    requests = row.get("configuration_requests")
    if type(requests) is not int or requests != 1:
        raise RuntimeError(f"{context}: configuration_requests must be exactly 1")
    expected = EXPECTED_UNUSED_MODULES[variant]
    if row.get("unused_modules") != expected:
        raise RuntimeError(f"{context}: unused_modules must equal {expected!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline-ref",
        help="必填：导入 boto3 与 s3transfer、且兼容当前 _request 配置夹具的旧版 Git 提交",
    )
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--output", type=Path, default=Path(".artifacts/runtime-benchmark.json"))
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        payload = json.load(sys.stdin)
        print(json.dumps(_child(payload["source"], payload["mode"])))
        return
    if args.baseline_ref is None:
        parser.error("--baseline-ref is required; choose an explicit pre-change commit")
    if args.repeats < 20:
        parser.error("--repeats must be at least 20")
    # Accept only a literal commit object ID, never revision options/expressions.
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", args.baseline_ref) is None:
        parser.error("--baseline-ref must be a hexadecimal commit ID")
    git = shutil.which("git")
    if git is None or not Path(git).is_file():
        parser.error("git executable is required")
    git = str(Path(git).absolute())
    # Validated hex revision and absolute local git; separate argv, no shell.
    # nosemgrep: dangerous-subprocess-use-audit, dangerous-subprocess-use-tainted-env-args
    baseline_ref = subprocess.run(
        # nosemgrep: dangerous-subprocess-use-tainted-env-args
        [git, "rev-parse", "--verify", "--end-of-options", f"{args.baseline_ref}^{{commit}}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        shell=False,  # nosec B603
    ).stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", baseline_ref) is None:
        raise ValueError("git did not return a full commit ID")
    # Fixed repository path; git result was validated as a full commit ID.
    # nosemgrep: dangerous-subprocess-use-audit, dangerous-subprocess-use-tainted-env-args
    baseline = subprocess.run(
        # nosemgrep: dangerous-subprocess-use-tainted-env-args
        [git, "show", f"{baseline_ref}:{RUNTIME}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        shell=False,  # nosec B603
    ).stdout
    current = (ROOT / RUNTIME).read_text()
    if baseline == current:
        raise RuntimeError("benchmark precondition failed: identical runtime source")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for mode in ("source_compile", "normal_import"):
        for pair in range(args.repeats):
            # 交替先后顺序，减小单向热文件缓存或温度漂移对差值的偏置。
            variants = ("baseline", "current") if pair % 2 == 0 else ("current", "baseline")
            for variant in variants:
                source = baseline if variant == "baseline" else current
                # Fixed child; reviewed Git source is data on stdin in the offline sandbox.
                result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
                    [sys.executable, str(Path(__file__).resolve()), "--child"],
                    input=json.dumps({"source": source, "mode": mode}),
                    cwd=ROOT,
                    env=_offline_environment(),
                    capture_output=True,
                    text=True,
                    check=True,
                    shell=False,  # nosec B603
                )
                row = cast(dict[str, object], json.loads(result.stdout))
                _validate_measurement(row, variant=variant, mode=mode, pair=pair)
                rows.append({"pair": pair, "variant": variant, **row})
            print(f"{mode} 已完成第 {pair + 1}/{args.repeats} 对进程", flush=True)
    summaries: dict[str, dict[str, object]] = {}
    for mode in ("source_compile", "normal_import"):
        summaries[mode] = {}
        for variant in ("baseline", "current"):
            selected = [row for row in rows if row["mode"] == mode and row["variant"] == variant]
            summaries[mode][variant] = {
                metric: _summary([cast(float, row[metric]) for row in selected])
                for metric in ("import_ms", "factory_ms", "total_ms")
            }
        paired = [
            cast(
                float,
                next(
                    r["total_ms"]
                    for r in rows
                    if r["mode"] == mode and r["pair"] == pair and r["variant"] == "baseline"
                ),
            )
            - cast(
                float,
                next(
                    r["total_ms"]
                    for r in rows
                    if r["mode"] == mode and r["pair"] == pair and r["variant"] == "current"
                ),
            )
            for pair in range(args.repeats)
        ]
        summaries[mode]["paired_total_saving_ms"] = _summary(paired)
    if (ROOT / RUNTIME).read_text() != current:
        raise RuntimeError("runtime source changed while benchmark was running")
    report = {
        "scope": "runtime 导入与真实 _request 工厂；SDK 对内存配置执行签名 GET 与响应解析",
        "limits": [
            "仅本地主机；source_compile 模拟依赖无 pyc，并非 Lambda 现场",
            "两边使用当前工作树相同依赖与其他源码模块，只替换 runtime 模块源码",
            "不包含网络等待、OIDC/JWKS 调用、secret 读取与 handler 执行",
            "normal_import 可能读取已有依赖 pyc，不可外推为 Lambda 收益",
            "只支持兼容当前依赖、_request 工厂及配置夹具的旧 runtime；不支持任意历史版本",
        ],
        "preconditions": {
            "validated": True,
            "distinct_runtime_sources": True,
            "configuration_requests_per_sample": 1,
            "unused_modules": EXPECTED_UNUSED_MODULES,
            "baseline_contract": (
                "旧源码须导入 boto3 与 s3transfer；当前源码须均不导入；"
                "两侧首次 _request 加载一次配置、再次调用复用相同 handler"
            ),
            "speed_threshold": None,
        },
        "percentile_method": "p50 为中位数；p95 为经验最近秩；配对差值按同组同序号计算",
        "python": sys.version,
        "platform": platform.platform(),
        "baseline_ref": baseline_ref,
        "baseline_sha256": hashlib.sha256(baseline.encode()).hexdigest(),
        "current_sha256": hashlib.sha256(current.encode()).hexdigest(),
        "repeats_per_mode": args.repeats,
        "summaries": summaries,
        "rows": rows,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

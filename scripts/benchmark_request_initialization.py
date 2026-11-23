#!/usr/bin/env python3
"""Offline first import of the deployed REQUEST entry; local timings are not Lambda Init."""

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
import socket
import statistics
import subprocess  # nosec B404
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import CodeType, ModuleType
from typing import IO, Any, NoReturn, cast
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = "semantic_service.request_runtime"
ENTRY_SOURCE = "src/semantic_service/request_runtime.py"
RUNTIME_SOURCE = "src/semantic_service/runtime.py"
VARIANTS = ("on-demand", "provisioned-concurrency")
MODES = ("normal_import", "source_compile")
METRICS = (
    "entry_elapsed_ms",
    "entry_module_import_ms",
    "runtime_module_init_ms",
    "init_factory_ms",
    "init_oidc_ms",
    "handler_factory_ms",
    "combined_ms",
)


def _recorded_counts(stream: IO[bytes], names: tuple[str, ...]) -> dict[str, int]:
    """Snapshot actual attempts without sharing the live observation list."""
    descriptor = stream.fileno()
    recorded = os.pread(descriptor, os.fstat(descriptor).st_size, 0).decode("ascii")
    counts = dict.fromkeys(names, 0)
    for name in recorded.splitlines():
        if name not in counts:
            raise RuntimeError("unrecognized offline benchmark attempt")
        counts[name] += 1
    return counts


def _offline_environment() -> dict[str, str]:
    """Do not inherit AWS profiles, real credentials, proxy settings or Python paths."""
    return {
        "PATH": os.defpath,
        "AWS_ACCESS_KEY_ID": "synthetic-access",
        "AWS_SECRET_ACCESS_KEY": "synthetic-secret",  # nosec B105
        "AWS_SESSION_TOKEN": "synthetic-token",  # nosec B105
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "BOTO_CONFIG": os.devnull,
        "AWS_REGION": "cn-north-1",
        "AWS_LAMBDA_FUNCTION_NAME": "offline-request-benchmark",
        "_HANDLER": ENTRYPOINT + ".request_handler",
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
                    "description": "Offline initialization measurement",
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


def _metadata_documents() -> dict[str, object]:
    return {
        "discovery": {
            "issuer": "https://identity.invalid",
            "jwks_uri": "https://identity.invalid/jwks",
        },
        "jwks": {
            "keys": [
                {
                    "kid": "offline-benchmark-key",
                    "kty": "RSA",
                    "alg": "RS256",
                    "use": "sig",
                    "e": "AQAB",
                    "n": (
                        "qPH2UKbghjTpaaXepILmmL1HmPKgYCNDdskaNSzkTba7fJ0SMotSxmAfxI-ctFFq"
                        "Hacaips0ad8RWGP4o4OPDqU5eBvVOjQQI5VSyLVbYaEB36at-3YAkV4Swkju_Nhg"
                        "jofsbvkj7eK5OaW6kRn1tNofqNroDCYeSvqYvqCz63i6LEw0zle5OIG2x0_i6-O1"
                        "dMPngXlugNkjCKsjYyIDZodik8-bCeg2-plZMq5XHLFO33qSjVeH7CSCLUymDCfzYfY"
                        "tj1PzQ2SMiKvj2-xi7_-1JN0sEmoDjQTKl4_U6gEhlrpxgWg9tQGYQxmUhzhZyyC"
                        "woTWM4LXbrHqTXM6HSQ"
                    ),
                }
            ]
        },
    }


def _child(variant: str, mode: str) -> dict[str, object]:
    if variant not in VARIANTS or mode not in MODES:
        raise ValueError("unsupported benchmark variant or mode")
    # No application/SDK preload is allowed to disappear from the measured import.
    preloaded = [
        name
        for name in (ENTRYPOINT, "semantic_service.runtime", "botocore", "httpx")
        if name in sys.modules
    ]
    if preloaded:
        raise RuntimeError("benchmark requires a fresh interpreter: " + ", ".join(preloaded))
    network_names = ("getaddrinfo", "connect", "connect_ex")
    metadata_names = ("discovery", "jwks")

    def forbidden(name: str) -> Callable[..., NoReturn]:
        def fail(*args: object, **kwargs: object) -> NoReturn:
            # Record before raising, so a caught exception is still observable.
            os.write(network_events.fileno(), (name + "\n").encode("ascii"))
            raise AssertionError("benchmark forbids network access: " + name)

        return fail

    with ExitStack() as stack:
        network_events = stack.enter_context(tempfile.NamedTemporaryFile(mode="a+b"))
        metadata_events = stack.enter_context(tempfile.NamedTemporaryFile(mode="a+b"))
        stack.enter_context(patch.dict(os.environ, _offline_environment(), clear=True))
        os.environ["AWS_LAMBDA_INITIALIZATION_TYPE"] = variant
        stack.enter_context(patch.object(socket, "getaddrinfo", forbidden("getaddrinfo")))
        stack.enter_context(patch.object(socket.socket, "connect", forbidden("connect")))
        stack.enter_context(patch.object(socket.socket, "connect_ex", forbidden("connect_ex")))
        data = _configuration()
        configuration_requests = 0
        metadata_process_ids: list[int] = []
        metadata_transport_ids: list[int] = []
        source_compiled_modules: list[str] = []
        original_get_code = importlib.machinery.SourceFileLoader.get_code
        original_exec_module = importlib.machinery.SourceFileLoader.exec_module

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
            nonlocal configuration_requests
            parsed = urlsplit(request.url)
            assert request.method == "GET"
            assert parsed.hostname == "offline-benchmark.s3.cn-north-1.amazonaws.com.cn"
            assert parsed.path == "/" + os.environ["SEMSEARCH_CONFIG_KEY"]
            assert parse_qs(parsed.query) == {"versionId": ["fixture-v1"]}
            assert b"Credential=synthetic-access/" in request.headers["Authorization"]
            assert request.headers["X-Amz-Security-Token"] == b"synthetic-token"
            configuration_requests += 1
            response_module = importlib.import_module("botocore.awsrequest")
            return cast(
                object,
                response_module.AWSResponse(
                    request.url,
                    200,
                    {"content-length": str(len(data)), "x-amz-version-id": "fixture-v1"},
                    Raw(),
                ),
            )

        def respond_metadata(self: object, request: Any) -> object:
            assert request.method == "GET"
            assert request.url.scheme == "https"
            assert request.url.host == "identity.invalid"
            assert request.url.query == b""
            endpoint = request.url.path.removeprefix("/")
            assert endpoint in metadata_names
            os.write(metadata_events.fileno(), (endpoint + "\n").encode("ascii"))
            metadata_process_ids.append(os.getpid())
            metadata_transport_ids.append(id(self))
            document = _metadata_documents()[endpoint]
            response_module = sys.modules["httpx"]
            return cast(object, response_module.Response(200, json=document))

        def get_code(
            loader: importlib.machinery.SourceFileLoader, fullname: str
        ) -> CodeType | None:
            path = Path(loader.path)
            if mode == "source_compile" and (
                path.is_relative_to(ROOT / "src") or "site-packages" in path.parts
            ):
                source_compiled_modules.append(fullname)
                return compile(loader.get_data(loader.path), loader.path, "exec", dont_inherit=True)
            return original_get_code(loader, fullname)

        def exec_module(loader: importlib.machinery.SourceFileLoader, module: ModuleType) -> None:
            original_exec_module(loader, module)
            # Patch after the SDK module loads, before its first consumer runs.
            # This preserves normal pyc behavior and never preimports the SDK.
            if module.__name__ == "botocore.endpoint":
                stack.enter_context(patch.object(module.Endpoint, "_send", respond))
            elif module.__name__ == "botocore.loaders":
                stack.enter_context(
                    patch.object(
                        module.Loader, "CUSTOMER_DATA_PATH", str(Path(os.devnull) / ".aws/models")
                    )
                )
            elif module.__name__ == "httpx":
                # Keep real client/transport construction and module import costs;
                # replace only transport I/O after httpx has fully loaded.
                stack.enter_context(
                    patch.object(module.HTTPTransport, "handle_request", respond_metadata)
                )
            elif module.__name__ == "semantic_service._request_prefetch":
                original_prefetch = module.prefetch

                def prefetch(verifier: object, **kwargs: Any) -> object:
                    return original_prefetch(verifier, **{**kwargs, "log_fd": output.fileno()})

                stack.enter_context(patch.object(module, "prefetch", prefetch))

        stack.enter_context(
            patch.object(importlib.machinery.SourceFileLoader, "get_code", get_code)
        )
        stack.enter_context(
            patch.object(importlib.machinery.SourceFileLoader, "exec_module", exec_module)
        )
        if str(ROOT / "src") not in sys.path:
            sys.path.insert(0, str(ROOT / "src"))
        # Raw attempt writes and Python summaries share an append-only file.
        output = stack.enter_context(tempfile.TemporaryFile(mode="a+", encoding="utf-8"))
        started = time.perf_counter()
        with redirect_stdout(output):
            entry = importlib.import_module(ENTRYPOINT)
        entry_elapsed_ms = (time.perf_counter() - started) * 1000
        init_configuration_requests = configuration_requests
        init_metadata_requests = _recorded_counts(metadata_events.file, metadata_names)
        output.flush()
        output.seek(0)
        records = [json.loads(line) for line in output.read().splitlines()]
        init_records = [record for record in records if record.get("operation") == "request_init"]
        oidc_attempts = [record for record in records if record.get("operation") == "oidc_fetch"]
        assert len(records) == len(init_records) + len(oidc_attempts), "unexpected Init telemetry"
        if variant == "on-demand":
            assert records == [], "on-demand entry unexpectedly emitted Init telemetry"
            init_record = None
            init_factory_ms = 0.0
            init_oidc_ms = 0.0
        else:
            assert len(init_records) == 1, "provisioned entry must emit exactly one Init record"
            init_record = init_records[0]
            init_factory_ms = init_record["stage_timings"]["handler.factory"]["duration_ms"]
            init_oidc_ms = init_record["stage_timings"]["auth.oidc"]["duration_ms"]
        runtime = sys.modules["semantic_service.runtime"]
        timings_module = sys.modules["semantic_core._timing"]
        assert entry.__file__ is not None
        assert runtime.__file__ is not None
        assert Path(entry.__file__).resolve() == ROOT / ENTRY_SOURCE
        assert Path(runtime.__file__).resolve() == ROOT / RUNTIME_SOURCE
        with timings_module.collect() as timings:
            started = time.perf_counter()
            with timings_module.stage("handler.factory"):
                handler = runtime._request()
            handler_factory_ms = (time.perf_counter() - started) * 1000
        assert runtime._request() is handler
        row: dict[str, object] = {
            "entrypoint": entry.__name__,
            "runtime_preimported": False,
            "variant": variant,
            "mode": mode,
            "entry_elapsed_ms": entry_elapsed_ms,
            "entry_module_import_ms": entry._ENTRY_IMPORT_MS,
            "runtime_module_init_ms": runtime._RUNTIME_MODULE_INIT_MS,
            "init_factory_ms": init_factory_ms,
            "init_oidc_ms": init_oidc_ms,
            "handler_factory_ms": handler_factory_ms,
            "combined_ms": entry_elapsed_ms + handler_factory_ms,
            "configuration_requests": configuration_requests,
            "init_configuration_requests": init_configuration_requests,
            "metadata_requests": _recorded_counts(metadata_events.file, metadata_names),
            "init_metadata_requests": init_metadata_requests,
            "oidc_attempts": oidc_attempts,
            "process_id": os.getpid(),
            "metadata_process_ids": sorted(set(metadata_process_ids)),
            "metadata_transport_ids": sorted(set(metadata_transport_ids)),
            "handler_transport_id": id(handler.verifier.client._transport),
            "network_attempts": _recorded_counts(network_events.file, network_names),
            "init_record": init_record,
            "handler_stages": timings.snapshot(),
            "first_invocation_unconsumed": runtime._FIRST_INVOCATION,
            "source_compiled_modules": sorted(source_compiled_modules),
            "loaded_roots": sorted({name.split(".")[0] for name in sys.modules}),
        }
        _validate_measurement(row, variant=variant, mode=mode, pair=0)
        return row


def _validate_measurement(row: dict[str, Any], *, variant: str, mode: str, pair: int) -> None:
    context = f"benchmark precondition failed ({mode}, {variant}, pair {pair})"
    if row.get("entrypoint") != ENTRYPOINT or row.get("runtime_preimported") is not False:
        raise RuntimeError(f"{context}: expected first import of the deployed REQUEST entrypoint")
    if row.get("variant") != variant or row.get("mode") != mode:
        raise RuntimeError(f"{context}: unexpected variant or mode")
    for field, expected in (
        ("configuration_requests", 1),
        ("init_configuration_requests", int(variant == "provisioned-concurrency")),
    ):
        if type(row.get(field)) is not int or row[field] != expected:
            raise RuntimeError(f"{context}: {field} must be exactly {expected}")
    metadata_count = int(variant == "provisioned-concurrency")
    for field in ("metadata_requests", "init_metadata_requests"):
        counts = row.get(field)
        if (
            not isinstance(counts, dict)
            or set(counts) != {"discovery", "jwks"}
            or any(type(value) is not int or value != metadata_count for value in counts.values())
        ):
            raise RuntimeError(f"{context}: {field} must count {metadata_count} per endpoint")
    oidc_attempts = row.get("oidc_attempts")
    if not isinstance(oidc_attempts, list) or len(oidc_attempts) != metadata_count:
        raise RuntimeError(f"{context}: oidc_attempts must contain {metadata_count} Init attempts")
    if metadata_count:
        attempt = oidc_attempts[0]
        if (
            not isinstance(attempt, dict)
            or attempt.get("operation") != "oidc_fetch"
            or attempt.get("event") != "attempt_finished"
            or attempt.get("phase") != "init"
            or "aws_request_id" not in attempt
            or attempt["aws_request_id"] is not None
            or attempt.get("endpoint") != "jwks"
            or type(attempt.get("attempt")) is not int
            or attempt["attempt"] != 1
            or attempt.get("compensation") is not False
            or attempt.get("outcome") != "ok"
            or "failure" not in attempt
            or attempt["failure"] is not None
            or type(attempt.get("duration_ms")) not in (int, float)
            or not math.isfinite(attempt["duration_ms"])
            or attempt["duration_ms"] < 0
        ):
            raise RuntimeError(f"{context}: oidc_attempts must show successful offline Init fetch")
    if row.get("first_invocation_unconsumed") is not True:
        raise RuntimeError(f"{context}: benchmark consumed an invocation")
    attempts = row.get("network_attempts")
    if (
        not isinstance(attempts, dict)
        or set(attempts) != {"getaddrinfo", "connect", "connect_ex"}
        or any(type(value) is not int or value != 0 for value in attempts.values())
    ):
        raise RuntimeError(f"{context}: network_attempts must contain only zero counts")
    process_id, handler_transport = row.get("process_id"), row.get("handler_transport_id")
    transports = row.get("metadata_transport_ids")
    if (
        type(process_id) is not int
        or process_id <= 0
        or row.get("metadata_process_ids") != ([process_id] if metadata_count else [])
    ):
        raise RuntimeError(f"{context}: metadata must stay in the measured process")
    if (
        type(handler_transport) is not int
        or handler_transport <= 0
        or not isinstance(transports, list)
        or len(transports) != metadata_count
        or any(type(value) is not int or value <= 0 for value in transports)
        or handler_transport in transports
    ):
        raise RuntimeError(f"{context}: prefetch must use a separate HTTP transport")
    compiled = row.get("source_compiled_modules")
    if not isinstance(compiled, list) or (
        mode == "normal_import"
        and compiled != []
        or mode == "source_compile"
        and not {ENTRYPOINT, "semantic_service.runtime"}.issubset(compiled)
    ):
        raise RuntimeError(f"{context}: source compilation mode was not exercised")
    for field in METRICS:
        value = row.get(field)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise RuntimeError(f"{context}: invalid {field}")
    record = row.get("init_record")
    if variant == "on-demand":
        if record is not None or row["init_factory_ms"] != 0 or row["init_oidc_ms"] != 0:
            raise RuntimeError(f"{context}: on-demand Init must be silent")
    elif (
        not isinstance(record, dict)
        or record.get("operation") != "request_init"
        or record.get("status") != "ready"
        or record.get("stage_timings", {}).get("handler.factory", {}).get("count") != 1
        or record["stage_timings"]["handler.factory"].get("errors") != 0
        or record["stage_timings"]["handler.factory"].get("duration_ms") != row["init_factory_ms"]
    ):
        raise RuntimeError(f"{context}: missing successful provisioned Init factory stage")
    elif (
        not isinstance(record.get("oidc_prefetch"), dict)
        or record["oidc_prefetch"].get("status") != "ready"
    ):
        raise RuntimeError(f"{context}: missing successful provisioned OIDC prefetch")
    elif (
        record.get("stage_timings", {}).get("auth.oidc", {}).get("count") != 1
        or record["stage_timings"]["auth.oidc"].get("errors") != 0
        or record["stage_timings"]["auth.oidc"].get("duration_ms") != row["init_oidc_ms"]
    ):
        raise RuntimeError(f"{context}: missing successful provisioned OIDC prefetch stage")
    if not math.isclose(
        row["combined_ms"], row["entry_elapsed_ms"] + row["handler_factory_ms"], abs_tol=0.001
    ):
        raise RuntimeError(f"{context}: combined_ms must include entry and handler factory")


def _summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50": statistics.median(ordered),
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "mean": statistics.mean(ordered),
        "min": min(ordered),
        "max": max(ordered),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument(
        "--output", type=Path, default=Path(".artifacts/request-init-benchmark.json")
    )
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        payload = json.load(sys.stdin)
        print(json.dumps(_child(payload["variant"], payload["mode"])))
        return
    if args.repeats < 20:
        parser.error("--repeats must be at least 20")
    rows: list[dict[str, Any]] = []
    for mode in MODES:
        for pair in range(args.repeats):
            variants = VARIANTS if pair % 2 == 0 else tuple(reversed(VARIANTS))
            for variant in variants:
                # Fixed child program; variant/mode are internal enumerations via stdin.
                result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
                    [sys.executable, str(Path(__file__).resolve()), "--child"],
                    input=json.dumps({"variant": variant, "mode": mode}),
                    cwd=ROOT,
                    env=_offline_environment(),
                    capture_output=True,
                    text=True,
                    check=True,
                    shell=False,  # nosec B603
                    timeout=30,
                )
                row = json.loads(result.stdout)
                _validate_measurement(row, variant=variant, mode=mode, pair=pair)
                rows.append({**row, "pair": pair})
    summaries = {
        mode: {
            variant: {
                field: _summary(
                    [
                        row[field]
                        for row in rows
                        if row["mode"] == mode and row["variant"] == variant
                    ]
                )
                for field in METRICS
            }
            for variant in VARIANTS
        }
        for mode in MODES
    }
    report = {
        "method": (
            "First import of the deployed REQUEST entry in fresh interpreters. "
            "Each compilation mode has alternating on-demand/provisioned pairs. "
            "Source compilation bypasses application and dependency pyc only; "
            "standard-library and native-extension behavior is unchanged. "
            "Signed, version-bound S3 configuration uses an in-memory response. "
            "Provisioned Init fetches discovery and JWKS through an offline metadata "
            "transport installed after httpx import in the current process; "
            "on-demand factories fetch neither. Attempts are independently recorded "
            "through private append-only files, including forbidden network access. "
            "Prefetch uses a separate HTTP client in the same process; the report "
            "records process and transport identities to verify that boundary. "
            "These are offline metadata processing costs, not platform network latency. "
            "No network, real credentials or handler work is performed. "
            "Entry elapsed includes provisioned preparation; combined adds the first "
            "handler factory lookup. The two arms perform different work, so combined_ms "
            "is not a direct latency benefit comparison; init_oidc_ms separately reports "
            "the provisioned auth.oidc stage and is zero for on-demand. "
            "PC relocation is not on-demand time savings. "
            "Local source/dependency timings do not measure package preparation or Lambda Init."
        ),
        "python": sys.version,
        "platform": platform.platform(),
        "entrypoint": ENTRYPOINT,
        "entrypoint_sha256": hashlib.sha256((ROOT / ENTRY_SOURCE).read_bytes()).hexdigest(),
        "runtime_sha256": hashlib.sha256((ROOT / RUNTIME_SOURCE).read_bytes()).hexdigest(),
        "pairs_per_mode": args.repeats,
        "preconditions": {
            "validated": True,
            "configuration_requests_per_sample": 1,
            "metadata_requests_per_endpoint": {"on-demand": 0, "provisioned-concurrency": 1},
            "network_attempts_per_sample": 0,
            "runtime_preimported": False,
        },
        "summaries": summaries,
        "rows": rows,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()

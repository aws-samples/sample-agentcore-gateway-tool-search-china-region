"""Measure the deployed REQUEST entry, not a preimported historical runtime."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess  # nosec B404
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/benchmark_request_initialization.py"
ENTRYPOINT = "semantic_service.request_runtime"


def child(
    variant: str, mode: str, extra_environment: dict[str, str] | None = None
) -> dict[str, Any]:
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_ACCESS_KEY_ID": "synthetic-access",
        "AWS_SECRET_ACCESS_KEY": "synthetic-secret",
        "AWS_SESSION_TOKEN": "synthetic-token",
        "BOTO_CONFIG": os.devnull,
        **(extra_environment or {}),
    }
    # Fixed local test program and fixture paths; argv is not request-controlled.
    # nosemgrep: dangerous-subprocess-use-audit
    result = subprocess.run(  # nosec B603
        [sys.executable, str(SCRIPT), "--child"],
        input=json.dumps({"variant": variant, "mode": mode}),
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("variant", ["on-demand", "provisioned-concurrency"])
@pytest.mark.parametrize("mode", ["normal_import", "source_compile"])
def test_real_child_measures_the_first_deployed_entry_import(variant: str, mode: str) -> None:
    row = child(variant, mode)
    metadata_count = int(variant == "provisioned-concurrency")
    assert row["entrypoint"] == ENTRYPOINT
    assert row["runtime_preimported"] is False
    assert row["configuration_requests"] == 1
    assert row["network_attempts"] == {"getaddrinfo": 0, "connect": 0, "connect_ex": 0}
    assert row["variant"] == variant
    assert row["mode"] == mode
    assert row["first_invocation_unconsumed"] is True
    assert row["entry_elapsed_ms"] >= row["entry_module_import_ms"] >= 0
    assert row["metadata_requests"] == {"discovery": metadata_count, "jwks": metadata_count}
    assert row["init_metadata_requests"] == row["metadata_requests"]
    if mode == "source_compile":
        assert ENTRYPOINT in row["source_compiled_modules"]
        assert "semantic_service.runtime" in row["source_compiled_modules"]
    else:
        assert row["source_compiled_modules"] == []
    if variant == "on-demand":
        assert row["init_record"] is None
        assert row["init_configuration_requests"] == 0
        assert row["init_factory_ms"] == 0
        assert row["init_oidc_ms"] == 0
        assert row["handler_factory_ms"] > 0
        assert row["oidc_attempts"] == []
    else:
        assert row["init_configuration_requests"] == 1
        assert row["init_factory_ms"] > 0
        assert row["init_oidc_ms"] > 0
        assert (
            row["init_oidc_ms"] == row["init_record"]["stage_timings"]["auth.oidc"]["duration_ms"]
        )
        assert row["init_record"]["stage_timings"]["handler.factory"]["errors"] == 0
        assert row["init_record"]["oidc_prefetch"] == {"status": "ready"}
        assert len(row["oidc_attempts"]) == 1
        attempt = row["oidc_attempts"][0]
        assert attempt["operation"] == "oidc_fetch"
        assert attempt["phase"] == "init"
        assert attempt["aws_request_id"] is None
        assert attempt["endpoint"] == "jwks"
        assert attempt["outcome"] == "ok"
        assert attempt["compensation"] is False
        assert attempt["attempt"] == 1


@pytest.fixture
def benchmark() -> ModuleType:
    spec = importlib.util.spec_from_file_location("request_benchmark_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid_row(variant: str, mode: str) -> dict[str, Any]:
    provisioned = variant == "provisioned-concurrency"
    return {
        "entrypoint": ENTRYPOINT,
        "runtime_preimported": False,
        "variant": variant,
        "mode": mode,
        "process_id": 100,
        "handler_transport_id": 200,
        "metadata_process_ids": [100] if provisioned else [],
        "metadata_transport_ids": [300] if provisioned else [],
        "entry_elapsed_ms": 100.0,
        "entry_module_import_ms": 50.0,
        "runtime_module_init_ms": 49.0,
        "init_factory_ms": 50.0 if provisioned else 0.0,
        "init_oidc_ms": 2.0 if provisioned else 0.0,
        "handler_factory_ms": 1.0 if provisioned else 50.0,
        "combined_ms": 101.0 if provisioned else 150.0,
        "configuration_requests": 1,
        "init_configuration_requests": int(provisioned),
        "metadata_requests": {"discovery": int(provisioned), "jwks": int(provisioned)},
        "init_metadata_requests": {"discovery": int(provisioned), "jwks": int(provisioned)},
        "first_invocation_unconsumed": True,
        "network_attempts": {"getaddrinfo": 0, "connect": 0, "connect_ex": 0},
        "source_compiled_modules": (
            [ENTRYPOINT, "semantic_service.runtime"] if mode == "source_compile" else []
        ),
        "init_record": (
            {
                "operation": "request_init",
                "status": "ready",
                "oidc_prefetch": {"status": "ready"},
                "stage_timings": {
                    "handler.factory": {"count": 1, "errors": 0, "duration_ms": 50.0},
                    "auth.oidc": {"count": 1, "errors": 0, "duration_ms": 2.0},
                },
            }
            if provisioned
            else None
        ),
        "oidc_attempts": (
            [
                {
                    "operation": "oidc_fetch",
                    "phase": "init",
                    "aws_request_id": None,
                    "endpoint": "jwks",
                    "event": "attempt_finished",
                    "attempt": 1,
                    "compensation": False,
                    "outcome": "ok",
                    "failure": None,
                    "duration_ms": 1.0,
                }
            ]
            if provisioned
            else []
        ),
    }


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ({"entrypoint": "semantic_service.runtime"}, "entrypoint"),
        ({"runtime_preimported": True}, "entrypoint"),
        ({"configuration_requests": 0}, "configuration_requests"),
        ({"configuration_requests": 2}, "configuration_requests"),
        ({"configuration_requests": True}, "configuration_requests"),
        ({"init_configuration_requests": 1}, "init_configuration_requests"),
        ({"metadata_requests": {}}, "metadata_requests"),
        ({"metadata_requests": {"discovery": 1, "jwks": 1}}, "metadata_requests"),
        ({"metadata_requests": {"discovery": False, "jwks": 0}}, "metadata_requests"),
        ({"init_metadata_requests": {"discovery": 0, "jwks": 1}}, "init_metadata_requests"),
        ({"oidc_attempts": [{"operation": "oidc_fetch"}]}, "oidc_attempts"),
        ({"first_invocation_unconsumed": False}, "consumed an invocation"),
        ({"network_attempts": {}}, "network_attempts"),
        (
            {"network_attempts": {"getaddrinfo": 1, "connect": 0, "connect_ex": 0}},
            "network_attempts",
        ),
        (
            {"network_attempts": {"getaddrinfo": False, "connect": 0, "connect_ex": 0}},
            "network_attempts",
        ),
        ({"source_compiled_modules": [ENTRYPOINT]}, "source compilation"),
        ({"init_record": {"status": "deferred"}}, "must be silent"),
        ({"combined_ms": 100.0}, "combined_ms"),
        ({"init_oidc_ms": 1.0}, "must be silent"),
    ],
)
def test_sample_rejects_invalid_measurement_preconditions(
    benchmark: ModuleType, replacement: dict[str, object], message: str
) -> None:
    row = {**valid_row("on-demand", "normal_import"), **replacement}
    with pytest.raises(RuntimeError, match=message):
        benchmark._validate_measurement(row, variant="on-demand", mode="normal_import", pair=0)


def test_provisioned_sample_requires_the_real_successful_factory_stage(
    benchmark: ModuleType,
) -> None:
    row = valid_row("provisioned-concurrency", "normal_import")
    row["init_record"]["stage_timings"] = {}
    with pytest.raises(RuntimeError, match="successful provisioned Init factory stage"):
        benchmark._validate_measurement(
            row, variant="provisioned-concurrency", mode="normal_import", pair=0
        )


@pytest.mark.parametrize("value", [0.0, 1.0, -1.0, True, float("inf")])
def test_provisioned_sample_requires_prefetch_timing_to_match_the_real_stage(
    benchmark: ModuleType, value: object
) -> None:
    row = valid_row("provisioned-concurrency", "normal_import")
    row["init_oidc_ms"] = value
    with pytest.raises(RuntimeError, match="init_oidc_ms|OIDC prefetch stage"):
        benchmark._validate_measurement(
            row, variant="provisioned-concurrency", mode="normal_import", pair=0
        )


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ({"metadata_requests": {"discovery": 0, "jwks": 0}}, "metadata_requests"),
        ({"metadata_requests": {"discovery": 1, "jwks": 2}}, "metadata_requests"),
        ({"metadata_requests": {"discovery": 1, "jwks": True}}, "metadata_requests"),
        ({"init_metadata_requests": {"discovery": 0, "jwks": 0}}, "init_metadata_requests"),
        ({"oidc_attempts": []}, "oidc_attempts"),
    ],
)
def test_provisioned_sample_requires_exactly_one_offline_metadata_fetch_per_endpoint(
    benchmark: ModuleType, replacement: dict[str, object], message: str
) -> None:
    row = {**valid_row("provisioned-concurrency", "normal_import"), **replacement}
    with pytest.raises(RuntimeError, match=message):
        benchmark._validate_measurement(
            row, variant="provisioned-concurrency", mode="normal_import", pair=0
        )


@pytest.mark.parametrize("prefetch", [None, {}, {"status": "failed"}, {"status": "not_started"}])
def test_provisioned_sample_rejects_failed_or_absent_prefetch(
    benchmark: ModuleType, prefetch: object
) -> None:
    row = valid_row("provisioned-concurrency", "normal_import")
    row["init_record"]["oidc_prefetch"] = prefetch
    with pytest.raises(RuntimeError, match="successful provisioned OIDC prefetch"):
        benchmark._validate_measurement(
            row, variant="provisioned-concurrency", mode="normal_import", pair=0
        )


@pytest.mark.parametrize(
    "replacement",
    [
        {"phase": "request"},
        {"aws_request_id": "synthetic-request"},
        {"endpoint": "discovery"},
        {"outcome": "failed"},
        {"compensation": True},
        {"attempt": 2},
        {"attempt": True},
        {"failure": {"category": "timeout"}},
    ],
)
def test_provisioned_sample_rejects_inconsistent_metadata_attempt_telemetry(
    benchmark: ModuleType, replacement: dict[str, object]
) -> None:
    row = valid_row("provisioned-concurrency", "normal_import")
    row["oidc_attempts"][0].update(replacement)
    with pytest.raises(RuntimeError, match="oidc_attempts"):
        benchmark._validate_measurement(
            row, variant="provisioned-concurrency", mode="normal_import", pair=0
        )


def test_source_compilation_sample_must_include_the_entry(benchmark: ModuleType) -> None:
    row = valid_row("on-demand", "source_compile")
    row["source_compiled_modules"] = ["semantic_service.runtime"]
    with pytest.raises(RuntimeError, match="source compilation"):
        benchmark._validate_measurement(row, variant="on-demand", mode="source_compile", pair=0)


def test_child_ignores_inherited_credentials_profile_and_proxy() -> None:
    row = child(
        "provisioned-concurrency",
        "normal_import",
        {
            "AWS_PROFILE": "not-a-real-profile",
            "AWS_ACCESS_KEY_ID": "caller-test-key",
            "AWS_SECRET_ACCESS_KEY": "caller-test-secret",
            "AWS_SESSION_TOKEN": "caller-test-token",
            "AWS_ENDPOINT_URL": "https://endpoint.invalid",
            "HTTPS_PROXY": "https://proxy.invalid",
        },
    )
    assert row["configuration_requests"] == 1
    assert row["network_attempts"] == {"getaddrinfo": 0, "connect": 0, "connect_ex": 0}


def test_on_demand_does_not_fetch_metadata() -> None:
    row = child("on-demand", "normal_import")
    assert row["metadata_process_ids"] == row["metadata_transport_ids"] == []


def test_prefetch_uses_the_parent_process_and_a_separate_http_transport() -> None:
    row = child("provisioned-concurrency", "normal_import")
    assert row["metadata_process_ids"] == [row["process_id"]]
    assert len(row["metadata_transport_ids"]) == 1
    assert row["handler_transport_id"] not in row["metadata_transport_ids"]


@pytest.mark.parametrize(
    "replacement",
    [
        {"process_id": True},
        {"metadata_process_ids": [101]},
        {"metadata_transport_ids": [200]},
        {"metadata_transport_ids": []},
        {"metadata_transport_ids": [300, 301]},
    ],
)
def test_report_rejects_wrong_process_or_shared_transport(
    benchmark: ModuleType, replacement: dict[str, object]
) -> None:
    row = {**valid_row("provisioned-concurrency", "normal_import"), **replacement}
    with pytest.raises(RuntimeError, match="process|transport"):
        benchmark._validate_measurement(
            row, variant="provisioned-concurrency", mode="normal_import", pair=0
        )


@pytest.mark.parametrize(
    "attempt",
    [
        'socket.getaddrinfo("example.invalid", 443)',
        'socket.socket().connect(("192.0.2.1", 443))',
        'socket.socket().connect_ex(("192.0.2.1", 443))',
    ],
    ids=["dns", "connect", "connect_ex"],
)
def test_child_rejects_even_caught_network_attempts(attempt: str) -> None:
    # The OS entry points are safe sentinels before the benchmark installs its guards.
    script = f"""
import runpy
import socket
def sentinel(*args, **kwargs):
    raise AssertionError("sentinel prevents real I/O if the benchmark guard regresses")
socket.getaddrinfo = sentinel
socket.socket.connect = sentinel
socket.socket.connect_ex = sentinel
benchmark = runpy.run_path({str(SCRIPT)!r})
original = benchmark["_configuration"]
def probe():
    try:
        {attempt}
    except AssertionError:
        pass
    return original()
benchmark["_child"].__globals__["_configuration"] = probe
benchmark["_child"]("on-demand", "normal_import")
"""
    # Fixed local test program and fixture paths; argv is not request-controlled.
    # nosemgrep: dangerous-subprocess-use-audit
    result = subprocess.run(  # nosec B603
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "network_attempts must contain only zero counts" in result.stderr


def test_child_restores_http_transport_patch_after_measurement() -> None:
    script = f"""
import runpy
benchmark = runpy.run_path({str(SCRIPT)!r})
benchmark["_child"]("on-demand", "normal_import")
import httpx
assert httpx.HTTPTransport.handle_request.__name__ == "handle_request"
from semantic_service import _request_prefetch
assert _request_prefetch.prefetch.__module__ == "semantic_service._request_prefetch"
"""
    # Fixed local test program and fixture paths; argv is not request-controlled.
    # nosemgrep: dangerous-subprocess-use-audit
    result = subprocess.run(  # nosec B603
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_child_captures_raw_attempts_written_in_the_current_process() -> None:
    script = f"""
import json
import os
import runpy
benchmark = runpy.run_path({str(SCRIPT)!r})
row = benchmark["_child"]("provisioned-concurrency", "normal_import")
assert len(row["oidc_attempts"]) == 1
assert row["oidc_attempts"][0]["phase"] == "init"
assert row["oidc_attempts"][0]["outcome"] == "ok"
assert row["metadata_process_ids"] == [os.getpid()]
print(json.dumps(row))
"""
    # Fixed local test program and fixture paths; argv is not request-controlled.
    # nosemgrep: dangerous-subprocess-use-audit
    result = subprocess.run(  # nosec B603
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads(result.stdout)
    assert row["metadata_requests"] == {"discovery": 1, "jwks": 1}


def test_measurement_counters_are_snapshots_not_live_transport_state() -> None:
    script = f"""
import runpy
benchmark = runpy.run_path({str(SCRIPT)!r})
validate = benchmark["_validate_measurement"]
def validate_snapshot(row, **kwargs):
    validate(row, **kwargs)
    import httpx
    before = dict(row["metadata_requests"])
    with httpx.Client() as client:
        client.get("https://identity.invalid/discovery")
    assert row["metadata_requests"] == before, "row changed after measurement"
benchmark["_child"].__globals__["_validate_measurement"] = validate_snapshot
benchmark["_child"]("on-demand", "normal_import")
"""
    # Fixed local test program and fixture paths; argv is not request-controlled.
    # nosemgrep: dangerous-subprocess-use-audit
    result = subprocess.run(  # nosec B603
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_default_run_alternates_twenty_pairs_in_each_compilation_mode(
    benchmark: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command[-1] == "--child"
        assert kwargs["env"] == benchmark._offline_environment()
        payload = json.loads(kwargs["input"])
        calls.append((payload["variant"], payload["mode"]))
        # CompletedProcess only constructs a mock result; it starts no process.
        # nosemgrep: dangerous-subprocess-use-audit
        return subprocess.CompletedProcess(
            command, 0, json.dumps(valid_row(payload["variant"], payload["mode"])), ""
        )

    output = tmp_path / "report.json"
    monkeypatch.setattr(benchmark, "subprocess", SimpleNamespace(run=run))
    monkeypatch.setattr(sys, "argv", ["benchmark", "--output", str(output)])
    benchmark.main()
    report = json.loads(output.read_text())
    assert report["pairs_per_mode"] == 20
    assert len(report["rows"]) == len(calls) == 80
    expected = [
        (variant, mode)
        for mode in ("normal_import", "source_compile")
        for pair in range(20)
        for variant in (
            ("on-demand", "provisioned-concurrency")
            if pair % 2 == 0
            else ("provisioned-concurrency", "on-demand")
        )
    ]
    assert calls == expected
    assert report["preconditions"]["validated"] is True
    assert report["preconditions"]["metadata_requests_per_endpoint"] == {
        "on-demand": 0,
        "provisioned-concurrency": 1,
    }
    assert "offline metadata" in report["method"]
    assert "network latency" in report["method"]
    assert "different work" in report["method"]
    assert "not a direct latency benefit comparison" in report["method"]
    for mode in benchmark.MODES:
        assert report["summaries"][mode]["on-demand"]["init_oidc_ms"]["p95"] == 0
        assert report["summaries"][mode]["provisioned-concurrency"]["init_oidc_ms"]["p95"] == 2.0


def test_insufficient_pairs_are_rejected(
    benchmark: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["benchmark", "--repeats", "19", "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        benchmark.main()
    assert error.value.code == 2
    assert not output.exists()

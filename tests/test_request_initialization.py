"""REQUEST Init is Lambda-only, observable, fatal on failure and request-isolated."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess  # nosec B404
import sys
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_oidc_reliability import signing_key  # noqa: F401

from semantic_core._timing import _STAGES, stage
from semantic_service import (
    _request_prefetch,  # noqa: F401
    runtime,
)
from semantic_service.auth import OIDCConfig, OIDCVerifier

MODULE = "semantic_service.request_runtime"
HANDLER = MODULE + ".request_handler"


def assert_init_record(row):
    assert set(row) == {
        "operation",
        "status",
        "duration_ms",
        "request_entry_import_ms",
        "runtime_module_init_ms",
        "factory_ms",
        "oidc_prefetch",
        "stage_timings",
    }
    assert row["operation"] == "request_init"
    assert row["status"] in {"ready", "failed"}
    assert row["oidc_prefetch"] == {
        "status": "ready" if row["status"] == "ready" else "not_started"
    }
    assert set(row["stage_timings"]) <= _STAGES
    assert row["factory_ms"] == row["stage_timings"]["handler.factory"]["duration_ms"]
    for key, value in row.items():
        if key not in {"operation", "status", "stage_timings", "oidc_prefetch"}:
            assert isinstance(value, (int, float)) and value >= 0
    for timing in row["stage_timings"].values():
        assert set(timing) == {"count", "duration_ms", "max_ms", "errors"}
        assert all(isinstance(value, (int, float)) for value in timing.values())


@pytest.fixture
def ready_handler(monkeypatch, signing_key):  # noqa: F811
    events = []
    prefetch_calls = []

    def no_http(request):
        raise AssertionError("successful factory fixture must stub prefetch")

    client = httpx.Client(transport=httpx.MockTransport(no_http), trust_env=False)
    verifier = OIDCVerifier(
        OIDCConfig(
            "https://idp.example.invalid",
            "https://idp.example.invalid/jwks",
            "fixture-audience",
            frozenset({"idp.example.invalid"}),
        ),
        client,
    )

    def metadata(unused, request):
        prefetch_calls.append(True)
        return httpx.Response(200, json={"keys": [signing_key[1]]})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", metadata)

    def handler(event, context):
        events.append(event)
        return {"accepted": True}

    handler.verifier = verifier
    handler.events = events
    handler.prefetch_calls = prefetch_calls
    yield handler
    client.close()


@pytest.fixture
def load_entrypoint(monkeypatch):
    monkeypatch.setattr(runtime, "_FIRST_INVOCATION", True)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "fixture-request")
    monkeypatch.setenv("_HANDLER", HANDLER)

    def load(initialization_type):
        sys.modules.pop(MODULE, None)
        if initialization_type is None:
            monkeypatch.delenv("AWS_LAMBDA_INITIALIZATION_TYPE", raising=False)
        else:
            monkeypatch.setenv("AWS_LAMBDA_INITIALIZATION_TYPE", initialization_type)
        return importlib.import_module(MODULE)

    yield load
    sys.modules.pop(MODULE, None)


@pytest.mark.parametrize("initialization_type", [None, "on-demand", "snap-start", "unknown"])
def test_ordinary_import_has_no_factory_collection_or_telemetry(
    monkeypatch, capfd, load_entrypoint, initialization_type
):
    calls = []
    monkeypatch.setattr(runtime, "_request", lambda: calls.append("factory"))
    timing = importlib.import_module("semantic_core._timing")
    # The controller is imported above so this guard patch cannot become its
    # permanent imported collector. Fresh-process import guards live separately.
    monkeypatch.setattr(timing, "collect", lambda: pytest.fail("deferred Init collected timings"))
    load_entrypoint(initialization_type)
    assert calls == [] and runtime._FIRST_INVOCATION is True
    assert capfd.readouterr().out == ""


@pytest.mark.parametrize(
    "marker,value", [("AWS_LAMBDA_FUNCTION_NAME", ""), ("_HANDLER", "other.handler")]
)
def test_pc_marker_alone_does_not_initialize_build_machine(
    monkeypatch, capfd, load_entrypoint, marker, value
):
    monkeypatch.setenv(marker, value)
    monkeypatch.setattr(runtime, "_request", lambda: pytest.fail("build host ran factory"))
    load_entrypoint("provisioned-concurrency")
    assert capfd.readouterr().out == ""


@pytest.mark.parametrize("matches_loaded_module", [True, False])
def test_preparation_guard_uses_loaded_module_name(
    monkeypatch, capfd, ready_handler, matches_loaded_module
):
    module_name = "semantic_service._renamed_request_entry"
    spec = importlib.util.spec_from_file_location(
        module_name, Path(runtime.__file__).with_name("request_runtime.py")
    )
    assert spec is not None and spec.loader is not None
    entry = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, entry)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "fixture-request")
    monkeypatch.setenv("AWS_LAMBDA_INITIALIZATION_TYPE", "provisioned-concurrency")
    monkeypatch.setenv(
        "_HANDLER", f"{module_name}.request_handler" if matches_loaded_module else HANDLER
    )
    monkeypatch.setattr(runtime, "_FIRST_INVOCATION", True)
    calls = []
    monkeypatch.setattr(runtime, "_request", lambda: calls.append("factory") or ready_handler)
    spec.loader.exec_module(entry)
    assert calls == (["factory"] if matches_loaded_module else [])
    assert runtime._FIRST_INVOCATION is True
    raw = "\n".join(
        line
        for line in capfd.readouterr().out.splitlines()
        if json.loads(line)["operation"] != "oidc_fetch"
    )
    if matches_loaded_module:
        row = json.loads(raw)
        assert_init_record(row)
        assert row["status"] == "ready"
        assert ready_handler.prefetch_calls == [True]
    else:
        assert raw == ""


def test_entrypoint_import_timer_is_independent_of_preimported_runtime():
    code = f"""
import json, time
started = time.perf_counter()
import {MODULE} as entry
wall_ms = (time.perf_counter() - started) * 1000
print(json.dumps({{'entry_ms': entry._ENTRY_IMPORT_MS, 'wall_ms': wall_ms}}))
"""
    for preimport in ("", "import semantic_service.runtime\n"):
        # Fixed local test program and fixture paths; argv is not request-controlled.
        # nosemgrep: dangerous-subprocess-use-audit
        result = subprocess.run(  # nosec B603
            [sys.executable, "-c", preimport + code],
            env={
                "PATH": os.defpath,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
            text=True,
            capture_output=True,
            check=True,
            timeout=20,
        )
        row = json.loads(result.stdout)
        assert 0 <= row["entry_ms"] <= row["wall_ms"]


def test_forwarder_uses_current_runtime_handler(monkeypatch, load_entrypoint):
    entry = load_entrypoint(None)
    monkeypatch.setattr(runtime, "request_handler", lambda event, context: {"event": event})
    assert entry.request_handler("forwarded", object()) == {"event": "forwarded"}


def test_provisioned_init_builds_once_without_consuming_first_invocation(
    monkeypatch, capfd, load_entrypoint, ready_handler
):
    calls = []
    events = ready_handler.events

    @lru_cache(maxsize=1)
    def factory():
        with stage("runtime.config"), stage("runtime.aws_client.secretsmanager"):
            calls.append("configuration")
        return ready_handler

    monkeypatch.setattr(runtime, "_request", factory)
    entrypoint = load_entrypoint("provisioned-concurrency")
    assert calls == ["configuration"] and events == [] and runtime._FIRST_INVOCATION is True
    assert ready_handler.prefetch_calls == [True]
    for event in [{"id": "first"}, {"id": "second"}]:
        assert entrypoint.request_handler(event, SimpleNamespace()) == {"accepted": True}
    init, first, second = [
        json.loads(line)
        for line in capfd.readouterr().out.splitlines()
        if json.loads(line)["operation"] != "oidc_fetch"
    ]
    assert_init_record(init)
    assert init["status"] == "ready"
    assert init["stage_timings"]["handler.factory"]["count"] == 1
    assert init["stage_timings"]["handler.factory"]["errors"] == 0
    assert "runtime.aws_client.secretsmanager" in init["stage_timings"]
    assert [row["first_invocation"] for row in (first, second)] == [True, False]
    assert calls == ["configuration"] and events == [{"id": "first"}, {"id": "second"}]


@pytest.mark.parametrize("failure", [ValueError, TypeError, AttributeError])
def test_failed_preparation_is_fatal_and_redacted(monkeypatch, capfd, load_entrypoint, failure):
    def factory():
        with stage("runtime.config"), stage("runtime.aws_client.secretsmanager"):
            raise failure("private-token https://private.invalid secret-material-9281")

    monkeypatch.setattr(runtime, "_request", factory)
    with pytest.raises(RuntimeError, match="REQUEST initialization failed") as exc:
        load_entrypoint("provisioned-concurrency")
    assert exc.value.__suppress_context__
    assert MODULE not in sys.modules and runtime._FIRST_INVOCATION is True
    raw = "\n".join(
        line
        for line in capfd.readouterr().out.splitlines()
        if json.loads(line)["operation"] != "oidc_fetch"
    )
    assert all(
        value not in raw for value in ("private-token", "private.invalid", "secret-material-9281")
    )
    init = json.loads(raw)
    assert_init_record(init)
    assert init["status"] == "failed"
    assert init["stage_timings"]["handler.factory"]["errors"] == 1


@pytest.mark.parametrize("failure", [SystemExit, KeyboardInterrupt])
def test_process_control_exceptions_remain_fatal(monkeypatch, capfd, load_entrypoint, failure):
    def factory():
        raise failure()

    monkeypatch.setattr(runtime, "_request", factory)
    with pytest.raises(failure):
        load_entrypoint("provisioned-concurrency")
    assert MODULE not in sys.modules
    row = [json.loads(line) for line in capfd.readouterr().out.splitlines()][-1]
    assert_init_record(row)
    assert row["status"] == "failed"


@pytest.mark.parametrize(
    "failure",
    [None, ValueError, SystemExit, KeyboardInterrupt],
    ids=["ready", "error", "exit", "interrupt"],
)
def test_missing_factory_stage_preserves_result_and_exception(
    monkeypatch, capfd, load_entrypoint, ready_handler, failure
):
    timing = importlib.import_module("semantic_core._timing")
    monkeypatch.setattr(timing, "_STAGES", timing._STAGES - {"handler.factory"})
    clock = [1.0]
    monkeypatch.setattr(timing.time, "perf_counter", lambda: clock[0])
    calls = []

    def factory():
        calls.append("factory")
        clock[0] += 0.125
        if failure is ValueError:
            raise failure("synthetic-sensitive-marker")
        if failure is SystemExit:
            raise failure(3)
        if failure is not None:
            raise failure()
        return ready_handler

    monkeypatch.setattr(runtime, "_request", factory)
    if failure is None:
        load_entrypoint("provisioned-concurrency")
    else:
        expected = RuntimeError if failure is ValueError else failure
        with pytest.raises(expected) as error:
            load_entrypoint("provisioned-concurrency")
        assert MODULE not in sys.modules
        if failure is ValueError:
            assert str(error.value) == "REQUEST initialization failed"
            assert error.value.__suppress_context__
        elif failure is SystemExit:
            assert error.value.code == 3
    assert calls == ["factory"] and runtime._FIRST_INVOCATION is True
    raw = "\n".join(
        line
        for line in capfd.readouterr().out.splitlines()
        if json.loads(line)["operation"] != "oidc_fetch"
    )
    assert "synthetic-sensitive-marker" not in raw
    row = json.loads(raw)
    assert row["status"] == ("ready" if failure is None else "failed")
    assert row["operation"] == "request_init"
    if failure is None:
        assert set(row["stage_timings"]) == {"auth.oidc", "auth.jwks"}
        assert row["stage_timings"]["auth.oidc"]["errors"] == 0
        assert row["oidc_prefetch"] == {"status": "ready"}
        assert ready_handler.prefetch_calls == [True]
    else:
        assert row["stage_timings"] == {}
        assert row["oidc_prefetch"] == {"status": "not_started"}
        assert ready_handler.prefetch_calls == []
    assert row["duration_ms"] == row["factory_ms"] == 125.0


def test_prepare_accepts_import_timing_without_global_order_dependency(
    monkeypatch, capfd, load_entrypoint, ready_handler
):
    entry = load_entrypoint(None)
    monkeypatch.setenv("AWS_LAMBDA_INITIALIZATION_TYPE", "provisioned-concurrency")
    monkeypatch.delattr(entry, "_ENTRY_IMPORT_MS")
    monkeypatch.setattr(runtime, "_request", lambda: ready_handler)
    entry._prepare(12.5)
    row = [json.loads(line) for line in capfd.readouterr().out.splitlines()][-1]
    assert row["request_entry_import_ms"] == 12.5
    assert row["oidc_prefetch"] == {"status": "ready"}
    assert ready_handler.prefetch_calls == [True]


@pytest.mark.parametrize("factory_stage_present", [True, False])
def test_total_init_duration_includes_prefetch_but_factory_duration_does_not(
    monkeypatch, capfd, load_entrypoint, ready_handler, factory_stage_present
):
    timing = importlib.import_module("semantic_core._timing")
    if not factory_stage_present:
        monkeypatch.setattr(timing, "_STAGES", timing._STAGES - {"handler.factory"})
    clock = [1.0]
    monkeypatch.setattr(timing.time, "perf_counter", lambda: clock[0])

    def factory():
        clock[0] += 0.125
        return ready_handler

    original_transport = httpx.HTTPTransport.handle_request

    def metadata(client, request):
        clock[0] += 0.400
        return original_transport(client, request)

    monkeypatch.setattr(runtime, "_request", factory)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", metadata)
    load_entrypoint("provisioned-concurrency")
    row = [json.loads(line) for line in capfd.readouterr().out.splitlines()][-1]
    assert row["status"] == "ready"
    assert row["oidc_prefetch"] == {"status": "ready"}
    assert row["factory_ms"] == 125.0
    assert row["stage_timings"]["auth.oidc"]["duration_ms"] == 400.0
    assert row["duration_ms"] == 525.0
    assert ready_handler.prefetch_calls == [True]
    assert ("handler.factory" in row["stage_timings"]) is factory_stage_present


def test_pinned_configuration_failure_does_not_promise_in_process_recovery():
    script = Path(__file__).with_name("fixtures") / "request-pinned-config-init.py"
    # Fixed local test program and fixture paths; argv is not request-controlled.
    # nosemgrep: dangerous-subprocess-use-audit
    result = subprocess.run(  # nosec B603
        [sys.executable, "-c", script.read_text()],
        env={"PATH": os.defpath, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "replacement environment required" in result.stdout

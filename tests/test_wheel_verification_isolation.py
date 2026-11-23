"""The wheel gate must not inherit Lambda activation or developer AWS access."""

import importlib.util
import os
import socket
import subprocess  # nosec B404
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def verifier():
    spec = importlib.util.spec_from_file_location(
        "wheel_verify_test", ROOT / "scripts/verify_wheel.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wheel_gate_spawns_clean_interpreter(verifier, monkeypatch):
    for key in (
        "AWS_LAMBDA_FUNCTION_NAME",
        "AWS_LAMBDA_INITIALIZATION_TYPE",
        "_HANDLER",
        "AWS_ACCESS_KEY_ID",
        "SEMSEARCH_CONFIG_BUCKET",
    ):
        monkeypatch.setenv(key, "must-not-inherit")
    monkeypatch.setattr(sys, "argv", [str(ROOT / "scripts/verify_wheel.py")])
    calls = []
    monkeypatch.setattr(
        verifier,
        "subprocess",
        SimpleNamespace(run=lambda *args, **kwargs: calls.append((args, kwargs))),
        raising=False,
    )
    verifier.main()
    assert len(calls) == 1
    command = calls[0][0][0]
    assert command[0] == sys.executable and "-I" in command and command[-1] == "--isolated"
    env = calls[0][1]["env"]
    assert all(
        key not in env
        for key in (
            "AWS_LAMBDA_FUNCTION_NAME",
            "_HANDLER",
            "AWS_ACCESS_KEY_ID",
            "SEMSEARCH_CONFIG_BUCKET",
        )
    )
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull


def test_isolated_gate_rejects_dns_and_connection_and_restores_environment(verifier, monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_INITIALIZATION_TYPE", "provisioned-concurrency")
    monkeypatch.setenv("_HANDLER", "semantic_service.request_runtime.request_handler")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-request")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: "unblocked")
    monkeypatch.setattr(socket.socket, "connect", lambda *args: "unblocked")
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *args: "unblocked")
    calls = []

    def probe():
        assert "AWS_LAMBDA_INITIALIZATION_TYPE" not in os.environ and "_HANDLER" not in os.environ
        with pytest.raises(RuntimeError, match="network"):
            socket.getaddrinfo("identity.invalid", 443)
        with socket.socket() as client:
            for method in (client.connect, client.connect_ex):
                with pytest.raises(RuntimeError, match="network"):
                    method(("127.0.0.1", 1))
        calls.append("verified")

    monkeypatch.setattr(verifier, "verify", probe, raising=False)
    verifier.verify_isolated()
    assert calls == ["verified"]
    assert os.environ["AWS_LAMBDA_INITIALIZATION_TYPE"] == "provisioned-concurrency"


def isolated_entry_probe(tmp_path, entry):
    wheel = tmp_path / "fixture.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("semantic_service/__init__.py", "# isolated fixture\n")
        archive.writestr(
            "semantic_service/facade.py",
            "def _router():\n"
            "    raise AssertionError('real factory must not execute during wheel inspection')\n"
            "def emit_record(record):\n"
            "    assert record['status'] == 'ok'\n"
            "class ServiceError(Exception):\n"
            "    pass\n",
        )
        archive.writestr("semantic_service/facade_lambda.py", entry)
    script = """
import importlib.util
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("wheel_verifier", sys.argv[1])
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
wheel = Path(sys.argv[2])
sys.path.insert(0, str(wheel))
def probe():
    module = verifier.import_packaged_module("semantic_service.facade_lambda", wheel)
    assert module.__file__.startswith(str(wheel) + "/")
    assert module._ROUTER.initialization_timings == {}
verifier.verify = probe
verifier.verify_isolated()
"""
    return subprocess.run(  # nosec B603
        [sys.executable, "-I", "-c", script, str(ROOT / "scripts/verify_wheel.py"), str(wheel)],
        text=True,
        capture_output=True,
        env={"PATH": os.defpath},
    )


def test_actual_eager_entry_imports_from_wheel_with_only_its_factory_replaced(tmp_path):
    entry = (ROOT / "src/semantic_service/facade_lambda.py").read_bytes()
    result = isolated_entry_probe(tmp_path, entry)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "entry",
    [
        "from semantic_service import facade\n"
        "def handler(event, context):\n"
        "    return facade._router()\n",
        "from semantic_service import facade\n"
        "_ROUTER = facade._router()\n"
        "_SECOND = facade._router()\n",
    ],
    ids=["factory-deferred-until-invoke", "factory-created-twice"],
)
def test_entry_verification_rejects_missing_or_repeated_init_factory(tmp_path, entry):
    result = isolated_entry_probe(tmp_path, entry)
    assert result.returncode != 0
    assert "exactly once during module initialization" in result.stderr


def test_special_entry_cannot_use_a_facade_imported_outside_the_wheel(
    verifier, monkeypatch, tmp_path
):
    calls = []
    facade = SimpleNamespace(__name__="semantic_service.facade", __file__="/outside/facade.py")
    monkeypatch.setattr(
        verifier.importlib, "import_module", lambda name: calls.append(name) or facade
    )
    with pytest.raises(SystemExit, match="outside the wheel"):
        verifier.import_packaged_module("semantic_service.facade_lambda", tmp_path / "x.whl")
    assert calls == ["semantic_service.facade"]


@pytest.mark.parametrize("name", ["os", "semantic_service.not_present", "semantic_service..auth"])
def test_import_allowlist_rejects_unknown_before_import(verifier, monkeypatch, tmp_path, name):
    def forbidden(name):
        raise AssertionError("unknown module reached import")

    monkeypatch.setattr(verifier.importlib, "import_module", forbidden)
    with pytest.raises(ValueError):
        verifier.import_packaged_module(name, tmp_path / "unused.whl")

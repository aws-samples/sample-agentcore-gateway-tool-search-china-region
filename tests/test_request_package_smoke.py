"""The isolated REQUEST smoke must exercise storage and block every network entry."""

from __future__ import annotations

import os
import subprocess  # nosec B404
import sys
from importlib import metadata
from pathlib import Path

import pytest
from scripts import build_lambda

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "tests/fixtures/request-package-smoke.py"


@pytest.fixture
def request_package(tmp_path: Path) -> Path:
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
    return isolated


def run_smoke(
    package: Path, script: str, initialization_type: str = "on-demand"
) -> subprocess.CompletedProcess[str]:
    script_path = package.parent / "smoke-probe.py"
    script_path.write_text(script)
    return subprocess.run(  # nosec B603
        [sys.executable, "-I", "-S", str(script_path), str(package)],
        cwd=ROOT,
        env={
            "PATH": os.defpath,
            "HOME": str(package.parent),
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_ACCESS_KEY_ID": "isolated-fixture",
            "AWS_SECRET_ACCESS_KEY": "isolated-fixture",
            "AWS_REGION": "cn-north-1",
            "AWS_LAMBDA_FUNCTION_NAME": "request-fixture",
            "_HANDLER": "semantic_service.request_runtime.request_handler",
            "AWS_LAMBDA_INITIALIZATION_TYPE": initialization_type,
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_pc_smoke_never_launches_a_metadata_process(request_package: Path) -> None:
    source = SMOKE.read_text()
    anchor = "with ExitStack() as stack:\n"
    assert source.count(anchor) == 1
    source = source.replace(
        anchor,
        "def forbidden_process(*args, **kwargs):\n"
        "    raise AssertionError('metadata prefetch must stay in the parent process')\n"
        "os.posix_spawn = forbidden_process\n"
        "os.fork = forbidden_process\n" + anchor,
    )
    result = run_smoke(request_package, source, "provisioned-concurrency")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("filename", "anchor"),
    [
        ("storage.py", "            protected = self._read_protected()"),
        ("storage.py", "        policy = _decode(self._get(control.policy, limit="),
        ("storage.py", "        raw = _decode(self._get(control.safety, limit="),
        ("auth.py", "        now = int(self.clock())\n        minute = now // 60"),
    ],
    ids=["control", "policy", "safety", "limiter"],
)
def test_smoke_detects_missing_lazy_dependency_in_real_request_paths(
    request_package: Path, filename: str, anchor: str
) -> None:
    """A future closure omission must fail smoke even when product errors sanitize it."""
    source = request_package / "semantic_service" / filename
    program = source.read_text()
    assert program.count(anchor) == 1
    indent = anchor[: len(anchor) - len(anchor.lstrip())]
    source.write_text(
        program.replace(anchor, indent + "import request_smoke_missing_dependency\n" + anchor)
    )
    result = run_smoke(request_package, SMOKE.read_text())
    assert result.returncode != 0, (
        f"{filename} path was bypassed; missing dependency went undetected\n{result.stdout}"
    )
    assert "PASS: isolated REQUEST factory, JWT and handler" not in result.stdout
    assert "ServiceError: internal failure" in result.stderr


@pytest.mark.parametrize(
    "attempt",
    [
        'socket.getaddrinfo("example.invalid", 443)',
        'socket.socket().connect(("192.0.2.1", 443))',
        'socket.socket().connect_ex(("192.0.2.1", 443))',
    ],
    ids=["dns", "connect", "connect_ex"],
)
def test_smoke_network_guards_reject_calls_before_os_io(
    request_package: Path, attempt: str
) -> None:
    # Sentinel replacements make even a broken guard safe to test without real I/O.
    prelude = """
import socket
def escaped_guard(*args, **kwargs):
    raise RuntimeError("network guard missing")
socket.getaddrinfo = escaped_guard
socket.socket.connect = escaped_guard
socket.socket.connect_ex = escaped_guard
"""
    setup = SMOKE.read_text().split("# Imports happen", 1)[0]
    result = run_smoke(request_package, prelude + setup + "\n" + attempt)
    assert result.returncode != 0
    assert "isolated package test attempted network access" in result.stderr
    assert "network guard missing" not in result.stderr


def test_smoke_rejects_available_but_forbidden_source_import(request_package: Path) -> None:
    """Availability and accidental loading are separate package invariants."""
    source = request_package / "semantic_core/search.py"
    source.write_text("value = 1\n")
    anchor = 'event["mcp"]["gatewayRequest"]["headers"]["Authorization"] = "Bearer invalid"'
    script = SMOKE.read_text().replace(anchor, "import semantic_core.search\n" + anchor)
    result = run_smoke(request_package, script)
    assert result.returncode != 0, result.stdout
    assert "semantic_core.search" in result.stderr


def test_smoke_uses_and_closes_a_separate_prefetch_client(
    request_package: Path,
) -> None:
    probe = """
http_client_constructions = []
original_http_client_init = httpx.Client.__init__
def record_http_client_init(self, *args, **kwargs):
    http_client_constructions.append((self, kwargs.get("transport")))
    original_http_client_init(self, *args, **kwargs)
httpx.Client.__init__ = record_http_client_init
"""
    anchor = "import httpx  # noqa: E402"
    script = SMOKE.read_text().replace(anchor, anchor + "\n" + probe)
    script += """
assert len(http_client_constructions) == 2
request_client, request_transport = http_client_constructions[0]
prefetch_client, configured_transport = http_client_constructions[1]
assert isinstance(request_transport, httpx.MockTransport)
assert request_client is client is handler.verifier.client
assert prefetch_client is not request_client
assert configured_transport is None
assert prefetch_client.is_closed
"""
    result = run_smoke(request_package, script, "provisioned-concurrency")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("suppressed_outcome", ["failed", "ok"])
def test_smoke_rejects_missing_jwks_attempt_event(
    request_package: Path, suppressed_outcome: str
) -> None:
    """Suppress one event only in the isolated copy; behavior alone must not pass smoke."""
    source = request_package / "semantic_service/auth.py"
    program = source.read_text()
    anchor = "    try:\n        if started is None or not 1 <= attempt <= 3:\n"
    assert program.count(anchor) == 1
    source.write_text(
        program.replace(
            anchor,
            f'    if outcome == "{suppressed_outcome}":\n        return\n' + anchor,
        )
    )
    result = run_smoke(request_package, SMOKE.read_text())
    assert result.returncode != 0, (
        f"smoke accepted a missing {suppressed_outcome} JWKS attempt event\n{result.stdout}"
    )
    assert "PASS: isolated REQUEST factory, JWT and handler" not in result.stdout
    assert "JWKS attempt" in result.stderr


@pytest.mark.parametrize("initialization_type", ["on-demand", "provisioned-concurrency"])
def test_smoke_covers_jwks_attempts_through_dedicated_entrypoint(
    request_package: Path, initialization_type: str
) -> None:
    result = run_smoke(request_package, SMOKE.read_text(), initialization_type)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: isolated REQUEST factory, JWT and handler" in result.stdout

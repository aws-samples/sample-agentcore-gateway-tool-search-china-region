"""PC Init warms the real verifier without consuming an invocation or hiding outages."""

import json

import httpx
import pytest
from test_oidc_reliability import fixture as oidc_fixture  # noqa: F401
from test_oidc_reliability import (
    signing_key,  # noqa: F401
)
from test_request_initialization import load_entrypoint as entrypoint_fixture  # noqa: F401

from semantic_service import runtime
from semantic_service.auth import _OIDC_DIAGNOSTICS, OIDCVerifier


@pytest.fixture
def load_entrypoint(entrypoint_fixture):  # noqa: F811
    return entrypoint_fixture


@pytest.fixture
def fixture(oidc_fixture):  # noqa: F811
    return oidc_fixture


def install_handler(monkeypatch, fixture):
    def handler(event, context):
        fixture.verifier.verify(fixture.token())
        return {"accepted": True}

    handler.verifier = fixture.verifier
    monkeypatch.setattr(runtime, "_request", lambda: handler)


def records(capfd):
    return [json.loads(line) for line in capfd.readouterr().out.splitlines()]


def test_pc_init_warms_metadata_before_first_real_authentication(
    monkeypatch, capfd, load_entrypoint, fixture
):
    install_handler(monkeypatch, fixture)
    entry = load_entrypoint("provisioned-concurrency")
    assert fixture.endpoints == ["discovery", "jwks"]
    assert runtime._FIRST_INVOCATION is True
    init_rows = records(capfd)
    init = init_rows[-1]
    assert init["operation"] == "request_init" and init["status"] == "ready"
    assert init["oidc_prefetch"] == {"status": "ready"}
    assert init["stage_timings"]["auth.discovery"]["count"] == 1
    assert init["stage_timings"]["auth.jwks"]["count"] == 1
    assert init["duration_ms"] >= init["factory_ms"]
    assert init_rows[0]["phase"] == "init"
    assert init_rows[0]["aws_request_id"] is None
    assert entry.request_handler({}, object()) == {"accepted": True}
    first = records(capfd)[-1]
    assert first["first_invocation"] is True
    assert "auth_recovered" not in first
    assert "auth.jwks" not in first["stage_timings"]
    assert fixture.endpoints == ["discovery", "jwks"]


@pytest.mark.parametrize("endpoint", ["discovery", "jwks"])
def test_failed_prefetch_is_ready_redacted_and_retried_by_first_request(
    monkeypatch, capfd, load_entrypoint, fixture, endpoint
):
    install_handler(monkeypatch, fixture)
    fixture.steps[endpoint] = [
        httpx.ReadTimeout("private-token https://private.invalid") for _ in range(2)
    ]
    entry = load_entrypoint("provisioned-concurrency")
    init_rows = records(capfd)
    init = init_rows[-1]
    assert init["status"] == "ready"
    assert init["oidc_prefetch"] == {
        "status": "failed",
        "failure_kind": "dependency",
        "auth_failure": {
            "endpoint": endpoint,
            "category": "timeout",
            "reason": "read",
            "http_status": None,
            "cause": None,
        },
    }
    assert "private" not in json.dumps(init_rows)
    before = len(fixture.calls)
    fixture.steps["jwks"] = [httpx.ReadTimeout("another sensitive error")]
    assert entry.request_handler({}, object()) == {"accepted": True}
    assert fixture.endpoints[before:] == ["discovery", "jwks", "jwks"]
    first = records(capfd)[-1]
    assert first["auth_recovered"] is True and first["first_invocation"] is True


def test_init_recovery_has_its_own_diagnostics_and_does_not_leak_to_request(
    monkeypatch, capfd, load_entrypoint, fixture
):
    install_handler(monkeypatch, fixture)
    fixture.steps["jwks"] = [httpx.ReadTimeout("synthetic timeout")]
    entry = load_entrypoint("provisioned-concurrency")
    rows = records(capfd)
    assert rows[-1]["oidc_prefetch"] == {"status": "ready", "auth_recovered": True}
    assert [row["outcome"] for row in rows[:-1]] == ["failed", "ok"]
    assert all(row["phase"] == "init" for row in rows[:-1])
    entry.request_handler({}, object())
    first = records(capfd)[-1]
    assert "auth_recovered" not in first
    assert fixture.endpoints == ["discovery", "jwks", "jwks"]


@pytest.mark.parametrize("initialization_type", [None, "on-demand", "snap-start"])
def test_non_pc_import_defers_both_metadata_endpoints(
    monkeypatch, capfd, load_entrypoint, fixture, initialization_type
):
    install_handler(monkeypatch, fixture)
    entry = load_entrypoint(initialization_type)
    assert fixture.endpoints == [] and records(capfd) == []
    entry.request_handler({}, object())
    assert fixture.endpoints == ["discovery", "jwks"]
    rows = records(capfd)
    assert rows[-1]["first_invocation"] is True
    assert all("phase" not in row for row in rows)


def test_unexpected_prefetch_exception_is_nonfatal_and_redacted(
    monkeypatch, capfd, load_entrypoint, fixture
):
    install_handler(monkeypatch, fixture)

    def broken(self, **kwargs):
        raise ValueError("private-token https://private.invalid")

    monkeypatch.setattr(OIDCVerifier, "_prefetch", broken)
    load_entrypoint("provisioned-concurrency")
    init = records(capfd)[-1]
    assert init["status"] == "ready"
    assert init["oidc_prefetch"] == {"status": "failed", "failure_kind": "structural"}
    assert "private" not in json.dumps(init)


def test_success_cannot_be_reclassified_by_a_diagnostics_status_key(
    monkeypatch, capfd, load_entrypoint, fixture
):
    install_handler(monkeypatch, fixture)
    original = OIDCVerifier._prefetch

    def prefetch(self, **kwargs):
        original(self, **kwargs)
        _OIDC_DIAGNOSTICS.get()["status"] = "private-diagnostics-status"

    monkeypatch.setattr(OIDCVerifier, "_prefetch", prefetch)
    load_entrypoint("provisioned-concurrency")
    init = records(capfd)[-1]
    assert init["oidc_prefetch"] == {"status": "ready"}
    assert "private-diagnostics-status" not in json.dumps(init)


def test_partial_metadata_recovery_is_observable_without_success_metric(
    monkeypatch, capfd, load_entrypoint, fixture
):
    install_handler(monkeypatch, fixture)
    fixture.steps["discovery"] = [httpx.Response(503)]
    fixture.steps["jwks"] = [httpx.Response(503)]
    load_entrypoint("provisioned-concurrency")
    init = records(capfd)[-1]
    assert init["status"] == "ready"
    prefetch = init["oidc_prefetch"]
    assert prefetch["status"] == "failed"
    assert prefetch["failure_kind"] == "dependency"
    assert prefetch["metadata_recovered"] is True
    assert "auth_recovered" not in prefetch
    assert prefetch["auth_failure"]["endpoint"] == "jwks"


@pytest.mark.parametrize("failure", ["wrong_verifier", "unexpected_transport"])
def test_structural_prefetch_failure_is_distinct_from_dependency_failure(
    monkeypatch, capfd, load_entrypoint, fixture, failure
):
    install_handler(monkeypatch, fixture)
    if failure == "wrong_verifier":
        runtime._request().verifier = object()
    else:
        fixture.steps["discovery"] = [AssertionError("private-guard-text")]
    load_entrypoint("provisioned-concurrency")
    init = records(capfd)[-1]
    assert init["status"] == "ready"
    assert init["oidc_prefetch"]["status"] == "failed"
    assert init["oidc_prefetch"]["failure_kind"] == "structural"
    assert "private-guard-text" not in json.dumps(init)


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
def test_optional_prefetch_contains_process_control_exceptions(
    monkeypatch, capfd, load_entrypoint, fixture, failure
):
    install_handler(monkeypatch, fixture)

    def interrupted(self, **kwargs):
        raise failure()

    monkeypatch.setattr(OIDCVerifier, "_prefetch", interrupted)
    load_entrypoint("provisioned-concurrency")
    init = records(capfd)[-1]
    assert init["status"] == "ready"
    assert init["oidc_prefetch"] == {"status": "failed", "failure_kind": "structural"}

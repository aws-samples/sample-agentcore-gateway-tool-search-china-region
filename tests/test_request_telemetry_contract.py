"""Init and request OIDC attempts have separate, closed telemetry schemas."""

import json

import httpx
import pytest
from test_oidc_attempt_events import EVENT_FIELDS, split_init_rows, split_request_rows
from test_oidc_reliability import (
    fixture,  # noqa: F401
    signing_key,  # noqa: F401
)
from test_request_initialization import load_entrypoint  # noqa: F401

from semantic_service import runtime


def test_attempt_schemas_distinguish_request_and_init():
    assert isinstance(EVENT_FIELDS, dict)
    assert set(EVENT_FIELDS) == {"request", "init"}
    assert EVENT_FIELDS["init"] == EVENT_FIELDS["request"] | {"phase"}


@pytest.mark.parametrize("unknown_field", ["phase", "private_field"])
def test_request_schema_remains_closed(unknown_field):
    assert isinstance(EVENT_FIELDS, dict)
    assert unknown_field not in EVENT_FIELDS["request"]


@pytest.mark.parametrize("recovered", [False, True])
def test_shared_schema_oracle_covers_real_init_then_request_logs(
    monkeypatch,
    capfd,
    load_entrypoint,  # noqa: F811
    fixture,  # noqa: F811
    recovered,
):
    if recovered:
        fixture.steps["jwks"] = [httpx.ReadTimeout("private-schema-marker")]

    def handler(event, context):
        fixture.verifier.verify(fixture.token())
        return {"accepted": True}

    handler.verifier = fixture.verifier
    monkeypatch.setattr(runtime, "_request", lambda: handler)
    entry = load_entrypoint("provisioned-concurrency")
    raw = capfd.readouterr().out
    assert "private-schema-marker" not in raw
    rows = [json.loads(line) for line in raw.splitlines()]
    events, summary = split_init_rows(rows)
    assert len(events) == (2 if recovered else 1)
    assert summary["status"] == summary["oidc_prefetch"]["status"] == "ready"
    assert summary["oidc_prefetch"].get("auth_recovered", False) is recovered
    assert all(event["aws_request_id"] is None for event in events)
    for mutation in ("missing_phase", "request_id", "extra_field"):
        changed = [dict(row) for row in rows]
        if mutation == "missing_phase":
            changed[0].pop("phase")
        elif mutation == "request_id":
            changed[0]["aws_request_id"] = "01234567-89ab-cdef-0123-456789abcdef"
        else:
            changed[0]["unknown"] = "must be rejected"
        with pytest.raises(AssertionError):
            split_init_rows(changed)
    # Expire the prefetched cache, so the request oracle sees real attempt rows.
    fixture.verifier._expires = -float("inf")
    assert entry.request_handler({}, object()) == {"accepted": True}
    rows = [json.loads(line) for line in capfd.readouterr().out.splitlines()]
    events, summary = split_request_rows(rows, None)
    assert len(events) == 1
    assert "phase" not in events[0]
    assert "auth_recovered" not in summary
    assert summary["first_invocation"] is True

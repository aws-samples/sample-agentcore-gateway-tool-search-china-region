"""期限边界失败仍保留一次安全诊断，不改变正常 provider 预算。"""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest
from service.test_identity import BUSINESS, context, event
from test_embedding_budget import budget_service, read_timeout

from semantic_service import search


@pytest.mark.parametrize("seconds", [10**400, 2**1024], ids=["decimal_huge", "binary_huge"])
def test_huge_direct_request_budget_uses_the_validation_error_contract(monkeypatch, seconds):
    with pytest.raises(ValueError, match="invalid search deadline"):
        with budget_service(monkeypatch, request_seconds=seconds):
            pytest.fail("invalid budget reached request handling")


@pytest.mark.parametrize(
    ("value", "error"),
    [("private-budget-marker", TypeError), (None, TypeError), (10**400, OverflowError)],
    ids=["text", "none", "huge_integer"],
)
@pytest.mark.parametrize("callback_fails", [False, True])
def test_deadline_evaluation_failure_emits_one_diagnostic_and_preserves_error(
    monkeypatch, value, error, callback_fails
):
    with budget_service(monkeypatch) as service:
        service.handler.request_seconds = value
        observed = []

        def collect(diagnostic):
            observed.append(diagnostic)
            if callback_fails:
                raise RuntimeError("private-diagnostic-marker")

        with pytest.raises(error):
            service.handler(event(), context(), diagnostics=collect)

        assert len(observed) == 1
        assert observed[0].configured_fusion == service.handler.config.fusion_strategy
        assert observed[0].lambda_deadline_ignored is False
        assert observed[0].vector_availability == "not_attempted"
        assert observed[0].embedding_timing is None
        assert service.requests == []
        assert "private-" not in json.dumps(asdict(observed[0]))


@pytest.mark.parametrize("error", [RuntimeError, TypeError])
def test_static_deadline_inspection_failure_keeps_budget_and_sets_safe_flag(monkeypatch, error):
    def failed_inspection(*args, **kwargs):
        raise error("private-static-inspection-marker")

    monkeypatch.setattr(search, "getattr_static", failed_inspection)
    with budget_service(monkeypatch, pre_seconds=1.0) as service:
        observed = []
        assert service.handler(event(), context(), diagnostics=observed.append) == {
            "tools": [BUSINESS.descriptor]
        }
        assert len(observed) == 1
        assert observed[0].lambda_deadline_ignored is True
        assert read_timeout(service.requests[0]) == pytest.approx(0.45)
        assert service.authorizer.calls == 5
        assert "private-" not in json.dumps(asdict(observed[0]))

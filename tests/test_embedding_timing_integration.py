"""Transport measurements remain private, per-request Search diagnostics."""

from __future__ import annotations

import time

import pytest
from service.test_identity import BUSINESS, SEARCH, codec, context
from test_embedding_error_diagnostics import log_record, private_event, service, success

from semantic_core import _embedding_telemetry
from semantic_service import runtime
from semantic_service.auth import ServiceError


def test_search_logs_embedding_budget_and_transport_without_changing_wire(monkeypatch, capsys):
    with service(lambda _: success()) as (search, requests):
        monkeypatch.setattr(runtime, "_search", lambda: search)
        assert runtime.search_handler(private_event(), context()) == {
            "tools": [BUSINESS.descriptor]
        }
    record = log_record(capsys)
    timing = record["embedding_timing"]
    assert timing["configured_timeout_ms"] == 1000
    assert 0 < timing["stage_budget_ms"] <= 1000
    assert timing["search_remaining_ms"] >= timing["stage_budget_ms"]
    assert len(timing["attempts"]) == len(requests) == 1
    assert timing["attempts"][0]["http_status"] == 200
    # MockTransport provides no network events: absent measurements are not zero.
    assert timing["attempts"][0]["tcp_ms"] is None
    assert timing["attempts"][0]["tls_ms"] is None
    assert timing["attempts"][0]["connection_state"] == "unknown"


def test_embedding_diagnostics_do_not_survive_next_rejected_request(monkeypatch, capsys):
    with service(lambda _: success()) as (search, _):
        monkeypatch.setattr(runtime, "_search", lambda: search)
        runtime.search_handler(private_event(), context())
        assert log_record(capsys)["embedding_timing"] is not None
        with pytest.raises(ServiceError):
            runtime.search_handler({"query": ""}, context())
        assert log_record(capsys)["embedding_timing"] is None


def test_expired_embedding_budget_is_measured_without_a_model_request(monkeypatch, capsys):
    with service(lambda _: success()) as (search, requests):
        ctx = context()
        ctx.get_remaining_time_in_millis = lambda: 200
        monkeypatch.setattr(runtime, "_search", lambda: search)
        assert runtime.search_handler(private_event(), ctx) == {"tools": [BUSINESS.descriptor]}
    record = log_record(capsys)
    assert requests == []
    assert record["embedding_timing"]["stage_budget_ms"] == 0
    assert record["embedding_timing"]["attempts"] == []
    assert record["embedding_error"]["kind"] == "deadline_exceeded"


def test_runtime_factory_uses_the_effective_search_total_budget(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "_configuration",
        lambda _: {
            "SEMSEARCH_SEARCH_TIMEOUT_SECONDS": "2.8",
            "MODEL_EMBEDDING_TIMEOUT_SECONDS": "1.0",
        },
    )
    monkeypatch.setattr(runtime, "_models", lambda _: None)
    monkeypatch.setattr(runtime, "_search_tool", lambda: SEARCH)
    monkeypatch.setattr(runtime, "_envelope", lambda _: codec())
    monkeypatch.setattr(runtime, "_store", object)
    runtime._search.cache_clear()
    try:
        handler = runtime._search()
        assert handler.request_seconds == 2.8
        assert handler.config.embedding_timeout == 1
    finally:
        runtime._search.cache_clear()


@pytest.mark.parametrize("failure_point", ["__init__", "snapshot"])
@pytest.mark.parametrize("status", [200, 503])
def test_diagnostic_scope_failure_preserves_the_model_outcome(
    monkeypatch, capsys, failure_point, status
):
    import httpx

    def failed(*args, **kwargs):
        raise RuntimeError("private-exception-marker")

    monkeypatch.setattr(_embedding_telemetry.EmbeddingTrace, failure_point, failed)
    with service(lambda _: success() if status == 200 else httpx.Response(status)) as (
        search,
        requests,
    ):
        monkeypatch.setattr(runtime, "_search", lambda: search)
        assert runtime.search_handler(private_event(), context()) == {
            "tools": [BUSINESS.descriptor]
        }
    record = log_record(capsys)
    assert len(requests) == 1
    assert record["degraded"] is (status != 200)
    if status == 200:
        assert record["embedding_error"] is None
    else:
        assert record["embedding_error"]["kind"] == "http_server_error"
        assert record["embedding_error"]["http_status"] == 503
    assert record["embedding_timing"]["diagnostics_failed"] is True


def test_failed_nested_collector_does_not_charge_the_parent_request(monkeypatch):
    deadline = time.monotonic() + 5
    with _embedding_telemetry.collect_embedding(deadline, deadline, 0.45) as parent:

        def failed(*args, **kwargs):
            raise RuntimeError("private-exception-marker")

        monkeypatch.setattr(_embedding_telemetry.EmbeddingTrace, "__init__", failed)
        with _embedding_telemetry.collect_embedding(deadline, deadline, 0.45) as child:
            assert _embedding_telemetry.start_http_attempt(0.45) is None
            assert _embedding_telemetry.snapshot_embedding(child).diagnostics_failed
        assert _embedding_telemetry.start_http_attempt(0.45) is not None
    assert len(_embedding_telemetry.snapshot_embedding(parent).attempts) == 1

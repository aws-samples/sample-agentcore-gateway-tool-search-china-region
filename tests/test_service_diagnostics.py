"""Request-local diagnostics must not change the Gateway business object."""

import json
from dataclasses import replace
from functools import lru_cache

import pytest
from service.test_identity import (
    BUSINESS,
    SEARCH,
    Authorizer,
    Reader,
    codec,
    context,
    event,
    view,
)

from semantic_service import runtime
from semantic_service.auth import AuthenticationError, IndexNotReadyError, InputError, ServiceError
from semantic_service.search import SearchHandler


def handler(**kwargs):
    return SearchHandler(SEARCH, codec(), Authorizer(), Reader(), **kwargs)


def test_direct_handler_uses_service_defaults_and_preserves_observer():
    observations, diagnostics = [], []
    search = handler(observer=lambda mode, abstained: observations.append((mode, abstained)))
    assert search.config.fusion_strategy == "vector_bm25_fallback"
    assert search.config.min_vector_similarity is search.config.min_vector_margin is None
    assert search.config.rerank_enabled is False
    assert search(event(), context(), diagnostics=diagnostics.append) == {
        "tools": [BUSINESS.descriptor]
    }
    assert observations == [("bm25", False)]
    assert len(diagnostics) == 1
    assert diagnostics[0].ranking_mode == "bm25"
    assert diagnostics[0].configured_fusion == "vector_bm25_fallback"
    assert diagnostics[0].vector_availability == "disabled"
    assert diagnostics[0].degraded is diagnostics[0].empty_result is False


@pytest.mark.parametrize("failure", ["input", "authentication", "index", "authorization"])
def test_pre_retrieval_failures_emit_null_ranking_without_raw_details(failure):
    search = handler()
    arguments, ctx = event(), context()
    expected = ServiceError
    if failure == "input":
        arguments = {"query": ""}
        expected = InputError
    elif failure == "authentication":
        ctx = context(bedrockAgentCoreMcpMessageId="wrong")
        expected = AuthenticationError
    elif failure == "index":
        search.snapshots = type(
            "Broken", (), {"current": lambda *_: (_ for _ in ()).throw(ValueError("private"))}
        )()
        expected = IndexNotReadyError
    else:
        search.authorizer = Authorizer([ServiceError()])
    records = []
    with pytest.raises(expected):
        search(arguments, ctx, diagnostics=records.append)
    assert len(records) == 1
    assert records[0].ranking_mode is None
    assert records[0].empty_result is records[0].below_threshold is False
    assert records[0].vector_availability == "not_attempted"


@pytest.mark.parametrize("reason", ["acl", "size"])
def test_final_response_empty_is_measured_after_service_filtering(reason, monkeypatch):
    search = handler()
    if reason == "acl":
        search.authorizer = Authorizer([view()] * 4 + [view([SEARCH.tool_id])])
    else:
        monkeypatch.setattr("semantic_service.search.RESPONSE_LIMIT", 1)
    records = []
    assert search(event(), context(), diagnostics=records.append) == {"tools": []}
    assert records[0].ranking_mode == "bm25"
    assert records[0].empty_result is True
    assert records[0].below_threshold is records[0].degraded is False


def test_cached_handler_diagnostics_do_not_leak_between_invocations(monkeypatch, capsys):
    search = handler()
    monkeypatch.setattr(runtime, "_search", lru_cache(maxsize=1)(lambda: search))
    assert runtime.search_handler(event(), context()) == {"tools": [BUSINESS.descriptor]}
    with pytest.raises(InputError):
        runtime.search_handler({"query": ""}, context())
    rows = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    rows = [row for row in rows if row["operation"] == "search"]
    assert len(rows) == 2
    assert rows[0]["ranking_mode"] == "bm25"
    assert rows[1]["ranking_mode"] is None
    assert rows[1]["vector_availability"] == "not_attempted"
    assert rows[1]["empty_result"] is False
    assert all(row["configured_fusion"] == "vector_bm25_fallback" for row in rows)
    assert all(row["rerank_state"] == "disabled" for row in rows)
    assert all("query" not in row and "tools" not in row for row in rows)


def test_initialization_failure_still_has_search_telemetry(monkeypatch, capsys):
    def broken():
        raise ValueError("private provider credential")

    monkeypatch.setattr(runtime, "_search", broken)
    with pytest.raises(ServiceError, match="internal failure"):
        runtime.search_handler(event(), context())
    raw = capsys.readouterr().out
    row = json.loads(raw)
    assert row["ranking_mode"] is None
    assert row["configured_fusion"] is None
    assert row["vector_availability"] == "not_attempted"
    assert row["empty_result"] is row["below_threshold"] is row["degraded"] is False
    assert "private" not in raw and "credential" not in raw


def test_diagnostics_callback_failure_does_not_change_business_response():
    def broken(_):
        raise ValueError("private observer data")

    assert handler()(event(), context(), diagnostics=broken) == {"tools": [BUSINESS.descriptor]}


def test_egress_disabled_is_intentional_not_provider_failure():
    search = handler()
    search.authorizer = Authorizer([replace(view(), query_egress_allowed=False)])
    search.embedding = object()
    records = []
    assert search(event(), context(), diagnostics=records.append) == {
        "tools": [BUSINESS.descriptor]
    }
    assert records[0].vector_availability == "disabled"
    assert records[0].degraded is False


def test_empty_core_result_followed_by_authorization_failure_is_not_empty_response():
    search = handler()
    search.authorizer = Authorizer([view([SEARCH.tool_id]), view([SEARCH.tool_id]), ServiceError()])
    records = []
    with pytest.raises(ServiceError):
        search(event(), context(), diagnostics=records.append)
    assert len(records) == 1
    assert records[0].empty_result is False
    assert records[0].ranking_mode is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("SEMSEARCH_FUSION_STRATEGY", "invalid"),
        ("SEMSEARCH_MIN_VECTOR_SIMILARITY", "nan"),
        ("SEMSEARCH_MIN_VECTOR_MARGIN", "-1"),
        ("MODEL_RERANK_ENABLED", "yes"),
    ],
)
def test_runtime_validates_config_before_disabled_models_or_clients(key, value, monkeypatch):
    monkeypatch.setattr(
        runtime,
        "_configuration",
        lambda _: {"SEMSEARCH_MODELS_ENABLED": "false", key: value},
    )
    monkeypatch.setattr(runtime, "_models", lambda *_: pytest.fail("validation must run first"))
    runtime._search.cache_clear()
    try:
        with pytest.raises(ValueError, match=key):
            runtime._search()
    finally:
        runtime._search.cache_clear()


@pytest.mark.parametrize("entry", ["models", "enhancer"])
def test_disabled_model_paths_reject_invalid_rerank_boolean_without_clients(
    entry, monkeypatch, capsys
):
    monkeypatch.setattr(
        runtime,
        "_configuration",
        lambda _: {"SEMSEARCH_MODELS_ENABLED": "false", "MODEL_RERANK_ENABLED": "yes"},
    )
    monkeypatch.setattr(runtime, "_aws", lambda *_: pytest.fail("must not build clients"))
    monkeypatch.setattr(runtime, "_http", lambda: pytest.fail("must not build clients"))
    runtime._models.cache_clear()
    try:
        if entry == "models":
            with pytest.raises(ValueError, match="MODEL_RERANK_ENABLED"):
                runtime._models()
        else:
            with pytest.raises(ServiceError, match="internal failure"):
                runtime.enhancer_handler({}, object())
            assert json.loads(capsys.readouterr().out)["status"] == "internal failure"
    finally:
        runtime._models.cache_clear()

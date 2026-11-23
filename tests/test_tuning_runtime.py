"""Budget, cache and holdout guards must fail before external work."""

import json
import time
from pathlib import Path

import httpx
import numpy as np
import pytest

from semantic_core._http import HTTPConfig, _HTTPProvider
from semantic_core.providers import (
    MockEmbeddingProvider,
    ModelProfile,
    ProviderError,
    RequestContext,
)
from semantic_eval.tuning_runtime import (
    DocumentCache,
    Ledger,
    RunStopped,
    claim_test,
    file_sha256,
    save_json,
)


def test_budget_counts_started_requests_across_processes(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, caps={"embedding": 1, "rerank": 1, "chat": 1})
    ledger.start("embedding", 10)
    resumed = Ledger(tmp_path, caps={"embedding": 1, "rerank": 1, "chat": 1})
    with pytest.raises(RunStopped, match="budget"):
        resumed.preflight({"embedding": 1})
    with pytest.raises(RunStopped, match="budget"):
        resumed.start("embedding", 1)
    assert resumed.counts() == {"embedding": 1, "rerank": 0, "chat": 0}


def test_missing_usage_is_unknown_and_only_numeric_counters_persist(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    row = ledger.start("rerank", 3)
    ledger.finish(row, {"usage": {"total_tokens": 7, "private": "secret"}}, 1.2)
    ledger.start("chat", 20)
    summary = ledger.summary()
    assert summary["unknown_cost_requests"] == 1
    assert summary["reported_usage"]["rerank"]["total_tokens"] == 7
    assert "secret" not in (tmp_path / "usage-ledger.jsonl").read_text()


def test_document_cache_reuses_vectors_but_never_persists_queries(tmp_path: Path) -> None:
    provider = MockEmbeddingProvider()
    cache = DocumentCache(provider, tmp_path)
    context = RequestContext("cache-test", time.monotonic() + 10)
    first = cache.embed(["tool summary"], "document", context)
    assert len(list(tmp_path.glob("*.npy"))) == 1
    second = DocumentCache(provider, tmp_path).embed(["tool summary"], "document", context)
    assert (first.vectors == second.vectors).all()
    assert second.usage == {}
    cache.embed(["query must stay in memory"], "query", context)
    assert len(list(tmp_path.glob("*.npy"))) == 1


def test_holdout_claim_is_single_use_and_binds_frozen_recommendation(tmp_path: Path) -> None:
    frozen = tmp_path / "recommendation.json"
    manifest = tmp_path / "split-manifest.json"
    save_json(manifest, {"source": "fixture"})
    finished = "2026-01-01T00:00:00Z"
    save_json(
        frozen,
        {
            "fusion_strategy": "vector",
            "dev_finished_at": finished,
            "split_manifest_sha256": file_sha256(manifest),
        },
    )
    save_json(
        tmp_path / "freeze-boundary.json",
        {
            "recommendation_sha256": file_sha256(frozen),
            "split_manifest_sha256": file_sha256(manifest),
            "dev_finished_at": finished,
            "external_time_attestation": False,
        },
    )
    claim_test(tmp_path, frozen, freeze_evidence_ref="fixture-capture-reference")
    with pytest.raises(RunStopped, match="already"):
        claim_test(tmp_path, frozen)
    claim = json.loads((tmp_path / "test-claim.json").read_text())
    assert claim["recommendation_sha256"] == file_sha256(frozen)
    assert claim["freeze_evidence_ref"] == "fixture-capture-reference"
    assert claim["external_reference_verified"] is False


def test_stale_ledger_cannot_exceed_persisted_cap_or_erase_later_attempt(tmp_path: Path) -> None:
    caps = {"embedding": 1, "rerank": 1, "chat": 1}
    first, stale = Ledger(tmp_path, caps=caps), Ledger(tmp_path, caps=caps)
    row = first.start("embedding", 1)
    with pytest.raises(RunStopped, match="budget"):
        stale.start("embedding", 1)
    stale.start("chat", 1)
    first.finish(row, {"usage": {"total_tokens": 10}}, 1.0)
    assert Ledger(tmp_path, caps=caps).counts() == {"embedding": 1, "rerank": 0, "chat": 1}


@pytest.mark.parametrize("required", [{"embedding": -1}, {"embedding": True}, {"unknown": 1}])
def test_invalid_preflight_never_records_an_approved_budget(tmp_path: Path, required) -> None:
    with pytest.raises(ValueError, match="budget"):
        Ledger(tmp_path).preflight(required)
    assert not list(tmp_path.glob("preflight-*.json"))


def test_unrecognized_or_above_authorized_caps_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="budget"):
        Ledger(tmp_path, caps={"embedding": 1501, "rerank": 1000, "chat": 500})


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": 10, "input_tokens": 11, "completion_tokens": 2},
        {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 13},
        {"prompt_tokens": 10, "completion_tokens": -1, "output_tokens": 2},
        {"prompt_tokens": 10, "completion_tokens": True, "output_tokens": 2},
        {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 11},
        },
    ],
)
def test_inconsistent_or_invalid_usage_is_unknown_cost(tmp_path: Path, usage) -> None:
    ledger = Ledger(tmp_path)
    row = ledger.start("chat", 1)
    ledger.finish(row, {"usage": usage}, 1.0)
    summary = ledger.summary()
    assert summary["unknown_cost_requests"] == 1
    assert summary["public_list_price_cny"] is None
    assert row["usage_status"] in {"invalid", "inconsistent"}


def test_unknown_requests_do_not_turn_partial_subtotal_into_total(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.finish(ledger.start("rerank", 3), {"usage": {"input_tokens": 1_000_000}}, 1.0)
    ledger.start("chat", 1)
    summary = ledger.summary()
    assert summary["known_public_list_price_cny"] == 0.5
    assert summary["public_list_price_cny"] is None
    assert summary["unknown_cost_requests"] == 1


def test_zero_reported_usage_is_distinct_from_missing_usage(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.finish(
        ledger.start("chat", 1),
        {"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
        1.0,
    )
    assert ledger.summary()["public_list_price_cny"] == 0.0
    assert ledger.summary()["unknown_cost_requests"] == 0


def test_document_cache_keys_include_full_profile_and_reject_invalid_input(
    tmp_path: Path,
) -> None:
    context = RequestContext("cache-test", time.monotonic() + 10)
    cache = DocumentCache(MockEmbeddingProvider(16), tmp_path)
    cache.embed(["same document"], "document", context)
    other = DocumentCache(MockEmbeddingProvider(32), tmp_path)
    result = other.embed(["same document"], "document", context)
    assert result.vectors.shape == (1, 32)
    assert len(list(tmp_path.glob("*.npy"))) == 2
    with pytest.raises(ProviderError):
        cache.embed([], "document", context)


def test_http_error_attempt_is_persisted_and_budget_stops_next_transport_call(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path, caps={"embedding": 1, "rerank": 1, "chat": 1})
    sent: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(503, json={"error": "not retained"})

    with httpx.Client(
        transport=httpx.MockTransport(transport),
        event_hooks={"request": [ledger.request], "response": [ledger.response]},
    ) as client:
        provider = _HTTPProvider(
            client,
            HTTPConfig("https://fixture.invalid", ("fixture.invalid",)),
            ModelProfile("text-embedding-v4", "fixture", 2),
        )
        context = RequestContext("budget-test", time.monotonic() + 10)
        with ledger.observe():
            with pytest.raises(ProviderError):
                provider._post("/embeddings", {"input": ["synthetic"]}, context)
            with pytest.raises(RunStopped, match="budget"):
                provider._post("/embeddings", {"input": ["synthetic"]}, context)
    assert len(sent) == 1
    resumed = Ledger(tmp_path)
    assert resumed.counts()["embedding"] == 1
    assert resumed.rows[0]["status"] == 503
    assert resumed.rows[0]["transport_error"] == "unavailable"
    assert resumed.summary()["unknown_cost_requests"] == 1
    assert "not retained" not in resumed.path.read_text()


def test_corrupt_cache_vectors_are_rejected_without_query_or_document_reuse(
    tmp_path: Path,
) -> None:
    context = RequestContext("cache-test", time.monotonic() + 10)
    cache = DocumentCache(MockEmbeddingProvider(), tmp_path)
    cache.embed(["synthetic document"], "document", context)
    np.save(next(tmp_path.glob("*.npy")), np.array([float("nan")] * 16), allow_pickle=False)
    with pytest.raises(ProviderError):
        cache.embed(["synthetic document"], "document", context)

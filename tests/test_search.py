"""Independent security and degradation regressions for the core pipeline."""

import time
from dataclasses import replace

import numpy as np
import pytest

from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord
from semantic_core.providers import (
    EmbeddingResult,
    ErrorCode,
    ModelProfile,
    ProviderError,
    RequestContext,
    RerankItem,
    RerankResult,
)
from semantic_core.search import (
    AuthorizationError,
    SearchConfig,
    SearchEngine,
    reciprocal_rank_fusion,
)
from semantic_core.snapshot import IndexSnapshot
from semantic_core.vectors import VectorIndex

GATEWAY = GatewayIdentity("000000000000", "cn-north-1", "fixture")
PROFILE = ModelProfile("fixture-embedding", "1", 2)


def tool(name, description, incarnation="1"):
    identity = ToolIdentity(GATEWAY, "fixture-target", incarnation, name)
    return ToolRecord.from_descriptor(
        identity,
        {
            "name": name,
            "description": description,
            "inputSchema": {"type": "object", "properties": {}},
        },
    )


class Embedding:
    def __init__(self, failure=None, vectors=None, profile=PROFILE):
        self.failure = failure
        self.vectors = np.array([[1, 0]], dtype=np.float32) if vectors is None else vectors
        self.profile = profile
        self.calls = []

    def describe(self):
        return self.profile

    def embed(self, texts, purpose, context):
        self.calls.append((tuple(texts), purpose, context))
        if self.failure:
            raise self.failure
        return EmbeddingResult(self.vectors, self.profile)


class Reranker:
    def __init__(self, failure=None, invalid=None):
        self.failure = failure
        self.invalid = invalid
        self.calls = []

    def describe(self):
        return ModelProfile("fixture-rerank", "1")

    def rerank(self, query, documents, top_n, context):
        self.calls.append((query, tuple(documents), top_n, context))
        if self.failure:
            raise self.failure
        items = tuple(RerankItem(document.id, float(i)) for i, document in enumerate(documents))
        if self.invalid is not None:
            items = self.invalid
        return RerankResult(items, self.describe())


def ctx():
    return RequestContext("offline-fixture", time.monotonic() + 10)


def fixture():
    tools = (tool("a", "cancel order"), tool("b", "cancel"), tool("c", "inventory"))
    vectors = VectorIndex([[0, 1], [0.1, 1], [1, 0]], [0, 1, 2], 3, 2)
    snapshot = IndexSnapshot.build(GATEWAY, tools, vectors=vectors, model_profile=PROFILE)
    return snapshot, frozenset(t.tool_id for t in tools)


def names(result):
    return [hit.tool.name for hit in result.hits]


def test_acl_mask_prevents_candidate_starvation_and_provider_egress_leak():
    hidden = [tool(f"hidden{i:03}", "cancel cancel cancel") for i in range(60)]
    allowed_tools = [tool("visible_a", "cancel"), tool("visible_b", "inventory")]
    tools = tuple(hidden + allowed_tools)
    # Many perfect unauthorized vectors must not occupy the 50 unique candidates.
    rows = [[1, 0]] * (len(hidden) * 7) + [[0.1, 1], [0, 1]]
    parents = [i for i in range(len(hidden)) for _ in range(7)] + [60, 61]
    snapshot = IndexSnapshot.build(
        GATEWAY, tools, vectors=VectorIndex(rows, parents, len(tools), 2), model_profile=PROFILE
    )
    reranker = Reranker()
    engine = SearchEngine(snapshot, Embedding(), reranker, SearchConfig(rerank_enabled=True))
    allowed = frozenset(t.tool_id for t in allowed_tools)
    result = engine.search("cancel", allowed, ctx())
    assert set(names(result)) == {"visible_a", "visible_b"}
    assert reranker.calls
    sent = reranker.calls[0][1]
    assert {d.id for d in sent} == allowed
    assert all("hidden" not in d.text for d in sent)
    assert all(hit.tool.tool_id in allowed for hit in result.hits)


def test_empty_authorization_never_calls_providers():
    snapshot, _ = fixture()
    embedding, reranker = Embedding(), Reranker()
    assert (
        SearchEngine(snapshot, embedding, reranker).search("cancel", frozenset(), ctx()).hits == ()
    )
    assert embedding.calls == reranker.calls == []


def test_rerank_off_keeps_vector_contribution_in_rrf():
    snapshot, allowed = fixture()
    reranker = Reranker()
    hybrid = SearchEngine(snapshot, Embedding(), reranker).search("cancel order", allowed, ctx())
    lexical = SearchEngine(snapshot).search("cancel order", allowed, ctx())
    assert hybrid.mode == "rrf"
    assert lexical.mode == "bm25"
    assert names(hybrid) != names(lexical)
    assert reranker.calls == []


@pytest.mark.parametrize("code", list(ErrorCode))
def test_rerank_failure_keeps_exact_rrf_order_and_scores(code):
    snapshot, allowed = fixture()
    base = SearchEngine(snapshot, Embedding()).search("cancel order", allowed, ctx())
    failed = SearchEngine(
        snapshot, Embedding(), Reranker(ProviderError(code)), SearchConfig(rerank_enabled=True)
    ).search("cancel order", allowed, ctx())
    assert failed == base


@pytest.mark.parametrize(
    "failure", [ProviderError(ErrorCode.UNAVAILABLE), RuntimeError("synthetic")]
)
def test_embedding_failure_is_only_model_failure_that_falls_back_to_bm25(failure):
    snapshot, allowed = fixture()
    reranker = Reranker()
    result = SearchEngine(
        snapshot, Embedding(failure), reranker, SearchConfig(rerank_enabled=True)
    ).search("cancel order", allowed, ctx())
    expected = SearchEngine(snapshot).search("cancel order", allowed, ctx())
    assert result == expected
    assert reranker.calls == []


@pytest.mark.parametrize(
    "vector", [np.array([[0, 0]]), np.array([[np.nan, 1]]), np.array([[1, 2, 3]])]
)
def test_invalid_query_vectors_are_rejected_and_use_bm25(vector):
    snapshot, allowed = fixture()
    result = SearchEngine(snapshot, Embedding(vectors=vector)).search(
        "cancel order", allowed, ctx()
    )
    assert result.mode == "bm25"


def test_model_revision_mismatch_never_combines_vector_spaces():
    snapshot, allowed = fixture()
    embedding = Embedding(profile=replace(PROFILE, revision="different"))
    result = SearchEngine(snapshot, embedding).search("cancel", allowed, ctx())
    assert result.mode == "bm25"
    assert embedding.calls == []


def test_rerank_over_budget_preserves_rrf_without_truncating_query():
    snapshot, allowed = fixture()
    query = "取消" * 100
    reranker = Reranker()
    config = SearchConfig(rerank_enabled=True)
    result = SearchEngine(snapshot, Embedding(), reranker, config).search(query, allowed, ctx())
    expected = SearchEngine(snapshot, Embedding()).search(query, allowed, ctx())
    assert result == expected
    assert reranker.calls == []


def test_rerank_invalid_candidate_injection_keeps_rrf():
    snapshot, allowed = fixture()
    invalid = (RerankItem("not-authorized", 999.0),)
    result = SearchEngine(
        snapshot, Embedding(), Reranker(invalid=invalid), SearchConfig(rerank_enabled=True)
    ).search("cancel", allowed, ctx())
    assert result == SearchEngine(snapshot, Embedding()).search("cancel", allowed, ctx())


def test_rerank_finite_scores_have_no_absolute_rejection_threshold():
    snapshot, allowed = fixture()
    base = SearchEngine(snapshot, Embedding()).search("cancel", allowed, ctx())
    ties = tuple(RerankItem(hit.tool.tool_id, -10000.0) for hit in reversed(base.hits))
    result = SearchEngine(
        snapshot, Embedding(), Reranker(invalid=ties), SearchConfig(rerank_enabled=True)
    ).search("cancel", allowed, ctx())
    assert result.mode == "rerank"
    assert names(result) == names(base)


def test_parent_dedup_happens_before_candidate_limit():
    tools = tuple(tool(f"tool{i}", "cancel") for i in range(4))
    vectors = VectorIndex([[1, 0]] * 7 + [[0.9, 1], [0.8, 1], [0.7, 1]], [0] * 7 + [1, 2, 3], 4, 2)
    snapshot = IndexSnapshot.build(GATEWAY, tools, vectors=vectors, model_profile=PROFILE)
    result = SearchEngine(snapshot, Embedding(), config=SearchConfig(recall_k=3)).search(
        "cancel", frozenset(t.tool_id for t in tools), ctx()
    )
    assert len(result.hits) == 3
    assert len({h.tool.tool_id for h in result.hits}) == 3


def test_rrf_math_missing_channel_and_name_tie_break():
    ranking = reciprocal_rank_fusion([["b", "a"], ["a", "c"]], {"a": "a", "b": "b", "c": "c"})
    assert ranking[0] == ("a", pytest.approx(1 / 62 + 1 / 61))
    assert ranking[1][0] == "b"
    assert ranking[2] == ("c", pytest.approx(1 / 62))
    assert reciprocal_rank_fusion([["b"], ["a"]], {"a": "a", "b": "b"})[0][0] == "a"


def test_reauthorization_before_egress_and_before_return():
    snapshot, allowed = fixture()
    visible = snapshot.tools[0].tool_id
    calls = iter([frozenset({visible}), frozenset()])
    reranker = Reranker()
    result = SearchEngine(
        snapshot, Embedding(), reranker, SearchConfig(rerank_enabled=True)
    ).search("cancel", allowed, ctx(), reauthorize=lambda: next(calls))
    assert [doc.id for doc in reranker.calls[0][1]] == [visible]
    assert result.hits == ()


def test_authorization_failure_is_not_model_degradation():
    snapshot, allowed = fixture()

    def unavailable():
        raise RuntimeError("policy unavailable")

    with pytest.raises(AuthorizationError):
        SearchEngine(snapshot, Embedding()).search(
            "cancel", allowed, ctx(), reauthorize=unavailable
        )


@pytest.mark.parametrize("query", ["", "   ", "x" * 2049])
def test_query_validation(query):
    snapshot, allowed = fixture()
    with pytest.raises(ValueError):
        SearchEngine(snapshot).search(query, allowed, ctx())


def test_empty_vector_index_is_bm25_without_embedding_call():
    snapshot, allowed = fixture()
    snapshot = replace(snapshot, vectors=VectorIndex([], [], len(snapshot.tools), 2))
    embedding = Embedding()
    result = SearchEngine(snapshot, embedding).search("cancel order", allowed, ctx())
    assert result.mode == "bm25"
    assert embedding.calls == []


def test_successful_rerank_reorders_only_authorized_candidates():
    snapshot, allowed = fixture()
    base = SearchEngine(snapshot, Embedding()).search("cancel", allowed, ctx())
    reverse = tuple(
        RerankItem(hit.tool.tool_id, float(10 - i)) for i, hit in enumerate(reversed(base.hits))
    )
    result = SearchEngine(
        snapshot, Embedding(), Reranker(invalid=reverse), SearchConfig(rerank_enabled=True)
    ).search("cancel", allowed, ctx())
    assert result.mode == "rerank"
    assert names(result) == list(reversed(names(base)))


@pytest.mark.parametrize(
    "invalid",
    [
        (RerankItem("duplicate", 1), RerankItem("duplicate", 1)),
        (RerankItem("unexpected", float("nan")),),
    ],
)
def test_bad_rerank_response_preserves_exact_rrf(invalid):
    snapshot, allowed = fixture()
    base = SearchEngine(snapshot, Embedding()).search("cancel", allowed, ctx())
    result = SearchEngine(
        snapshot, Embedding(), Reranker(invalid=invalid), SearchConfig(rerank_enabled=True)
    ).search("cancel", allowed, ctx())
    assert result == base


@pytest.mark.parametrize("allowed", [None, ["tool"], {"tool"}, frozenset({1})])
def test_malformed_authorization_fails_before_provider(allowed):
    snapshot, _ = fixture()
    embedding = Embedding()
    with pytest.raises(AuthorizationError):
        SearchEngine(snapshot, embedding).search("cancel", allowed, ctx())
    assert embedding.calls == []


def test_late_revocation_refills_without_recomputing_catalog_statistics():
    tools = tuple(tool(f"tool{i:03}", "cancel") for i in range(60))
    snapshot = IndexSnapshot.build(GATEWAY, tools)
    before = snapshot.bm25.to_dict()
    remaining = frozenset(t.tool_id for t in tools[50:])
    decisions = iter([frozenset(t.tool_id for t in tools), remaining])
    result = SearchEngine(snapshot).search(
        "cancel", frozenset(t.tool_id for t in tools), ctx(), reauthorize=lambda: next(decisions)
    )
    assert names(result) == [f"tool{i:03}" for i in range(50, 55)]
    assert snapshot.bm25.to_dict() == before


@pytest.mark.parametrize(
    "change",
    [
        {"top_k": 21},
        {"top_k": True},
        {"recall_k": 51},
        {"rerank_limit": 31},
        {"rrf_constant": float("nan")},
        {"embedding_timeout": 0},
        {"rerank_enabled": 1},
    ],
)
def test_invalid_search_configuration_rejected(change):
    with pytest.raises(ValueError):
        SearchConfig(**change)


def test_request_pins_snapshot_before_scoring(monkeypatch):
    original, allowed = fixture()
    replacement = IndexSnapshot.build(GATEWAY, [tool("replacement", "cancel")])
    engine = SearchEngine(original, Embedding())
    original_scores = original.bm25.masked_scores

    def swap_after_score(query, mask):
        scores = original_scores(query, mask)
        engine.snapshot = replacement
        return scores

    monkeypatch.setattr(original.bm25, "masked_scores", swap_after_score)
    result = engine.search("cancel order", allowed, ctx())
    assert result.mode == "rrf"
    assert set(names(result)) == {"a", "b", "c"}

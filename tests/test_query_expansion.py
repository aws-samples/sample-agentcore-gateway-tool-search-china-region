"""查询别名回归：保留索引统计、模型输入和授权边界。"""

import math
import time
from dataclasses import replace

import numpy as np
import pytest

from semantic_core.bm25 import BM25Index
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
from semantic_core.search import SearchConfig, SearchEngine
from semantic_core.snapshot import IndexSnapshot
from semantic_core.tokenization import ChineseTokenizer, TokenizerConfig
from semantic_core.vectors import VectorIndex

GATEWAY = GatewayIdentity("000000000000", "cn-north-1", "query-fixture")
PROFILE = ModelProfile("query-fixture", "1", 2)


def _tool(name, description):
    return ToolRecord.from_descriptor(
        ToolIdentity(GATEWAY, "fixture-target", "1", name),
        {
            "name": name,
            "description": description,
            "inputSchema": {"type": "object", "properties": {}},
        },
    )


def _snapshot(tools, similarities=None, *, legacy=False):
    vectors = None
    if similarities is not None:
        vectors = VectorIndex(
            [[score, math.sqrt(1 - score**2)] for score in similarities],
            list(range(len(tools))),
            len(tools),
            2,
        )
    snapshot = IndexSnapshot.build(
        GATEWAY,
        tools,
        vectors=vectors,
        model_profile=PROFILE if vectors is not None else None,
    )
    if legacy:
        snapshot = replace(
            snapshot,
            bm25=BM25Index.build(
                [tool.fields() for tool in tools], ChineseTokenizer(TokenizerConfig())
            ),
        )
    return snapshot, frozenset(tool.tool_id for tool in tools)


def _inventory(similarities=None, *, legacy=False):
    tools = tuple(
        _tool(name, description)
        for name, description in (
            ("a_read_stock", "read stock 库存"),
            ("b_update_stock", "update stock 库存"),
            ("c_delete_product", "delete product 库存"),
            ("d_read_warehouse", "read warehouse 库存"),
            ("e_create_warehouse", "create warehouse 库存"),
            ("f_create_stock", "create stock 库存"),
            ("g_update_product", "update product 库存"),
            ("z_create_product", "create product 库存"),
        )
    )
    return _snapshot(tools, similarities, legacy=legacy)


def _context():
    return RequestContext("query-expansion-fixture", time.monotonic() + 10)


def _names(result):
    return [hit.tool.name for hit in result.hits]


class _Embedding:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []

    def describe(self):
        return PROFILE

    def embed(self, texts, purpose, context):
        self.calls.append((tuple(texts), purpose, context.request_id))
        if self.failure is not None:
            raise ProviderError(self.failure)
        return EmbeddingResult(np.array([[1, 0]], dtype=np.float32), PROFILE)


class _Reranker:
    def __init__(self):
        self.calls = []

    def describe(self):
        return ModelProfile("query-fixture-rerank", "1")

    def rerank(self, query, documents, top_n, context):
        self.calls.append((query, tuple(documents), context.request_id))
        return RerankResult(
            tuple(
                RerankItem(document.id, float(top_n - index))
                for index, document in enumerate(documents)
            ),
            self.describe(),
        )


@pytest.mark.parametrize("resource", ["invoice", "customer"])
@pytest.mark.parametrize(
    ("query_action", "expected_action"),
    [
        ("新增", "create"),
        ("新建", "create"),
        ("创建", "create"),
        ("添加", "create"),
        ("查看", "read"),
        ("查询", "read"),
        ("读取", "read"),
        ("更新", "update"),
        ("修改", "update"),
        ("编辑", "update"),
        ("删除", "delete"),
        ("移除", "delete"),
    ],
)
def test_crud_aliases_retrieve_the_requested_operation_across_resources(
    resource, query_action, expected_action
):
    tools = tuple(
        _tool(f"service___{action}_{entity}", f"{action} {entity} record")
        for entity in ("invoice", "customer")
        for action in ("create", "read", "update", "delete")
    )
    snapshot, allowed = _snapshot(tools)
    result = SearchEngine(snapshot).search(f"{query_action} {resource}", allowed, _context())
    assert _names(result)[0] == f"service___{expected_action}_{resource}"


@pytest.mark.parametrize(
    ("query", "indexed_term"),
    [("产品", "product"), ("商品", "product"), ("product", "商品")],
)
def test_resource_aliases_work_in_both_language_directions(query, indexed_term):
    snapshot, allowed = _snapshot(
        (_tool("a_unrelated", "warehouse"), _tool("z_relevant", indexed_term))
    )
    result = SearchEngine(snapshot).search(query, allowed, _context())
    assert _names(result)[0] == "z_relevant"


def test_inventory_query_recalls_product_from_a_persisted_versioned_index():
    original, allowed = _inventory()
    persisted = original.bm25.to_dict()
    snapshot = replace(original, bm25=BM25Index.from_dict(persisted))
    original_scores = snapshot.bm25.masked_scores("新增一个库存产品", [True] * 8)

    result = SearchEngine(snapshot).search("新增一个库存产品", allowed, _context())

    assert _names(result)[0] == "z_create_product"
    assert snapshot.bm25.to_dict() == persisted
    np.testing.assert_array_equal(
        snapshot.bm25.masked_scores("新增一个库存产品", [True] * 8), original_scores
    )


@pytest.mark.parametrize(
    ("query", "tools"),
    [
        ("新增", (("a_english", "create create create"), ("z_chinese", "新增"))),
        ("添加", (("a_stock", "stock"), ("z_weather", "weather"))),
        ("unrelated", (("a_stock", "stock"), ("z_weather", "weather"))),
    ],
)
@pytest.mark.parametrize("legacy", [False, True])
def test_known_terms_and_queries_without_indexed_aliases_keep_exact_scores(
    monkeypatch, query, tools, legacy
):
    snapshot, allowed = _snapshot(
        tuple(_tool(name, description) for name, description in tools), legacy=legacy
    )
    original = snapshot.bm25.masked_scores
    expected = original(query, [True] * len(tools))
    observed = []

    def record_scores(text, mask):
        scores = original(text, mask)
        observed.append(scores.copy())
        return scores

    monkeypatch.setattr(snapshot.bm25, "masked_scores", record_scores)
    SearchEngine(snapshot).search(query, allowed, _context())
    assert len(observed) == 1
    np.testing.assert_array_equal(observed[0], expected)


def test_finding_a_tool_does_not_invent_a_read_operation():
    snapshot, allowed = _snapshot(
        (
            _tool("a_create_invoice", "create invoice"),
            _tool("z_read_invoice", "read invoice"),
        )
    )
    engine = SearchEngine(snapshot)
    expected = engine.search("invoice", allowed, _context())
    actual = engine.search("查找 invoice 工具", allowed, _context())
    assert actual == expected
    assert set(snapshot.bm25.tokenizer.tokenize("查找 invoice 工具")).isdisjoint(
        {"read", "查看", "查询", "读取"}
    )


def test_repeated_query_aliases_do_not_multiply_lexical_contributions(monkeypatch):
    snapshot, allowed = _inventory()
    original = snapshot.bm25.masked_scores
    observed = []

    def record_scores(query, mask):
        scores = original(query, mask)
        observed.append(scores.copy())
        return scores

    monkeypatch.setattr(snapshot.bm25, "masked_scores", record_scores)
    engine = SearchEngine(snapshot)
    single = engine.search("新增 产品", allowed, _context())
    repeated = engine.search("新增 新增 产品 产品", allowed, _context())
    assert _names(single)[0] == "z_create_product"
    assert repeated == single
    np.testing.assert_array_equal(observed[0], observed[1])


def test_unauthorized_catalog_terms_cannot_disable_visible_aliases():
    tools = (
        _tool("a_visible_stock", "stock"),
        _tool("y_hidden", "新增"),
        _tool("z_visible_create", "create"),
    )
    snapshot, _ = _snapshot(tools)
    allowed = frozenset({tools[0].tool_id, tools[2].tool_id})
    before = snapshot.bm25.to_dict()
    result = SearchEngine(snapshot).search("新增", allowed, _context())
    assert _names(result) == ["z_visible_create", "a_visible_stock"]
    assert snapshot.bm25.to_dict() == before


def test_unauthorized_alias_matches_cannot_starve_the_authorized_candidate():
    snapshot, all_ids = _inventory()
    allowed = all_ids - {snapshot.tools[-1].tool_id}
    result = SearchEngine(snapshot, config=SearchConfig(top_k=1, recall_k=1)).search(
        "新增一个库存产品", allowed, _context()
    )
    assert len(result.hits) == 1
    assert result.hits[0].tool.tool_id in allowed


def test_healthy_vector_order_remains_authoritative_when_aliases_disagree():
    snapshot, allowed = _inventory([0.95, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.8])
    embedding = _Embedding()
    query = "新增一个库存产品"
    diagnostics = []
    result = SearchEngine(
        snapshot, embedding, config=SearchConfig(fusion_strategy="vector_bm25_fallback")
    ).search(query, allowed, _context(), diagnostics=diagnostics.append)
    assert _names(result)[0] == "a_read_stock"
    assert result.mode == "vector_bm25_fallback"
    assert embedding.calls == [((query,), "query", "query-expansion-fixture")]
    assert diagnostics[0].vector_availability == "available"
    assert not diagnostics[0].degraded


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy", "rebuilt"])
def test_healthy_low_similarity_uses_the_snapshot_bm25_baseline(legacy):
    snapshot, allowed = _inventory([0.2] * 8, legacy=legacy)
    diagnostics = []
    result = SearchEngine(
        snapshot, _Embedding(), config=SearchConfig(fusion_strategy="vector_bm25_fallback")
    ).search("新增一个库存产品", allowed, _context(), diagnostics=diagnostics.append)
    assert _names(result) == (
        [
            "a_read_stock",
            "b_update_stock",
            "c_delete_product",
            "d_read_warehouse",
            "e_create_warehouse",
        ]
        if legacy
        else [
            "z_create_product",
            "e_create_warehouse",
            "f_create_stock",
            "c_delete_product",
            "g_update_product",
        ]
    )
    assert result.mode == "bm25"
    assert diagnostics[0].ranking_mode == "bm25"
    assert diagnostics[0].vector_availability == "available"
    assert not diagnostics[0].degraded


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy", "rebuilt"])
def test_healthy_low_similarity_fallback_depends_on_persisted_profile(legacy):
    snapshot, allowed = _snapshot(
        (
            _tool("a_create_order", "create order"),
            _tool("z_read_product", "read product"),
        ),
        [0.30, 0.45],
        legacy=legacy,
    )
    diagnostics = []
    result = SearchEngine(
        snapshot, _Embedding(), config=SearchConfig(fusion_strategy="vector_bm25_fallback")
    ).search("新增产品", allowed, _context(), diagnostics=diagnostics.append)
    assert _names(result) == (
        ["z_read_product", "a_create_order"] if legacy else ["a_create_order", "z_read_product"]
    )
    assert result.mode == ("vector_bm25_fallback" if legacy else "bm25")
    assert diagnostics[0].ranking_mode == result.mode
    assert diagnostics[0].vector_availability == "available"
    assert not diagnostics[0].degraded


@pytest.mark.parametrize("rerank", [False, True])
@pytest.mark.parametrize("legacy", [True, False], ids=["legacy", "rebuilt"])
@pytest.mark.parametrize(
    ("similarity", "fusion", "expected_names", "expected_mode"),
    [
        (
            0.2,
            "rrf",
            [
                "a_read_stock",
                "b_update_stock",
                "c_delete_product",
                "z_create_product",
                "d_read_warehouse",
            ],
            "rrf",
        ),
        (
            0.2,
            "vector",
            [
                "z_create_product",
                "a_read_stock",
                "b_update_stock",
                "c_delete_product",
                "d_read_warehouse",
            ],
            "vector",
        ),
        (
            0.2,
            "vector_bm25_fallback",
            [
                "a_read_stock",
                "b_update_stock",
                "c_delete_product",
                "d_read_warehouse",
                "e_create_warehouse",
            ],
            "bm25",
        ),
        (
            0.95,
            "rrf",
            [
                "a_read_stock",
                "b_update_stock",
                "c_delete_product",
                "d_read_warehouse",
                "z_create_product",
            ],
            "rrf",
        ),
        (
            0.95,
            "vector",
            [
                "a_read_stock",
                "z_create_product",
                "b_update_stock",
                "c_delete_product",
                "d_read_warehouse",
            ],
            "vector",
        ),
        (
            0.95,
            "vector_bm25_fallback",
            [
                "a_read_stock",
                "z_create_product",
                "b_update_stock",
                "c_delete_product",
                "d_read_warehouse",
            ],
            "vector_bm25_fallback",
        ),
    ],
)
def test_healthy_fusion_and_rerank_use_the_snapshot_baseline(
    similarity, fusion, expected_names, expected_mode, rerank, legacy
):
    """旧快照保持原基线；重建后的质量变更及两套排名明确记录在 runbook。"""
    if not legacy and fusion == "rrf":
        expected_names = [
            "z_create_product",
            "a_read_stock",
            "e_create_warehouse",
            "c_delete_product",
            "b_update_stock",
        ]
    elif not legacy and fusion == "vector_bm25_fallback" and similarity == 0.2:
        expected_names = [
            "z_create_product",
            "e_create_warehouse",
            "f_create_stock",
            "c_delete_product",
            "g_update_product",
        ]
    snapshot, allowed = _inventory([similarity] + [0.1] * 6 + [0.4], legacy=legacy)
    diagnostics = []
    result = SearchEngine(
        snapshot,
        _Embedding(),
        _Reranker(),
        SearchConfig(fusion_strategy=fusion, rerank_enabled=rerank),
    ).search("新增一个库存产品", allowed, _context(), diagnostics=diagnostics.append)
    assert _names(result) == expected_names
    assert result.mode == ("rerank" if rerank else expected_mode)
    assert diagnostics[0].ranking_mode == result.mode
    assert diagnostics[0].vector_availability == "available"
    assert not diagnostics[0].degraded


@pytest.mark.parametrize("authorize_note", [False, True])
@pytest.mark.parametrize("note_count", [1, 12])
def test_catalog_growth_cannot_disable_inventory_aliases(authorize_note, note_count):
    original, _ = _inventory()
    notes = tuple(_tool(f"note_{index}", "新增 产品 备注") for index in range(note_count))
    snapshot, allowed = _snapshot((*original.tools, *notes))
    if not authorize_note:
        allowed -= frozenset(tool.tool_id for tool in notes)
    result = SearchEngine(snapshot).search("新增一个库存产品", allowed, _context())
    assert "z_create_product" in _names(result)


@pytest.mark.parametrize("failure", [ErrorCode.UNAVAILABLE, ErrorCode.TIMEOUT])
def test_embedding_failure_retains_alias_recall_and_reports_degradation(failure):
    snapshot, allowed = _inventory([0.2] * 7 + [0.9])
    embedding, reranker = _Embedding(failure), _Reranker()
    diagnostics = []
    query = "新增一个库存产品"
    result = SearchEngine(
        snapshot,
        embedding,
        reranker,
        SearchConfig(fusion_strategy="vector_bm25_fallback", rerank_enabled=True),
    ).search(query, allowed, _context(), diagnostics=diagnostics.append)
    assert _names(result)[0] == "z_create_product"
    assert result.mode == "bm25"
    assert embedding.calls == [((query,), "query", "query-expansion-fixture")]
    assert reranker.calls == []
    assert diagnostics[0].vector_availability == "failed"
    assert diagnostics[0].degraded


@pytest.mark.parametrize("revoke_before_rerank", [True, False])
def test_alias_expansion_preserves_raw_model_queries_and_revocation(revoke_before_rerank):
    snapshot, allowed = _inventory([0.2] * 7 + [0.9])
    revoked = snapshot.tools[-1].tool_id
    reduced = allowed - {revoked}
    authorization = iter([reduced, reduced] if revoke_before_rerank else [allowed, reduced])
    embedding, reranker = _Embedding(), _Reranker()
    query = "新增一个库存产品"
    result = SearchEngine(
        snapshot,
        embedding,
        reranker,
        SearchConfig(fusion_strategy="vector_bm25_fallback", rerank_enabled=True),
    ).search(query, allowed, _context(), reauthorize=lambda: next(authorization))
    assert embedding.calls == [((query,), "query", "query-expansion-fixture")]
    assert len(reranker.calls) == 1
    rerank_query, documents, request_id = reranker.calls[0]
    assert rerank_query == query
    assert request_id == "query-expansion-fixture"
    assert {document.id for document in documents} <= (reduced if revoke_before_rerank else allowed)
    assert all(hit.tool.tool_id in reduced for hit in result.hits)

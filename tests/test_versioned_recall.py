"""版本化同义词索引保持精确操作匹配和中文库存召回。"""

import time
from dataclasses import replace

import numpy as np
import pytest

from semantic_core import _index_config
from semantic_core.bm25 import BM25Index, DocumentFields
from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord
from semantic_core.providers import EmbeddingResult, ModelProfile, RequestContext
from semantic_core.search import SearchConfig, SearchEngine
from semantic_core.snapshot import IndexSnapshot, load_snapshot, save_snapshot
from semantic_core.tokenization import ChineseTokenizer, TokenizerConfig
from semantic_core.vectors import VectorIndex

GATEWAY = GatewayIdentity("000000000000", "cn-north-1", "versioned-fixture")
PROFILE = ModelProfile("versioned-fixture", "1", 2)


def _snapshot():
    # Independent synthetic tools exercise operation synonyms and persistence.
    tools = tuple(
        ToolRecord.from_descriptor(
            ToolIdentity(GATEWAY, "fixture-target", "1", f"fixture-inventory___{action}_product"),
            {
                "name": f"fixture-inventory___{action}_product",
                "description": f"{verb}库存产品 / {action} inventory product",
                "inputSchema": {"type": "object", "properties": {}},
            },
        )
        for action, verb in (
            ("create", "创建"),
            ("read", "读取"),
            ("update", "更新"),
            ("delete", "删除"),
        )
    )
    return IndexSnapshot.build(GATEWAY, tools)


def _search(snapshot, query, embedding=None, diagnostics=None):
    return SearchEngine(
        snapshot, embedding, config=SearchConfig(fusion_strategy="vector_bm25_fallback")
    ).search(
        query,
        frozenset(tool.tool_id for tool in snapshot.tools),
        RequestContext("versioned-recall", time.monotonic() + 10),
        diagnostics=diagnostics,
    )


@pytest.mark.parametrize("query", ["read product", "查看 product", "查询 product", "读取 product"])
def test_exact_read_operation_stays_first_after_index_rebuild(query):
    result = _search(_snapshot(), query)
    assert result.hits[0].tool.name == "fixture-inventory___read_product"


def test_inventory_aliases_are_persisted_with_the_new_index_version(tmp_path):
    snapshot = _snapshot()
    assert snapshot.bm25.tokenizer.config.synonyms_version == "synonyms-v2"
    assert dict(snapshot.bm25.tokenizer.config.synonyms)["新增"] == (
        "create",
        "新建",
        "创建",
        "添加",
    )
    save_snapshot(snapshot, tmp_path / "new")
    restored = load_snapshot(tmp_path / "new", expected_gateway=GATEWAY)
    assert restored.bm25.to_dict() == snapshot.bm25.to_dict()
    result = _search(restored, "新增一个库存产品")
    assert result.hits[0].tool.name == "fixture-inventory___create_product"


def test_build_uses_the_explicit_tokenizer_profile_and_persists_it(tmp_path):
    tools = _snapshot().tools
    config = TokenizerConfig(
        synonyms_version="explicit-profile-v3", synonyms=(("product", ("商品",)),)
    )
    tokenizer = ChineseTokenizer(config)
    snapshot = IndexSnapshot.build(GATEWAY, tools, tokenizer=tokenizer)
    assert snapshot.bm25.tokenizer.config == config
    save_snapshot(snapshot, tmp_path / "explicit")
    restored = load_snapshot(tmp_path / "explicit", expected_gateway=GATEWAY)
    assert restored.bm25.tokenizer.config == config
    assert restored.bm25.to_dict() == snapshot.bm25.to_dict()


def test_legacy_snapshot_keeps_its_own_configuration_and_scores(tmp_path):
    current = _snapshot()
    legacy = replace(
        current,
        bm25=BM25Index.build(
            [tool.fields() for tool in current.tools], ChineseTokenizer(TokenizerConfig())
        ),
    )
    scores = legacy.bm25.masked_scores("read product", [True] * len(current.tools))
    save_snapshot(legacy, tmp_path / "legacy")
    restored = load_snapshot(tmp_path / "legacy", expected_gateway=GATEWAY)
    assert restored.bm25.tokenizer.config.synonyms_version == "synonyms-v1"
    assert restored.bm25.to_dict() == legacy.bm25.to_dict()
    assert (
        restored.bm25.masked_scores("read product", [True] * len(current.tools)).tolist()
        == scores.tolist()
    )
    assert _search(restored, "read product").hits[0].tool.name.endswith("___read_product")


@pytest.mark.parametrize(
    "legacy,lengths,scores",
    [
        (True, [6, 6, 6], [0.236910535, 0.236910535, 0.236910535]),
        (False, [14, 12, 10], [0.231314223, 0.236910535, 0.242784350]),
    ],
)
def test_action_group_size_has_the_documented_normalization_cost(legacy, lengths, scores):
    # 已知代价：查询不含动作，TF/DF 相同，展开后的长度仍使较大动作组失分。
    # 固定观测不是宣称组大小中立；改变词表或评分口径时须重新评估这个取舍。
    config = TokenizerConfig() if legacy else _index_config.TOKENIZER_CONFIG
    documents = [
        DocumentFields(f"{action}_x", f"{action} x", "shared")
        for action in ("create", "update", "delete")
    ]
    index = BM25Index.build(documents, ChineseTokenizer(config))
    assert index.document_lengths.tolist() == lengths
    assert index.document_frequencies["x"] == 3
    assert [row["x"] for row in index._term_frequencies] == [5.0, 5.0, 5.0]
    assert index.masked_scores("x", [True] * 3).tolist() == pytest.approx(scores, abs=1e-9)


class _LowSimilarityEmbedding:
    def describe(self):
        return PROFILE

    def embed(self, texts, purpose, context):
        return EmbeddingResult(np.array([[1.0, 0.0]], dtype=np.float32), PROFILE)


def test_rebuilt_index_covers_healthy_low_similarity_bm25_fallback():
    snapshot = _snapshot()
    vectors = VectorIndex(
        [[0.4, (1 - 0.4**2) ** 0.5]] * len(snapshot.tools),
        list(range(len(snapshot.tools))),
        len(snapshot.tools),
        2,
    )
    snapshot = replace(snapshot, vectors=vectors, model_profile=PROFILE)
    diagnostics = []
    result = _search(snapshot, "新增一个库存产品", _LowSimilarityEmbedding(), diagnostics.append)
    assert result.mode == "bm25"
    assert diagnostics[0].vector_availability == "available"
    assert not diagnostics[0].degraded
    assert result.hits[0].tool.name == "fixture-inventory___create_product"

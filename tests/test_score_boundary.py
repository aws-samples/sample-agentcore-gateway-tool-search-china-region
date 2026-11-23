"""Public result types cannot export full-catalog lexical statistics."""

import time
from dataclasses import asdict, fields

from semantic_core.bm25 import BM25Index, DocumentFields
from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord
from semantic_core.providers import RequestContext
from semantic_core.search import SearchEngine, SearchHit
from semantic_core.snapshot import IndexSnapshot


def corpus(hidden_count, hidden_description):
    gateway = GatewayIdentity("000000000000", "cn-north-1", "score-boundary")

    def tool(name, description):
        return ToolRecord.from_descriptor(
            ToolIdentity(gateway, "target", "one", name),
            {"name": name, "description": description, "inputSchema": {}},
        )

    visible = tool("visible", "取消订单 cancel order")
    hidden = [tool(f"hidden{i}", hidden_description) for i in range(hidden_count)]
    return IndexSnapshot.build(gateway, [visible, *hidden]), frozenset({visible.tool_id})


def test_default_result_is_identical_when_only_hidden_lexical_statistics_change():
    payloads = []
    for count, description in [
        (0, ""),
        (1, "取消订单 cancel order"),
        (5, "取消订单 cancel order"),
        (50, "取消订单 cancel order"),
        (50, "unrelated inventory stock with longer description"),
    ]:
        snapshot, allowed = corpus(count, description)
        result = SearchEngine(snapshot).search(
            "取消订单", allowed, RequestContext("score-boundary", time.monotonic() + 10)
        )
        payloads.append(asdict(result))
    assert all(payload == payloads[0] for payload in payloads[1:])


def test_search_hit_type_has_no_raw_score_field_or_property():
    snapshot, allowed = corpus(1, "取消订单")
    result = SearchEngine(snapshot).search(
        "取消订单", allowed, RequestContext("score-boundary", time.monotonic() + 10)
    )
    assert tuple(field.name for field in fields(SearchHit)) == ("tool",)
    assert not hasattr(result.hits[0], "score")
    assert "score" not in asdict(result)["hits"][0]


def test_unmasked_bm25_scores_are_not_a_public_method():
    index = BM25Index.build([DocumentFields("visible", "cancel")])
    assert not hasattr(index, "score")
    assert index.masked_scores("cancel", [True]).shape == (1,)

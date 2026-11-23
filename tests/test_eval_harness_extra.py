"""Independent guards against ambiguous truth in the synthetic knee remapping."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import pytest

from semantic_core.catalog import ToolRecord
from semantic_core.providers import EmbeddingProvider, RequestContext, RerankProvider
from semantic_core.snapshot import IndexSnapshot
from semantic_eval import harness
from semantic_eval.dataset import generate_dataset
from semantic_eval.selection import SelectionResult


def test_knee_query_namespace_makes_duplicate_crud_semantics_unambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = generate_dataset()
    query = next(
        query
        for query in data.queries
        if query.gateway_id == "gateway-2"
        and query.language == "en"
        and len(query.relevant_ids) == 1
        and set(query.relevant_ids) <= set(query.allowed_ids)
    )
    truth = next(tool for tool in data.tools if tool.id == query.relevant_ids[0])
    data = replace(data, queries=(query,))
    prefix = f"Synthetic namespace: {query.gateway_id}. "
    requested_name = f"{query.gateway_id}_{truth.descriptor['name']}"
    observed: list[tuple[str, ...]] = []
    retrieval_queries: list[str] = []
    original_retrieve = harness._retrieve

    def capture_retrieval(
        snapshot: IndexSnapshot,
        query_text: str,
        allowed: frozenset[str],
        method: str,
        embedding: EmbeddingProvider,
        reranker: RerankProvider | None,
        timeout: float,
    ) -> tuple[tuple[str, ...], str]:
        retrieval_queries.append(query_text)
        return original_retrieve(
            snapshot, query_text, allowed, method, embedding, reranker, timeout
        )

    class NamespaceSelector:
        def select(
            self, query_text: str, tools: Sequence[ToolRecord], context: RequestContext
        ) -> SelectionResult:
            context.remaining()
            assert query_text == prefix + query.text
            for tool in tools:
                namespace = tool.name.split("_", 1)[0]
                assert tool.description.startswith(f"Synthetic namespace: {namespace}. ")
            observed.append(tuple(tool.name for tool in tools))
            exact = [tool for tool in tools if tool.name == requested_name]
            assert len(exact) <= 1
            return SelectionResult(exact[0].tool_id if exact else None)

    monkeypatch.setattr(harness, "_retrieve", capture_retrieval)
    report = harness.run_knee(
        data,
        NamespaceSelector(),
        sizes=(20, 50, 100, 200, 500),
        queries_per_size=1,
        bootstrap_samples=20,
    )
    full_runs = [row for row in report["runs"] if row["arm"] == "full_load"]
    assert len(full_runs) == 5
    assert all(row["accuracy"]["value"] == 1.0 for row in full_runs)
    assert retrieval_queries == [prefix + query.text] * 5
    all_tools = next(names for names in observed if len(names) == 500)
    duplicates = [name for name in all_tools if name.split("_", 1)[1] == truth.descriptor["name"]]
    assert len(duplicates) == 5
    assert requested_name in duplicates
    assert "namespace" in report["protocol"].lower()

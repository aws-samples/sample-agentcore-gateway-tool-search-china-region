"""Regression guards for request-time identity serialization and mask scans."""

import time
from dataclasses import replace

import numpy as np

from semantic_core import catalog
from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord
from semantic_core.providers import EmbeddingResult, ModelProfile, RequestContext
from semantic_core.search import SearchEngine
from semantic_core.snapshot import IndexSnapshot
from semantic_core.vectors import VectorIndex


class Embedding:
    profile = ModelProfile("fixture-model", "one", 2)

    def describe(self):
        return self.profile

    def embed(self, texts, purpose, context):
        return EmbeddingResult(np.array([[1, 0]], dtype=np.float32), self.profile)


def make_snapshot(count=300):
    gateway = GatewayIdentity("000000000000", "cn-north-1", "identity-fixture")
    tools = tuple(
        ToolRecord.from_descriptor(
            ToolIdentity(gateway, "target", "one", f"tool{i:05}"),
            {"name": f"tool{i:05}", "description": "cancel order", "inputSchema": {}},
        )
        for i in range(count)
    )
    return IndexSnapshot.build(
        gateway,
        tools,
        vectors=VectorIndex([[1, 0]] * count, list(range(count)), count, 2),
        model_profile=Embedding.profile,
    )


def test_search_never_serializes_an_existing_catalog_identity(monkeypatch):
    snapshot = make_snapshot()
    allowed = frozenset(tool.tool_id for tool in snapshot.tools)
    serialized = []
    original = catalog.canonical_json

    def spy(value):
        serialized.append(value)
        return original(value)

    monkeypatch.setattr(catalog, "canonical_json", spy)
    result = SearchEngine(snapshot, Embedding()).search(
        "cancel", allowed, RequestContext("identity-fixture", time.monotonic() + 10)
    )
    assert len(result.hits) == 5
    assert serialized == [], f"request serialized {len(serialized)} immutable identities"


def test_search_does_not_rescan_tool_ids_for_each_channel_mask(monkeypatch):
    snapshot = make_snapshot()
    allowed = frozenset(tool.tool_id for tool in snapshot.tools)
    accesses = 0
    original = ToolRecord.tool_id.fget

    def counted(tool):
        nonlocal accesses
        accesses += 1
        return original(tool)

    monkeypatch.setattr(ToolRecord, "tool_id", property(counted))
    SearchEngine(snapshot, Embedding()).search(
        "cancel",
        allowed,
        RequestContext("identity-fixture", time.monotonic() + 10),
        reauthorize=lambda: allowed,
    )
    assert accesses <= len(snapshot.tools) + 100, f"request accessed {accesses} tool IDs"


def test_recreated_identity_refreshes_the_construction_cache():
    original = make_snapshot(1).tools[0]
    recreated = replace(original, identity=replace(original.identity, incarnation="two"))
    assert recreated.tool_id == recreated.identity.key
    assert recreated.tool_id != original.tool_id

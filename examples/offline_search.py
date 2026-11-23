"""Run synthetic authorized retrieval with deterministic mocks and no network."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord
from semantic_core.providers import MockEmbeddingProvider, RequestContext
from semantic_core.search import SearchConfig, SearchEngine
from semantic_core.snapshot import IndexSnapshot, load_snapshot, save_snapshot
from semantic_core.vectors import VectorIndex


def example_catalog(gateway: GatewayIdentity) -> tuple[ToolRecord, ...]:
    """All descriptors are synthetic; the full catalog includes a denied tool."""
    descriptions = (
        ("orders___cancel_order", "取消尚未发货的订单"),
        ("orders___get_order", "查询订单状态和发货进度"),
        ("orders___export_orders", "查询并导出所有客户订单，包含取消订单"),
    )
    return tuple(
        ToolRecord.from_descriptor(
            ToolIdentity(gateway, "orders", "fixture-v1", name),
            {
                "name": name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string", "description": "订单编号"},
                    },
                    "required": ["order_id"],
                    "additionalProperties": False,
                },
            },
        )
        for name, description in descriptions
    )


def run(directory: Path, query: str) -> dict[str, object]:
    gateway = GatewayIdentity("000000000000", "cn-north-1", "offline-example")
    tools = example_catalog(gateway)
    provider = MockEmbeddingProvider(dimension=16)
    embedded = provider.embed(
        [tool.summary for tool in tools],
        "document",
        RequestContext("example-index", time.monotonic() + 30),
    )
    vectors = VectorIndex(
        embedded.vectors,
        parent_indices=list(range(len(tools))),
        tool_count=len(tools),
        dimension=16,
    )
    snapshot = IndexSnapshot.build(
        gateway,
        tools,
        vectors=vectors,
        model_profile=provider.describe(),
        build_id="example-v1",
    )
    save_snapshot(snapshot, directory)
    restored = load_snapshot(
        directory,
        expected_gateway=gateway,
        expected_model=provider.describe(),
    )
    allowed_ids = frozenset(tool.tool_id for tool in tools[:2])

    def reauthorize() -> frozenset[str]:
        # A real caller supplies a trusted current authorization source at runtime.
        return allowed_ids

    result = SearchEngine(restored, embedding=provider, config=SearchConfig(top_k=2)).search(
        query,
        allowed_ids,
        RequestContext("example-search", time.monotonic() + 5),
        reauthorize=reauthorize,
    )
    if result.mode != "rrf":
        raise RuntimeError("the offline fixture must retain its vector channel")
    if any(hit.tool.tool_id not in allowed_ids for hit in result.hits):
        raise RuntimeError("the offline fixture returned a denied tool")
    # Public output contains only authorized descriptors, without scores or IDs.
    return {"tools": [hit.tool.descriptor for hit in result.hits]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="取消尚未发货的订单")
    parser.add_argument(
        "--output",
        type=Path,
        help="retain a new snapshot directory; its parent must already exist",
    )
    arguments = parser.parse_args()
    if arguments.output is None:
        with TemporaryDirectory(prefix="semantic-example-") as temporary:
            result = run(Path(temporary) / "snapshot", arguments.query)
    else:
        result = run(arguments.output, arguments.query)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

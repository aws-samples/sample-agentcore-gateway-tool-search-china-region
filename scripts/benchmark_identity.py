"""Offline request-cost comparison, with the old search loaded only in memory."""

from __future__ import annotations

import hashlib
import json
import platform
import statistics
import subprocess  # nosec B404
import sys
import time
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from semantic_core import catalog  # noqa: E402
from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord  # noqa: E402
from semantic_core.providers import EmbeddingResult, ModelProfile, RequestContext  # noqa: E402
from semantic_core.search import SearchEngine  # noqa: E402
from semantic_core.snapshot import IndexSnapshot  # noqa: E402
from semantic_core.vectors import VectorIndex  # noqa: E402

COUNT = 20_000
REPEATS = 5
BASELINE = "242bd4abba97c7240b553434be2c290be2b4d74a"


class Embedding:
    profile = ModelProfile("fixture-embedding", "one", 2)

    def describe(self):
        return self.profile

    def embed(self, texts, purpose, context):
        return EmbeddingResult(np.array([[1, 0]], dtype=np.float32), self.profile)


def main() -> None:
    source = subprocess.check_output(  # nosec B607
        ["git", "show", f"{BASELINE}:src/semantic_core/search.py"],
        cwd=ROOT,
        text=True,
        shell=False,  # nosec B603
    )
    module = types.ModuleType("_identity_cost_baseline")
    sys.modules[module.__name__] = module
    # Reviewed full Git object ID and fixed source path, for offline comparison only.
    # nosemgrep: exec-detected
    exec(compile(source, f"<git:{BASELINE}:search.py>", "exec"), module.__dict__)  # nosec B102

    gateway = GatewayIdentity("fixture-account", "fixture-region", "identity-cost")
    started = time.perf_counter()
    tools = tuple(
        ToolRecord.from_descriptor(
            ToolIdentity(gateway, "target", "one", f"tool{index:05}"),
            {"name": f"tool{index:05}", "description": "cancel order", "inputSchema": {}},
        )
        for index in range(COUNT)
    )
    snapshot = IndexSnapshot.build(
        gateway,
        tools,
        vectors=VectorIndex([[1, 0]] * COUNT, list(range(COUNT)), COUNT, 2),
        model_profile=Embedding.profile,
    )
    setup_seconds = time.perf_counter() - started
    allowed = frozenset(tool.tool_id for tool in tools)
    engines = {"baseline": module.SearchEngine(snapshot, Embedding())}
    engines["current"] = SearchEngine(snapshot, Embedding())
    original_property = ToolRecord.tool_id
    original_json = catalog.canonical_json
    baseline_property = property(lambda tool: tool.identity.key)
    properties = {"baseline": baseline_property, "current": original_property}
    samples = {name: [] for name in engines}
    result_names = {}
    counts = {}

    def search(name):
        context = RequestContext("identity-cost-fixture", time.monotonic() + 30)
        start = time.perf_counter()
        result = engines[name].search("cancel", allowed, context)
        elapsed = time.perf_counter() - start
        assert result.mode == "rrf"
        result_names[name] = [hit.tool.name for hit in result.hits]
        return elapsed * 1000

    try:
        for name in engines:
            ToolRecord.tool_id = properties[name]
            search(name)
        for repeat in range(REPEATS):
            order = ("baseline", "current") if repeat % 2 == 0 else ("current", "baseline")
            for name in order:
                ToolRecord.tool_id = properties[name]
                samples[name].append(search(name))
        assert result_names["baseline"] == result_names["current"]

        # Counts are a separate pass, so timings do not include instrumentation.
        for name in engines:
            observed = {"identity_serializations": 0, "tool_id_accesses": 0}
            getter = properties[name].fget

            def count_json(value, observed=observed):
                if isinstance(value, list) and len(value) == 6:
                    observed["identity_serializations"] += 1
                return original_json(value)

            def count_getter(tool, observed=observed, getter=getter):
                observed["tool_id_accesses"] += 1
                return getter(tool)

            catalog.canonical_json = count_json
            ToolRecord.tool_id = property(count_getter)
            search(name)
            counts[name] = dict(observed)
            catalog.canonical_json = original_json
    finally:
        ToolRecord.tool_id = original_property
        catalog.canonical_json = original_json
        sys.modules.pop(module.__name__, None)

    assert counts["current"]["identity_serializations"] == 0
    assert counts["baseline"]["identity_serializations"] >= 6 * COUNT
    baseline_median = statistics.median(samples["baseline"])
    current_median = statistics.median(samples["current"])
    report = {
        "scope": "Synthetic local hotspot measurement; not a production SLO or capacity benchmark.",
        "method": (
            "Load exact baseline search source from git into an isolated in-memory module. "
            "Restore the old identity.key getter only inside this benchmark process. "
            "Both variants share the current snapshot, BM25/vector/provider implementations "
            "and installed dependencies to isolate search/getter cost. "
            "One warm-up per variant; five uninstrumented alternating timing samples. "
            "Identity counts use a separate instrumented pass."
        ),
        "baseline_revision": subprocess.check_output(  # nosec B607
            ["git", "rev-parse", BASELINE],
            cwd=ROOT,
            text=True,
            shell=False,  # nosec B603
        ).strip(),
        "baseline_search_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "current_search_sha256": hashlib.sha256(
            (ROOT / "src/semantic_core/search.py").read_bytes()
        ).hexdigest(),
        "python": platform.python_version(),
        "system": platform.system(),
        "machine": platform.machine(),
        "fixture": {
            "tools": COUNT,
            "vectors_per_tool": 1,
            "vector_dimension": 2,
            "authorized_tools": COUNT,
            "query": "cancel",
            "embedding": "constant in-process fixture",
            "rerank": "disabled",
            "mode": "rrf",
            "setup_seconds_excluded_from_timings": setup_seconds,
        },
        "same_returned_tool_order": result_names["baseline"] == result_names["current"],
        "returned_tools": result_names["current"],
        "baseline": {
            **counts["baseline"],
            "request_ms_samples": samples["baseline"],
            "request_ms_median": baseline_median,
        },
        "current": {
            **counts["current"],
            "request_ms_samples": samples["current"],
            "request_ms_median": current_median,
        },
        "median_reduction_percent": (1 - current_median / baseline_median) * 100,
        "constraints": "No checkout/reset/source writes, credentials, network calls or installs.",
    }
    destination = ROOT / ".artifacts/fix-identity-performance.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

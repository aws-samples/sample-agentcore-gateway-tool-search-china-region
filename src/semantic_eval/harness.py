"""Local retrieval ablations and controlled tool-count experiments.

Each retrieval index is Gateway scoped. The knee fixture explicitly remaps the
synthetic catalog into one experimental Gateway; it is not a search aggregator.
"""

from __future__ import annotations

import hashlib
import random
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord, canonical_json
from semantic_core.providers import (
    EmbeddingProvider,
    EmbeddingResult,
    ErrorCode,
    MockEmbeddingProvider,
    MockRerankProvider,
    ModelProfile,
    ProviderError,
    Purpose,
    RequestContext,
    RerankDocument,
    RerankProvider,
    RerankResult,
    validate_embedding_result,
)
from semantic_core.search import SearchConfig, SearchEngine
from semantic_core.snapshot import IndexSnapshot
from semantic_core.vectors import VectorIndex
from semantic_eval.dataset import Dataset, EvalQuery, EvalTool
from semantic_eval.metrics import Observation, estimate, summarize
from semantic_eval.selection import SelectionResult, Selector

METHODS = ("bm25", "vector", "rrf", "rrf_rerank")
Checkpoint = Callable[[dict[str, Any]], None]


@dataclass
class CallBudget:
    """Hard limit on attempted embedding, rerank and chat calls, including failures."""

    limit: int | None = 2000
    calls: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    usage: dict[str, dict[str, int]] = field(default_factory=dict)
    document_cache_hits: int = 0

    def __post_init__(self) -> None:
        if self.limit is not None and (type(self.limit) is not int or not 1 <= self.limit <= 2000):
            raise ValueError("total model call limit must be 1..2000")

    def before(self, kind: str) -> None:
        if self.limit is not None and sum(self.calls.values()) >= self.limit:
            self.errors["budget_exhausted"] = self.errors.get("budget_exhausted", 0) + 1
            raise ProviderError(ErrorCode.UNAVAILABLE)
        self.calls[kind] = self.calls.get(kind, 0) + 1

    def after(self, kind: str, usage: Mapping[str, int]) -> None:
        counters = self.usage.setdefault(kind, {})
        for key, value in usage.items():
            counters[key] = counters.get(key, 0) + value


class MeteredEmbedding:
    """Cache document vectors across ablations, never cache timed query vectors."""

    def __init__(self, provider: EmbeddingProvider, budget: CallBudget) -> None:
        self.provider = provider
        self.budget = budget
        self.cache: dict[str, np.ndarray[Any, np.dtype[np.float32]]] = {}

    def describe(self) -> ModelProfile:
        return self.provider.describe()

    def embed(
        self, texts: Sequence[str], purpose: Purpose, context: RequestContext
    ) -> EmbeddingResult:
        context.remaining()
        if purpose == "document" and all(text in self.cache for text in texts):
            self.budget.document_cache_hits += len(texts)
            return EmbeddingResult(np.stack([self.cache[t] for t in texts]), self.describe())
        self.budget.before("embedding")
        usage: Mapping[str, int] = {}
        try:
            result = self.provider.embed(texts, purpose, context)
            usage = result.usage
            vectors = validate_embedding_result(result, self.describe(), len(texts))
        except ProviderError as exc:
            key = "embedding_" + exc.code.value
            self.budget.errors[key] = self.budget.errors.get(key, 0) + 1
            self.budget.after("embedding", usage or exc.usage)
            raise
        self.budget.after("embedding", result.usage)
        if purpose == "document":
            self.cache.update(zip(texts, vectors, strict=True))
        return result


class MeteredRerank:
    def __init__(self, provider: RerankProvider, budget: CallBudget) -> None:
        self.provider = provider
        self.budget = budget

    def describe(self) -> ModelProfile:
        return self.provider.describe()

    def rerank(
        self,
        query: str,
        documents: Sequence[RerankDocument],
        top_n: int,
        context: RequestContext,
    ) -> RerankResult:
        self.budget.before("rerank")
        try:
            result = self.provider.rerank(query, documents, top_n, context)
        except ProviderError as exc:
            key = "rerank_" + exc.code.value
            self.budget.errors[key] = self.budget.errors.get(key, 0) + 1
            self.budget.after("rerank", exc.usage)
            raise
        self.budget.after("rerank", result.usage)
        return result


class MeteredSelector:
    """Charge selection calls and reported usage even when validation fails."""

    def __init__(self, selector: Selector, budget: CallBudget) -> None:
        self.selector = selector
        self.budget = budget

    def select(
        self, query: str, tools: Sequence[ToolRecord], context: RequestContext
    ) -> SelectionResult:
        context.remaining()
        if not tools:
            return self.selector.select(query, tools, context)
        self.budget.before("chat")
        try:
            result = self.selector.select(query, tools, context)
        except ProviderError as exc:
            key = "chat_" + exc.code.value
            self.budget.errors[key] = self.budget.errors.get(key, 0) + 1
            self.budget.after("chat", exc.usage)
            raise
        self.budget.after("chat", result.usage)
        return result


def _checkpoint(
    callback: Checkpoint | None,
    budget: CallBudget,
    *,
    builds: Sequence[dict[str, Any]],
    runs: Sequence[dict[str, Any]],
) -> None:
    if callback is not None:
        callback(
            {
                "synthetic": True,
                "quality_acceptance": False,
                "builds": list(builds),
                "runs": list(runs),
                "provider_accounting": asdict(budget),
            }
        )


def _context(seconds: float) -> RequestContext:
    return RequestContext("synthetic-evaluation", time.monotonic() + seconds)


def _records(
    tools: Sequence[EvalTool], examples: bool, *, gateway_override: str | None = None
) -> tuple[tuple[ToolRecord, ...], dict[str, str]]:
    records = []
    external_ids = {}
    for tool in tools:
        gateway = GatewayIdentity("000000000000", "cn-north-1", gateway_override or tool.gateway_id)
        descriptor = dict(tool.descriptor)
        if gateway_override is not None:
            # Preserve distinct namespaces after remapping five synthetic catalogs.
            descriptor["name"] = tool.gateway_id + "_" + str(descriptor["name"])
            descriptor["description"] = f"Synthetic namespace: {tool.gateway_id}. " + str(
                descriptor["description"]
            )
        identity = ToolIdentity(gateway, tool.system, "synthetic-v1", str(descriptor["name"]))
        record = ToolRecord.from_descriptor(identity, descriptor, tool.examples if examples else ())
        records.append(record)
        external_ids[record.tool_id] = tool.id
    return tuple(records), external_ids


def _snapshot(
    records: Sequence[ToolRecord],
    embedding: EmbeddingProvider,
    *,
    batch_size: int,
    timeout: float,
) -> IndexSnapshot:
    if not records:
        raise ValueError("evaluation requires a nonempty catalog")
    texts = []
    parents = []
    for index, record in enumerate(records):
        for text in (record.summary, *record.examples):
            texts.append(text)
            parents.append(index)
    profile = embedding.describe()
    if profile.dimension is None:
        raise ValueError("embedding dimension is required")
    batches = [
        validate_embedding_result(
            embedding.embed(texts[start : start + batch_size], "document", _context(timeout)),
            profile,
            len(texts[start : start + batch_size]),
        )
        for start in range(0, len(texts), batch_size)
    ]
    return IndexSnapshot.build(
        records[0].identity.gateway,
        records,
        vectors=VectorIndex(np.concatenate(batches), parents, len(records), profile.dimension),
        model_profile=profile,
    )


def _retrieve(
    snapshot: IndexSnapshot,
    query: str,
    allowed: frozenset[str],
    method: str,
    embedding: EmbeddingProvider,
    reranker: RerankProvider | None,
    timeout: float,
) -> tuple[tuple[str, ...], str]:
    if method != "vector":
        engine = SearchEngine(
            snapshot,
            embedding if method != "bm25" else None,
            reranker,
            SearchConfig(
                rerank_enabled=method == "rrf_rerank",
                embedding_timeout=timeout,
                rerank_timeout=timeout,
            ),
        )
        result = engine.search(
            query, allowed, _context(2 * timeout + 5), reauthorize=lambda: allowed
        )
        return tuple(hit.tool.tool_id for hit in result.hits), result.mode
    if not allowed:
        return (), "vector"
    assert snapshot.vectors is not None and snapshot.model_profile is not None
    query_vector = validate_embedding_result(
        embedding.embed([query], "query", _context(timeout)), snapshot.model_profile, 1
    )[0]
    scores = snapshot.vectors.scores(
        query_vector, [tool.tool_id in allowed for tool in snapshot.tools]
    )
    indices = sorted(
        (i for i in range(len(scores)) if np.isfinite(scores[i])),
        key=lambda i: (-float(scores[i]), snapshot.tools[i].name),
    )
    return tuple(snapshot.tools[i].tool_id for i in indices[:5]), "vector"


def evaluate(
    data: Dataset,
    *,
    embedding: EmbeddingProvider | None = None,
    reranker: RerankProvider | None = None,
    real: bool = False,
    enable_rerank: bool = True,
    budget: CallBudget | None = None,
    batch_size: int = 32,
    timeout: float = 30.0,
    bootstrap_samples: int = 1000,
    checkpoint: Checkpoint | None = None,
) -> dict[str, Any]:
    budget = budget or CallBudget(limit=2000 if real else None)
    if real and budget.limit is None:
        raise ValueError("real mode requires a finite call budget")
    embedded = MeteredEmbedding(embedding or MockEmbeddingProvider(), budget)
    ranked = MeteredRerank(reranker or MockRerankProvider(), budget)
    if real and (embedding is None or (enable_rerank and reranker is None)):
        raise ValueError("real mode requires explicitly configured providers")
    if batch_size < 1 or timeout <= 0:
        raise ValueError("batch size and timeout must be positive")
    runs: list[dict[str, Any]] = []
    builds = []
    methods = METHODS if enable_rerank else METHODS[:-1]
    for examples in (False, True):
        snapshots = {}
        for gateway_id in sorted({tool.gateway_id for tool in data.tools}):
            tools = [tool for tool in data.tools if tool.gateway_id == gateway_id]
            records, id_map = _records(tools, examples)
            started = time.perf_counter()
            snapshot = _snapshot(records, embedded, batch_size=batch_size, timeout=timeout)
            snapshots[gateway_id] = (snapshot, id_map)
            builds.append(
                {
                    "gateway_id": gateway_id,
                    "examples": examples,
                    "tool_count": len(records),
                    "vector_count": len(snapshot.vectors.vectors) if snapshot.vectors else 0,
                    "build_ms": (time.perf_counter() - started) * 1000,
                }
            )
            _checkpoint(checkpoint, budget, builds=builds, runs=runs)
        for method in methods:
            observations = []
            modes: Counter[str] = Counter()
            for query in data.queries:
                snapshot, id_map = snapshots[query.gateway_id]
                authorized = frozenset(
                    key for key, value in id_map.items() if value in query.allowed_ids
                )
                started = time.perf_counter()
                error = None
                try:
                    ids, mode = _retrieve(
                        snapshot, query.text, authorized, method, embedded, ranked, timeout
                    )
                    retrieved = tuple(id_map[key] for key in ids)
                except ProviderError as exc:
                    retrieved, mode, error = (), "error", exc.code.value
                elapsed = (time.perf_counter() - started) * 1000
                modes[mode] += 1
                observations.append(
                    Observation(
                        query.id,
                        query.language,
                        query.permission_group,
                        query.relevant_ids,
                        query.allowed_ids,
                        retrieved,
                        elapsed,
                        error,
                    )
                )
                _checkpoint(
                    checkpoint,
                    budget,
                    builds=builds,
                    runs=[
                        *runs,
                        {
                            "configuration": {"method": method, "examples": examples},
                            "observations": [asdict(row) for row in observations],
                        },
                    ],
                )
            runs.append(
                {
                    "configuration": {"method": method, "examples": examples},
                    "metrics": summarize(
                        observations, seed=data.seed, bootstrap_samples=bootstrap_samples
                    ),
                    "mode_counts": dict(modes),
                    "observations": [asdict(row) for row in observations],
                }
            )
    return {
        "metadata": {
            "synthetic": True,
            "label": "合成数据 / synthetic data",
            "quality_acceptance": False,
            "purpose": "synthetic real-model experiment" if real else "offline regression baseline",
            "seed": data.seed,
            "dataset_sha256": hashlib.sha256(canonical_json(asdict(data))).hexdigest(),
            "tool_count": len(data.tools),
            "query_count": len(data.queries),
            "gateway_count": len(snapshots),
            "provider_mode": "real" if real else "deterministic_mock",
            "mode": "real" if real else "mock",
            "embedding_profile": asdict(embedded.describe()),
            "rerank_profile": asdict(ranked.describe()) if enable_rerank else None,
            "rerank_production_enabled": False,
            "latency_scope": (
                "warm local retrieval including query provider calls; no Gateway/network SLO"
            ),
            "document_cache": "build only; query embeddings are never cached",
            "ci_scope": (
                "query bootstrap, conditional on this synthetic corpus; no real-world quality claim"
            ),
        },
        "builds": builds,
        "runs": runs,
        "provider_accounting": asdict(budget),
    }


def _knee_queries(data: Dataset, count: int) -> list[EvalQuery]:
    # Round-robin languages avoids a language-specific prefix in a generated file.
    groups = {
        language: [
            query
            for query in data.queries
            if query.language == language
            and len(query.relevant_ids) == 1
            and set(query.relevant_ids) <= set(query.allowed_ids)
        ]
        for language in sorted({query.language for query in data.queries})
    }
    output: list[EvalQuery] = []
    while len(output) < count and any(groups.values()):
        for queries in groups.values():
            if queries and len(output) < count:
                output.append(queries.pop(0))
    if not output:
        raise ValueError("knee experiment requires singleton authorized truth")
    return output


def run_knee(
    data: Dataset,
    selector: Selector,
    *,
    sizes: Sequence[int] = (20, 50, 100, 200, 500),
    queries_per_size: int = 10,
    embedding: EmbeddingProvider | None = None,
    budget: CallBudget | None = None,
    timeout: float = 30.0,
    batch_size: int = 32,
    bootstrap_samples: int = 1000,
    checkpoint: Checkpoint | None = None,
) -> dict[str, Any]:
    if type(queries_per_size) is not int or not 1 <= queries_per_size <= 50:
        raise ValueError("each size must use 1..50 queries")
    if (
        not sizes
        or len(set(sizes)) != len(sizes)
        or any(type(size) is not int or not 1 <= size <= len(data.tools) for size in sizes)
    ):
        raise ValueError("sizes must be unique and fit the synthetic catalog")
    budget = budget or CallBudget(limit=None)
    provider = MeteredEmbedding(embedding or MockEmbeddingProvider(), budget)
    metered_selector = MeteredSelector(selector, budget)
    records, id_map = _records(data.tools, False, gateway_override="synthetic-knee")
    full_index = _snapshot(records, provider, batch_size=batch_size, timeout=timeout)
    assert full_index.vectors is not None
    by_external = {external: i for i, external in enumerate(id_map.values())}
    queries = _knee_queries(data, queries_per_size)
    runs: list[dict[str, Any]] = []
    for size in sizes:
        observations: dict[str, list[dict[str, Any]]] = {"full_load": [], "search_first": []}
        for query in queries:
            selection_query = f"Synthetic namespace: {query.gateway_id}. {query.text}"
            truth = by_external[query.relevant_ids[0]]
            distractors = [i for i in range(len(records)) if i != truth]
            rng = random.Random(f"{data.seed}:{query.id}")  # nosec B311
            rng.shuffle(distractors)
            chosen = [truth, *distractors[: size - 1]]
            rng.shuffle(chosen)
            catalog = tuple(records[i] for i in chosen)
            catalog_snapshot = IndexSnapshot.build(
                catalog[0].identity.gateway,
                catalog,
                vectors=VectorIndex(
                    full_index.vectors.vectors[chosen],
                    list(range(size)),
                    size,
                    full_index.vectors.dimension,
                ),
                model_profile=full_index.model_profile,
            )
            allowed = frozenset(tool.tool_id for tool in catalog)
            for arm in observations:
                started = time.perf_counter()
                error = None
                search_ms = 0.0
                search_mode = "not_applicable" if arm == "full_load" else "error"
                candidates = catalog
                usage: Mapping[str, int] = {}
                token_estimate = None
                usage_source = "unavailable"
                selected_id = None
                selecting = False
                try:
                    if arm == "search_first":
                        found, search_mode = _retrieve(
                            catalog_snapshot,
                            selection_query,
                            allowed,
                            "rrf",
                            provider,
                            None,
                            timeout,
                        )
                        by_id = {tool.tool_id: tool for tool in catalog}
                        candidates = tuple(by_id[key] for key in found)
                        search_ms = (time.perf_counter() - started) * 1000
                    selecting = True
                    selection = metered_selector.select(
                        selection_query, candidates, _context(timeout)
                    )
                    selected_id, usage, error = (
                        selection.selected_id,
                        selection.usage,
                        selection.error,
                    )
                    token_estimate, usage_source = selection.token_estimate, selection.usage_source
                except ProviderError as exc:
                    error = exc.code.value
                    usage = exc.usage if selecting else {}
                    usage_source = "provider" if usage else "unavailable"
                observations[arm].append(
                    {
                        "query_id": query.id,
                        "language": query.language,
                        "selected_id": selected_id,
                        "expected_id": records[truth].tool_id,
                        "candidate_ids": [tool.tool_id for tool in candidates],
                        "correct": selected_id == records[truth].tool_id and error is None,
                        "error": error,
                        "search_mode": search_mode,
                        "latency_ms": (time.perf_counter() - started) * 1000,
                        "search_ms": search_ms,
                        "candidate_count": len(candidates),
                        "usage": dict(usage),
                        "token_estimate": token_estimate,
                        "usage_source": usage_source,
                    }
                )
                _checkpoint(
                    checkpoint,
                    budget,
                    builds=[],
                    runs=[
                        *runs,
                        *[
                            {"tool_count": size, "arm": name, "observations": rows}
                            for name, rows in observations.items()
                        ],
                    ],
                )
        for arm, rows in observations.items():

            def metric(values: Sequence[float], statistic: str = "mean") -> dict[str, object]:
                if statistic == "p50":
                    return estimate(
                        values, statistic="p50", seed=data.seed, bootstrap_samples=bootstrap_samples
                    )
                if statistic == "p95":
                    return estimate(
                        values, statistic="p95", seed=data.seed, bootstrap_samples=bootstrap_samples
                    )
                return estimate(values, seed=data.seed, bootstrap_samples=bootstrap_samples)

            runs.append(
                {
                    "tool_count": size,
                    "arm": arm,
                    "n": len(rows),
                    "query_ids": [row["query_id"] for row in rows],
                    "truth_present_count": len(rows),
                    "accuracy": metric([float(row["correct"]) for row in rows]),
                    "n_errors": sum(row["error"] is not None for row in rows),
                    "search_mode_counts": dict(Counter(row["search_mode"] for row in rows)),
                    "latency_ms": {
                        name: metric([row["latency_ms"] for row in rows], name)
                        for name in ("p50", "p95")
                    },
                    "candidate_tools": {
                        "min": min(row["candidate_count"] for row in rows),
                        "max": max(row["candidate_count"] for row in rows),
                    },
                    "mean_reported_tokens": {
                        key: metric(
                            [float(row["usage"][key]) for row in rows if key in row["usage"]]
                        )
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                    },
                    "mean_estimated_prompt_tokens": metric(
                        [
                            float(row["token_estimate"])
                            for row in rows
                            if row["token_estimate"] is not None
                        ]
                    ),
                    "observations": rows,
                }
            )
    return {
        "synthetic": True,
        "quality_acceptance": False,
        "seed": data.seed,
        "protocol": (
            "Controlled single-Gateway synthetic remapping; singleton authorized truth inserted "
            "in each nested distractor catalog. Query and descriptor specify source namespace "
            "so duplicate CRUD functions have unambiguous truth. "
            "Same queries/catalogs paired across arms. "
            "Search-first uses the original query for configured RRF retrieval "
            "then one selection "
            "chat, both with the same namespace prefix; no tool execution. "
            "Language labels refer to the source query before that prefix. "
            "Actual search modes report BM25 degradation explicitly. "
            "Index build excluded, retrieval included in latency."
        ),
        # Report prose describing token accounting, not an authentication value.
        "token_scope": (  # nosec B105
            "Chat usage is provider-reported only; missing fields remain null. Mock estimate "
            "is serialized prompt UTF-8 bytes/4, not model tokens. Search embedding usage is "
            "reported separately in provider_accounting."
        ),
        "queries_requested_per_size": queries_per_size,
        "queries_actual_per_size": len(queries),
        "bootstrap": {"samples": bootstrap_samples, "seed": data.seed},
        "dataset_sha256": hashlib.sha256(canonical_json(asdict(data))).hexdigest(),
        "provider_accounting": asdict(budget),
        "runs": runs,
    }

"""Dev-only tuning followed by one frozen, independently claimed test evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from semantic_core.catalog import ToolRecord, canonical_json
from semantic_core.openai_compatible import (
    ChatConfig,
    EmbeddingConfig,
    OpenAICompatibleConfig,
    OpenAIEmbeddingProvider,
)
from semantic_core.providers import (
    EmbeddingProvider,
    EmbeddingResult,
    MockEmbeddingProvider,
    MockRerankProvider,
    ModelProfile,
    ProviderError,
    Purpose,
    RequestContext,
    RerankProvider,
)
from semantic_core.rerank import HTTPRerankProvider, RerankCodec, RerankConfig
from semantic_core.search import SearchConfig, SearchEngine
from semantic_core.snapshot import IndexSnapshot
from semantic_core.vectors import VectorIndex
from semantic_eval.dataset import Dataset, load_dataset
from semantic_eval.harness import _context, _records, _snapshot
from semantic_eval.metrics import Observation, summarize
from semantic_eval.runtime import CredentialsExpired, RealModeUnavailable, load_credentials
from semantic_eval.selection import MockSelector, OpenAISelector, Selector
from semantic_eval.tuning import paired_knee_queries, query_group_id, split_dataset
from semantic_eval.tuning_runtime import (
    DocumentCache,
    Ledger,
    RunStopped,
    claim_test,
    endpoint_fingerprint,
    file_sha256,
    read_provenance,
    save_json,
    utc_now,
    validate_test_provenance,
)

ROOT = Path(__file__).resolve().parents[1]
SEED = 20261011
NOTICE = "合成数据，不代表质量验收。Rerank 默认关闭。"
TIMEOUT = 60.0


class QueryReplay:
    """One live embedding per unique dev/test text; cache remains in memory."""

    def __init__(self, provider: EmbeddingProvider) -> None:
        self.provider = provider
        self.results: dict[str, EmbeddingResult] = {}
        self.duration: dict[str, float] = {}

    def describe(self) -> ModelProfile:
        return self.provider.describe()

    def embed(
        self, texts: Sequence[str], purpose: Purpose, context: RequestContext
    ) -> EmbeddingResult:
        if purpose != "query" or len(texts) != 1:
            return self.provider.embed(texts, purpose, context)
        text = texts[0]
        if text not in self.results:
            started = time.perf_counter()
            self.results[text] = self.provider.embed(texts, purpose, context)
            self.duration[text] = (time.perf_counter() - started) * 1000
        result = self.results[text]
        return EmbeddingResult(result.vectors, result.profile)


def log(event: str, **values: object) -> None:
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def grid() -> dict[str, SearchConfig]:
    base = SearchConfig(embedding_timeout=TIMEOUT, rerank_timeout=TIMEOUT)
    return {
        "rrf": base,
        "vector": replace(base, fusion_strategy="vector"),
        "weighted_0.1": replace(base, lexical_weight=0.1),
        "weighted_0.25": replace(base, lexical_weight=0.25),
        "weighted_0.5": replace(base, lexical_weight=0.5),
        "vector_fallback": replace(base, fusion_strategy="vector_bm25_fallback"),
    }


def build(
    data: Dataset, embedding: EmbeddingProvider
) -> dict[str, tuple[IndexSnapshot, dict[str, str]]]:
    snapshots = {}
    for gateway in sorted({tool.gateway_id for tool in data.tools}):
        records, id_map = _records([t for t in data.tools if t.gateway_id == gateway], False)
        snapshots[gateway] = (_snapshot(records, embedding, batch_size=10, timeout=TIMEOUT), id_map)
    return snapshots


def retrieval(
    data: Dataset,
    snapshots: dict[str, tuple[IndexSnapshot, dict[str, str]]],
    configs: dict[str, SearchConfig],
    provider: EmbeddingProvider,
    reranker: RerankProvider,
    output: Path,
    phase: str,
) -> list[dict[str, Any]]:
    replay = provider if isinstance(provider, QueryReplay) else QueryReplay(provider)
    # Prime before timing each configuration, sharing the identical model output.
    for query in data.queries:
        if query.allowed_ids:
            replay.embed([query.text], "query", _context(TIMEOUT))
    runs: list[dict[str, Any]] = []
    for name, config in configs.items():
        rows = []
        for query in data.queries:
            snapshot, external = snapshots[query.gateway_id]
            internal = {value: key for key, value in external.items()}
            allowed = frozenset(internal[key] for key in query.allowed_ids)
            engine = SearchEngine(snapshot, replay, reranker, config)
            started = time.perf_counter()
            error = None
            mode = "error"
            found: tuple[str, ...] = ()
            try:
                result = engine.search(query.text, allowed, _context(2 * TIMEOUT + 5))
                mode = result.ranking_mode
                found = tuple(external[hit.tool.tool_id] for hit in result.hits)
            except ProviderError as exc:
                error = exc.code.value
            local_ms = (time.perf_counter() - started) * 1000
            embedding_ms = replay.duration.get(query.text, 0) if allowed else 0
            observation = Observation(
                query.id,
                query.language,
                query.permission_group,
                query.relevant_ids,
                query.allowed_ids,
                found,
                local_ms + embedding_ms,
                error,
            )
            rows.append(
                {
                    **asdict(observation),
                    "mode": mode,
                    "local_and_rerank_ms": local_ms,
                    "shared_embedding_ms": embedding_ms,
                    "text_group": query_group_id(query.text),
                }
            )
            save_json(
                output / f"{phase}-partial.json", {"runs": runs, "active": name, "rows": rows}
            )
        observations = [
            Observation(**{key: row[key] for key in Observation.__dataclass_fields__})
            for row in rows
        ]
        summary = summarize(observations, seed=SEED, bootstrap_samples=1000)
        runs.append(
            {
                "name": name,
                "config": asdict(config),
                "summary": summary,
                "mode_counts": dict(Counter(row["mode"] for row in rows)),
                "observations": rows,
            }
        )
        log("retrieval_complete", phase=phase, arm=name, n=len(rows), modes=runs[-1]["mode_counts"])
    return runs


def quality(run: dict[str, Any], metric: str) -> float:
    return float(run["summary"]["overall"]["primary"][metric]["value"])


def choose_fusion(runs: list[dict[str, Any]]) -> dict[str, Any]:
    # Quality first; prefer simpler vector on exact metric ties.
    return max(
        runs,
        key=lambda run: (
            quality(run, "recall_at_5"),
            quality(run, "hit_at_5"),
            quality(run, "mrr_at_5"),
            run["name"] == "vector",
            -run["summary"]["overall"]["latency_ms"]["p50"]["value"],
        ),
    )


def guard_dev(output: Path) -> None:
    """A frozen development result cannot be overwritten by a later invocation."""
    if (output / "test-claim.json").exists():
        raise RunStopped("test already claimed; further tuning is forbidden")
    if (output / "recommendation.json").exists():
        raise RunStopped("dev recommendation already frozen; use a new authorized run")


def required_calls(data: Dataset, phase: str) -> dict[str, int]:
    if phase == "dev":
        return {"embedding": 50 + len(data.queries), "rerank": 2 * len(data.queries)}
    n = len(paired_knee_queries(data))
    return {
        "embedding": 100 + len(data.queries) + 10 * n,
        "rerank": len(data.queries) + 5 * n,
        "chat": 15 * n,
    }


def validate_partition(data: Dataset, output: Path, phase: str) -> None:
    manifest = read_provenance(output / "split-manifest.json")
    partition_hash = hashlib.sha256(canonical_json(asdict(data))).hexdigest()
    if manifest.get(f"{phase}_dataset_sha256") != partition_hash:
        raise RunStopped("dataset partition does not match frozen split provenance")


def dev(
    data: Dataset,
    provider: EmbeddingProvider,
    reranker: RerankProvider,
    output: Path,
    ledger: Ledger,
) -> None:
    guard_dev(output)
    validate_partition(data, output, "dev")
    ledger.phase = "dev"
    ledger.preflight(required_calls(data, "dev"))
    snapshots = build(data, provider)
    replay = QueryReplay(provider)
    fusion = retrieval(data, snapshots, grid(), replay, reranker, output, "dev-fusion")
    save_json(output / "dev-fusion.json", {"notice": NOTICE, "runs": fusion})
    selected = choose_fusion(fusion)
    base = SearchConfig(**selected["config"])
    thresholds = {"ungated": base}
    # Evaluation-only grid: serving defaults and historical artifacts are unchanged.
    for step in range(20, 71, 5):
        value = step / 100
        thresholds[f"cosine_{value}"] = replace(base, min_vector_similarity=value)
    for value in (0.01, 0.025, 0.05, 0.1, 0.15, 0.2):
        thresholds[f"margin_{value}"] = replace(base, min_vector_margin=value)
    curves = retrieval(data, snapshots, thresholds, replay, reranker, output, "dev-threshold")
    save_json(output / "dev-threshold.json", {"notice": NOTICE, "runs": curves})
    acceptable = [
        run
        for run in curves
        if all(
            quality(run, metric) >= quality(selected, metric) - 0.01
            for metric in ("recall_at_5", "hit_at_5", "mrr_at_5")
        )
    ]
    chosen = min(
        acceptable,
        key=lambda run: (
            run["summary"]["overall"]["empty"]["false_recommendation_rate"]["value"],
            -quality(run, "recall_at_5"),
            run["name"] != "ungated",
        ),
    )
    ranked = retrieval(
        data,
        snapshots,
        {
            "vector_rerank": replace(grid()["vector"], rerank_enabled=True),
            "rrf_rerank": replace(grid()["rrf"], rerank_enabled=True),
        },
        replay,
        reranker,
        output,
        "dev-rerank",
    )
    save_json(output / "dev-rerank.json", {"notice": NOTICE, "runs": ranked})
    recommendation = output / "recommendation.json"
    finished = utc_now()
    split_hash = file_sha256(output / "split-manifest.json")
    model_path = output / "model-config.json"
    model_hash = file_sha256(model_path) if model_path.exists() else None
    save_json(
        recommendation,
        {
            "notice": NOTICE,
            "chosen_fusion_arm": selected["name"],
            "chosen_threshold_arm": chosen["name"],
            "config": chosen["config"],
            "rerank_default": False,
            "service_defaults_changed": False,
            "selection_rule": (
                "Max primary-cohort Recall@5 then Hit@5 and MRR; "
                "exact quality ties prefer pure vector. "
                "Min empty false recommendation with <=1pp loss in each quality metric. "
                "Fixed threshold grid; no test inspection. Latency breaks remaining fusion ties."
            ),
            "dev_finished_at": finished,
            "split_manifest_sha256": split_hash,
            "model_config_sha256": model_hash,
        },
    )
    save_json(
        output / "freeze-boundary.json",
        {
            "dev_finished_at": finished,
            "split_manifest_sha256": split_hash,
            "recommendation_sha256": file_sha256(recommendation),
            "model_config_sha256": model_hash,
            "external_time_attestation": False,
            "next_step": (
                "Externally capture recommendation.json, split-manifest.json, model-config.json "
                "and this boundary "
                "before any test invocation. Pass the capture reference via --freeze-evidence-ref. "
                "The runner cannot authenticate the reference or attest to external chronology."
            ),
        },
    )
    log("dev_frozen", fusion=selected["name"], threshold=chosen["name"])


def knee(
    data: Dataset,
    provider: EmbeddingProvider,
    reranker: RerankProvider,
    selector: Selector,
    config: SearchConfig,
    output: Path,
) -> None:
    queries = paired_knee_queries(data)
    records, id_map = _records(data.tools, False, gateway_override="synthetic-knee")
    full = _snapshot(records, provider, batch_size=10, timeout=TIMEOUT)
    assert full.vectors is not None
    by_external = {external: i for i, external in enumerate(id_map.values())}
    rows = []
    for size in (20, 50, 100, 200, 500):
        for query in queries:
            text = f"Synthetic namespace: {query.gateway_id}. {query.text}"
            truth = by_external[query.relevant_ids[0]]
            distractors = [i for i in range(len(records)) if i != truth]
            rng = random.Random(f"{SEED}:{query.id}")  # nosec B311
            rng.shuffle(distractors)
            chosen = [truth, *distractors[: size - 1]]
            rng.shuffle(chosen)
            catalog = tuple(records[i] for i in chosen)
            snapshot = IndexSnapshot.build(
                catalog[0].identity.gateway,
                catalog,
                vectors=VectorIndex(
                    full.vectors.vectors[chosen], list(range(size)), size, full.vectors.dimension
                ),
                model_profile=full.model_profile,
            )
            allowed = frozenset(tool.tool_id for tool in catalog)
            arms = ["full_load", "search_first", "search_rerank"]
            random.Random(f"{SEED}:{size}:{query.id}:arms").shuffle(arms)  # nosec B311
            for order, arm in enumerate(arms):
                candidates: Sequence[ToolRecord] = catalog
                started = time.perf_counter()
                search_ms = 0.0
                search_mode = "not_applicable"
                error = None
                selected_id = None
                usage: dict[str, int] = {}
                try:
                    if arm != "full_load":
                        engine = SearchEngine(
                            snapshot,
                            provider,
                            reranker,
                            replace(config, rerank_enabled=arm == "search_rerank"),
                        )
                        found = engine.search(text, allowed, _context(2 * TIMEOUT + 5))
                        candidates = tuple(hit.tool for hit in found.hits)
                        search_mode = found.ranking_mode
                        search_ms = (time.perf_counter() - started) * 1000
                    result = selector.select(text, candidates, _context(TIMEOUT))
                    selected_id, error, usage = result.selected_id, result.error, dict(result.usage)
                except ProviderError as exc:
                    error = exc.code.value
                    usage = dict(exc.usage)
                rows.append(
                    {
                        "tool_count": size,
                        "arm": arm,
                        "arm_order": order,
                        "query_id": query.id,
                        "language": query.language,
                        "correct": selected_id == records[truth].tool_id and error is None,
                        "selected_id": id_map.get(selected_id) if selected_id else None,
                        "expected_id": query.relevant_ids[0],
                        "candidate_ids": [id_map[tool.tool_id] for tool in candidates],
                        "candidate_count": len(candidates),
                        "search_mode": search_mode,
                        "error": error,
                        "latency_ms": (time.perf_counter() - started) * 1000,
                        "search_ms": search_ms,
                        "usage": usage,
                    }
                )
                save_json(
                    output / "test-knee.json",
                    {
                        "notice": NOTICE,
                        "complete": False,
                        "query_ids": [q.id for q in queries],
                        "observations": rows,
                    },
                )
            if len(rows) % 15 == 0:
                log("knee_progress", size=size, completed=len(rows))
    value = json.loads((output / "test-knee.json").read_text())
    value["complete"] = True
    value["config"] = asdict(config)
    value["protocol"] = (
        "Same held-out unique singleton texts across all sizes and arms; deterministic nested "
        "distractors contain truth. Arm order randomized per pair. Namespaces disambiguate "
        "duplicate functions. Live search embedding and optional rerank included in latency, "
        "document index build excluded. Empty candidate sets skip chat. No tools executed."
    )
    save_json(output / "test-knee.json", value)


def test(
    data: Dataset,
    provider: EmbeddingProvider,
    reranker: RerankProvider,
    selector: Selector,
    output: Path,
    ledger: Ledger,
    *,
    freeze_evidence_ref: str | None = None,
    smoke: bool = False,
) -> None:
    recommendation = output / "recommendation.json"
    validate_test_provenance(
        output, recommendation, freeze_evidence_ref=freeze_evidence_ref, smoke=smoke
    )
    validate_partition(data, output, "test")
    config = SearchConfig(**read_provenance(recommendation)["config"])
    ledger.phase = "test"
    ledger.preflight(required_calls(data, "test"))
    claim_test(output, recommendation, freeze_evidence_ref=freeze_evidence_ref, smoke=smoke)
    snapshots = build(data, provider)
    runs = retrieval(
        data,
        snapshots,
        {
            "current_rrf": grid()["rrf"],
            "vector_ungated": replace(
                config,
                fusion_strategy="vector",
                min_vector_similarity=None,
                min_vector_margin=None,
                rerank_enabled=False,
            ),
            "recommended": config,
            "recommended_rerank": replace(config, rerank_enabled=True),
        },
        provider,
        reranker,
        output,
        "test-retrieval",
    )
    save_json(output / "test-retrieval.json", {"notice": NOTICE, "runs": runs})
    ledger.phase = "test-knee"
    knee(data, provider, reranker, selector, config, output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("dev", "test", "smoke"), required=True)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--embedding-model")
    parser.add_argument("--chat-model")
    parser.add_argument("--rerank-model")
    parser.add_argument("--embedding-dimension", type=int, default=1024)
    parser.add_argument("--rerank-path", help="Verified path relative to the configured base URL")
    parser.add_argument(
        "--prices-json",
        type=Path,
        help="Optional JSON object: model role to [input, output] CNY per million tokens",
    )
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports/tuning")
    parser.add_argument(
        "--freeze-evidence-ref",
        help="External capture reference for frozen dev files; not authenticated by this runner",
    )
    args = parser.parse_args()
    output = args.output_dir
    if (
        not args.real
        and args.phase == "smoke"
        and (
            output.resolve() == (ROOT / "reports/tuning").resolve()
            or (output.exists() and any(output.iterdir()))
        )
    ):
        log("stopped", reason="smoke_requires_fresh_separate_output")
        return 1
    ledger: Ledger | None = None
    execution_started = False
    try:
        # All local refusals precede credential reads and preserve completed evidence.
        if args.real:
            if os.environ.get("SEMANTIC_EVAL_REAL") != "1" or args.phase == "smoke":
                raise RunStopped("real mode requires explicit dual switches")
            for role in ("embedding", "rerank", "chat"):
                model = getattr(args, role + "_model")
                if not isinstance(model, str) or not model.strip():
                    raise RunStopped(f"real mode requires --{role}-model")
                ModelProfile(model, "tuning-v1")
            if not args.rerank_path:
                raise RunStopped("real mode requires a verified --rerank-path")
            RerankCodec(args.rerank_path)
            ModelProfile(args.embedding_model, "tuning-v1", args.embedding_dimension)
        elif args.phase != "smoke":
            raise RunStopped("mock mode must use smoke and separate output")
        if args.phase in ("dev", "smoke"):
            guard_dev(output)
        else:
            validate_test_provenance(
                output,
                output / "recommendation.json",
                freeze_evidence_ref=args.freeze_evidence_ref,
            )
            SearchConfig(**read_provenance(output / "recommendation.json")["config"])
        models = {
            role: getattr(args, role + "_model") if args.real else "mock-" + role
            for role in ("embedding", "rerank", "chat")
        }
        model_configuration = {
            "models": models,
            "revision": "tuning-v1" if args.real else "mock-v1",
            "embedding_dimension": (
                args.embedding_dimension
                if args.real
                else MockEmbeddingProvider().describe().dimension
            ),
            "paths": {
                "embedding": "/embeddings",
                "chat": "/chat/completions",
                "rerank": args.rerank_path if args.real else None,
            },
        }
        model_path = output / "model-config.json"
        if model_path.exists():
            stored_configuration = read_provenance(model_path)
            stored_configuration.pop("endpoint_hmac_sha256", None)
            if stored_configuration != model_configuration:
                raise RunStopped("frozen model configuration mismatch")
        elif args.phase == "test":
            raise RunStopped("missing frozen model configuration")
        data = load_dataset(ROOT / "data/synthetic-v1.json")
        if args.phase == "smoke":
            # Use a small, truly disjoint mock split; never relabel dev queries as test.
            data = replace(data, queries=paired_knee_queries(data, count=12))
        development, heldout, manifest = split_dataset(data)
        manifest_path = output / "split-manifest.json"
        if manifest_path.exists() and read_provenance(manifest_path) != manifest:
            raise RunStopped("frozen split mismatch")
        prices = None if args.prices_json is None else read_provenance(args.prices_json)
        ledger = Ledger(output, models=models, prices=prices)
        ledger.phase = "test" if args.phase == "test" else "dev"
        required = required_calls(heldout if args.phase == "test" else development, ledger.phase)
        ledger.preflight(required, persist=False)
        if args.real:
            logging.disable(logging.CRITICAL)
            credentials = load_credentials()
            host = urlsplit(credentials.base_url).hostname
            assert host is not None
            transport = OpenAICompatibleConfig(
                credentials.base_url,
                (host,),
                timeout_seconds=TIMEOUT,
                max_response_bytes=4_000_000,
                auth_header_name="Authorization",
            )
            endpoint_hash = endpoint_fingerprint(
                transport.base_url,
                ROOT / ".local/tuning-endpoint-salt",
                create=not model_path.exists(),
            )
            model_configuration["endpoint_hmac_sha256"] = endpoint_hash
            if model_path.exists() and read_provenance(model_path) != model_configuration:
                raise RunStopped("frozen endpoint configuration mismatch")
        if not model_path.exists():
            save_json(model_path, model_configuration)
        ledger.preflight(required)
        if not manifest_path.exists():
            save_json(manifest_path, manifest)
        execution_started = True
        if not args.real:
            mock = MockEmbeddingProvider()
            dev(development, mock, MockRerankProvider(), output, ledger)
            test(
                heldout,
                mock,
                MockRerankProvider(),
                MockSelector(),
                output,
                ledger,
                smoke=True,
            )
        else:
            with (
                httpx.Client(
                    trust_env=False,
                    event_hooks={"request": [ledger.request], "response": [ledger.response]},
                ) as client,
                ledger.observe(),
            ):
                embedding = DocumentCache(
                    OpenAIEmbeddingProvider(
                        client,
                        transport,
                        EmbeddingConfig(
                            ModelProfile(
                                args.embedding_model,
                                "tuning-v1",
                                args.embedding_dimension,
                                config_version=endpoint_hash,
                            ),
                            "/embeddings",
                            10,
                            4096,
                        ),
                        credential=credentials.api_key,
                    ),
                    ROOT / ".local/tuning-document-vectors",
                )
                reranker = HTTPRerankProvider(
                    client,
                    transport,
                    RerankConfig(
                        ModelProfile(args.rerank_model, "tuning-v1", config_version=endpoint_hash),
                        RerankCodec(args.rerank_path),
                    ),
                    credential=credentials.api_key,
                )
                selector = OpenAISelector(
                    client,
                    transport,
                    ChatConfig(
                        ModelProfile(args.chat_model, "tuning-v1", config_version=endpoint_hash),
                        "/chat/completions",
                        max_text_bytes=2_000_000,
                        max_tokens=256,
                        json_mode=False,
                    ),
                    credential=credentials.api_key,
                )
                if args.phase == "dev":
                    dev(development, embedding, reranker, output, ledger)
                else:
                    test(
                        heldout,
                        embedding,
                        reranker,
                        selector,
                        output,
                        ledger,
                        freeze_evidence_ref=args.freeze_evidence_ref,
                    )
        save_json(output / "run-status.json", {"status": "completed", "phase": args.phase})
        return 0
    except CredentialsExpired:
        if execution_started:
            save_json(
                output / "run-status.json", {"status": "credentials_expired", "phase": args.phase}
            )
        log("stopped", reason="ExpiredToken")
        return 20
    except (RunStopped, RealModeUnavailable, ProviderError) as exc:
        reason = exc.code.value if isinstance(exc, ProviderError) else type(exc).__name__
        if execution_started:
            save_json(
                output / "run-status.json",
                {"status": "stopped", "phase": args.phase, "reason": reason},
            )
        log("stopped", reason=reason)
        return 1
    except Exception as exc:
        if execution_started:
            save_json(
                output / "run-status.json",
                {"status": "stopped", "phase": args.phase, "reason": type(exc).__name__},
            )
        log("stopped", reason=type(exc).__name__)
        return 1
    finally:
        if execution_started and ledger is not None:
            save_json(output / "usage-cost.json", ledger.summary())


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the complete synthetic workflow. Default operation has no network access."""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import httpx

from semantic_core.openai_compatible import (
    ChatConfig,
    EmbeddingConfig,
    OpenAICompatibleConfig,
    OpenAIEmbeddingProvider,
)
from semantic_core.providers import ModelProfile, ProviderError
from semantic_core.rerank import HTTPRerankProvider, RerankCodec, RerankConfig
from semantic_eval.dataset import Dataset, EvalQuery, load_dataset
from semantic_eval.harness import CallBudget, evaluate, run_knee
from semantic_eval.reporting import render_knee_markdown, render_markdown
from semantic_eval.runtime import CredentialsExpired, RealModeUnavailable, load_credentials
from semantic_eval.selection import MockSelector, OpenAISelector

ROOT = Path(__file__).resolve().parents[1]
KNEE_SIZES = (20, 50, 100, 200, 500)


def subset(data: Dataset, limit: int | None) -> Dataset:
    """Deterministic round-robin strata; no inspection of retrieval results."""
    if limit is None or limit >= len(data.queries):
        return data
    if limit < 1:
        raise ValueError("query limit must be positive")
    groups: dict[tuple[str, str, int], list[EvalQuery]] = defaultdict(list)
    for query in data.queries:
        count = len(set(query.relevant_ids) & set(query.allowed_ids))
        groups[(query.language, query.permission_group, min(count, 6))].append(query)
    chosen: list[EvalQuery] = []
    while len(chosen) < limit:
        for key in sorted(groups):
            if groups[key] and len(chosen) < limit:
                chosen.append(groups[key].pop(0))
    return replace(data, queries=tuple(chosen))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--mode", choices=("mock", "real"), default="mock")
    result.add_argument("--dataset", type=Path, default=ROOT / "data" / "synthetic-v1.json")
    result.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / ".artifacts" / "offline-current",
        help="Output directory (default: repository .artifacts/offline-current).",
    )
    result.add_argument("--query-limit", type=int)
    result.add_argument("--queries-per-size", type=int, default=10)
    result.add_argument("--bootstrap-samples", type=int, default=1000)
    result.add_argument("--skip-knee", action="store_true")
    result.add_argument("--allow-host", action="append", default=[])
    result.add_argument("--embedding-model", default="text-embedding-v4")
    result.add_argument("--embedding-dimension", type=int, default=1024)
    result.add_argument("--embedding-path", default="/embeddings")
    result.add_argument("--embedding-batch-size", type=int, default=10)
    result.add_argument("--chat-model", default="qwen-plus")
    result.add_argument("--chat-path", default="/chat/completions")
    result.add_argument("--model-revision", default="configured-eval-v1")
    result.add_argument("--timeout", type=float, default=30.0)
    result.add_argument("--chat-max-tokens", type=int, default=256)
    result.add_argument("--chat-max-request-bytes", type=int, default=2_000_000)
    result.add_argument("--enable-rerank", action="store_true")
    result.add_argument("--rerank-model", default="qwen3-rerank")
    result.add_argument("--rerank-path")
    result.add_argument(
        "--rerank-score-field", choices=("relevance_score", "score"), default="relevance_score"
    )
    result.add_argument("--max-model-calls", type=int, default=2000)
    return result


def write_report(output: Path, name: str, report: dict[str, Any], markdown: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{name}.json").write_text(
        json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / f"{name}.md").write_text(markdown, encoding="utf-8")


def checkpointed(
    output: Path,
    phase: str,
    accounting: CallBudget,
    operation: Callable[..., dict[str, Any]],
    *arguments: Any,
    **options: Any,
) -> dict[str, Any]:
    """Persist completed observations and the latest counters before propagating failure."""
    partial: dict[str, Any] = {}

    def save(report: dict[str, Any], *, status: str = "running", reason: str | None = None) -> None:
        nonlocal partial
        partial = report
        document = {
            **report,
            "synthetic": True,
            "quality_acceptance": False,
            "phase": phase,
            "status": status,
            "reason": reason,
            "provider_accounting": asdict(accounting),
        }
        output.mkdir(parents=True, exist_ok=True)
        # A failed write must not truncate the previous valid checkpoint.
        temporary = output / "evaluation-partial.json.tmp"
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output / "evaluation-partial.json")

    save(partial)
    try:
        report = operation(*arguments, checkpoint=save, **options)
    except (Exception, KeyboardInterrupt) as error:
        reason = str(error) if isinstance(error, ProviderError) else "evaluation interrupted"
        save(partial, status="failed", reason=reason)
        raise
    save(report, status="complete")
    return report


def planned_model_calls(
    data: Dataset,
    *,
    query_count: int,
    batch_size: int,
    enable_rerank: bool,
    knee_queries: int,
) -> dict[str, int]:
    """Conservative attempted-call counts before reading any credentials.

    Evaluation builds both summary-only and up-to-six-example indexes. Each
    non-BM25 ablation embeds its queries, and reranking adds one call per query
    in each of the two index variants. Knee has two chat arms but only one
    query-embedding arm, so five sizes cost 5*q embeddings plus 10*q chats.
    """
    build_calls = sum(
        math.ceil(sum(tool.gateway_id == gateway for tool in data.tools) * factor / batch_size)
        for gateway in {tool.gateway_id for tool in data.tools}
        for factor in (1, 7)
    )
    calls = {
        "embedding": build_calls + query_count * (6 if enable_rerank else 4),
        "rerank": 2 * query_count if enable_rerank else 0,
        "chat": 0,
    }
    if knee_queries:
        calls["embedding"] += (
            math.ceil(len(data.tools) / batch_size) + len(KNEE_SIZES) * knee_queries
        )
        calls["chat"] += len(KNEE_SIZES) * 2 * knee_queries
    return calls


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    data = load_dataset(args.dataset)
    real = args.mode == "real"
    if real and os.environ.get("SEMANTIC_EVAL_REAL") != "1":
        raise SystemExit("Real mode requires SEMANTIC_EVAL_REAL=1 and --mode real.")
    if not 1 <= args.queries_per_size <= 50:
        raise SystemExit("Each tool-count size must use 1..50 queries.")
    if (
        args.bootstrap_samples < 1
        or args.embedding_batch_size < 1
        or not math.isfinite(args.timeout)
        or args.timeout <= 0
    ):
        raise SystemExit("Bootstrap count, batch size and timeout must be positive.")
    selected = subset(
        data, args.query_limit if args.query_limit is not None else (100 if real else None)
    )
    budget = CallBudget(limit=args.max_model_calls if real else None)
    if real:
        if not args.allow_host:
            raise SystemExit("Real mode requires an explicit --allow-host.")
        if args.enable_rerank and not args.rerank_path:
            raise SystemExit("Real rerank requires an explicitly verified --rerank-path.")
        planned_calls = planned_model_calls(
            data,
            query_count=len(selected.queries),
            batch_size=args.embedding_batch_size,
            enable_rerank=args.enable_rerank,
            knee_queries=0 if args.skip_knee else args.queries_per_size,
        )
        if sum(planned_calls.values()) > args.max_model_calls:
            raise SystemExit(
                "Requested run exceeds call budget; reduce --query-limit or --queries-per-size."
            )
        try:
            credentials = load_credentials()
        except CredentialsExpired:
            write_report(
                args.output_dir,
                "real-mode-status",
                {"status": "stopped", "reason": "AWS credentials expired", "model_calls": 0},
                "# Real mode stopped\n\nAWS credentials expired. No model calls made.\n",
            )
            raise
        except RealModeUnavailable:
            write_report(
                args.output_dir,
                "real-mode-status",
                {
                    "status": "skipped",
                    "reason": "specified AWS prerequisites unavailable",
                    "model_calls": 0,
                },
                "# Real mode skipped\n\n"
                "Specified AWS prerequisites unavailable. No model calls made.\n",
            )
            print("Real mode skipped: specified AWS prerequisites unavailable.")
            return 0
        transport = OpenAICompatibleConfig(
            base_url=credentials.base_url,
            allowed_hosts=tuple(args.allow_host),
            auth_header_name="Authorization",
            timeout_seconds=args.timeout,
            max_response_bytes=4_000_000,
        )
        with httpx.Client(trust_env=False) as client:
            embedding = OpenAIEmbeddingProvider(
                client,
                transport,
                EmbeddingConfig(
                    ModelProfile(
                        args.embedding_model, args.model_revision, args.embedding_dimension
                    ),
                    args.embedding_path,
                    args.embedding_batch_size,
                    4096,
                ),
                credential=credentials.api_key,
            )
            reranker = (
                HTTPRerankProvider(
                    client,
                    transport,
                    RerankConfig(
                        profile=ModelProfile(args.rerank_model, args.model_revision),
                        codec=RerankCodec(
                            path=args.rerank_path, score_field=args.rerank_score_field
                        ),
                    ),
                    credential=credentials.api_key,
                )
                if args.enable_rerank
                else None
            )
            selector = OpenAISelector(
                client,
                transport,
                ChatConfig(
                    ModelProfile(args.chat_model, args.model_revision),
                    args.chat_path,
                    args.chat_max_request_bytes,
                    max_tokens=args.chat_max_tokens,
                    json_mode=False,
                ),
                credential=credentials.api_key,
            )
            report = checkpointed(
                args.output_dir,
                "real-synthetic",
                budget,
                evaluate,
                selected,
                embedding=embedding,
                reranker=reranker,
                real=True,
                enable_rerank=args.enable_rerank,
                budget=budget,
                batch_size=args.embedding_batch_size,
                timeout=args.timeout,
                bootstrap_samples=args.bootstrap_samples,
            )
            write_report(args.output_dir, "real-synthetic", report, render_markdown(report))
            if not args.skip_knee:
                knee = checkpointed(
                    args.output_dir,
                    "real-knee",
                    budget,
                    run_knee,
                    data,
                    selector,
                    embedding=embedding,
                    budget=budget,
                    sizes=KNEE_SIZES,
                    queries_per_size=args.queries_per_size,
                    batch_size=args.embedding_batch_size,
                    timeout=args.timeout,
                    bootstrap_samples=args.bootstrap_samples,
                )
                knee["mode"] = "real"
                knee["model"] = args.chat_model
                knee["selector_profile"] = {
                    "model_id": args.chat_model,
                    "revision": args.model_revision,
                    "temperature": 0,
                    "max_tokens": args.chat_max_tokens,
                }
                write_report(args.output_dir, "real-knee", knee, render_knee_markdown(knee))
    else:
        report = checkpointed(
            args.output_dir,
            "offline-regression",
            budget,
            evaluate,
            selected,
            budget=budget,
            bootstrap_samples=args.bootstrap_samples,
        )
        write_report(args.output_dir, "offline-regression", report, render_markdown(report))
        if not args.skip_knee:
            knee = checkpointed(
                args.output_dir,
                "offline-knee",
                budget,
                run_knee,
                data,
                MockSelector(),
                budget=budget,
                sizes=KNEE_SIZES,
                queries_per_size=args.queries_per_size,
                bootstrap_samples=args.bootstrap_samples,
            )
            knee["mode"] = "mock"
            knee["model"] = "deterministic_mock"
            knee["selector_profile"] = {
                "model_id": "mock-llm",
                "revision": "fixture-v1",
                "selection_rule": "deterministic lexical overlap fixture",
            }
            write_report(args.output_dir, "offline-knee", knee, render_knee_markdown(knee))
    # Keep the last complete checkpoint if rendering or publishing either final
    # report fails. Remove it only after every requested report was written.
    (args.output_dir / "evaluation-partial.json").unlink(missing_ok=True)
    print("Synthetic reports written. Quality acceptance and production rerank remain deferred.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProviderError as error:
        raise SystemExit(f"Evaluation stopped: provider {error.code.value}.") from None

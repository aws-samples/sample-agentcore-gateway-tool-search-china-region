"""External evaluation configuration and numeric usage observer; no library edits.

From the worktree root:
SEMANTIC_EVAL_REAL=1 AWS_REGION=cn-north-1 AWS_DEFAULT_REGION=cn-north-1 \
PYTHONPATH=src .venv/bin/python reports/real/run_reference.py --mode real \
    --output-dir .local/reference-rerun
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import os
import sys
import time
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from semantic_core._http import _HTTPProvider
from semantic_core.openai_compatible import (
    ChatConfig, EmbeddingConfig, OpenAICompatibleConfig, OpenAIEmbeddingProvider,
)
from semantic_core.providers import ModelProfile, ProviderError, RequestContext
from semantic_eval import runtime

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "reports" / "real"
TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens")
DETAIL_KEYS = ("cached_tokens", "reasoning_tokens", "cache_creation_input_tokens")
STATE = {"phase": "preflight", "http_embedding_attempts": 0, "http_chat_attempts": 0}
RECORDS = []


class CredentialsExpired(BaseException):
    pass


class ExistingEvidence(BaseException):
    pass


def save(name, value):
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def log(event, **values):
    line = json.dumps({"event": event, **values}, ensure_ascii=False, allow_nan=False)
    print(line, flush=True)
    with (OUTPUT / "run.log").open("a") as stream:
        stream.write(line + "\n")


def observe_request(request):
    payload = json.loads(request.content)
    kind = "embedding" if "input" in payload else "chat"
    if kind == "embedding" and STATE["http_embedding_attempts"] >= 2000:
        raise RuntimeError("request ceiling")
    STATE[f"http_{kind}_attempts"] += 1
    row = {
        "sequence": len(RECORDS) + 1, "phase": STATE["phase"], "kind": kind,
        "requested_model": "text-embedding-v4" if kind == "embedding" else "qwen-plus",
        "input_count": len(payload.get("input", [])) if kind == "embedding" else None,
        "tool_count": len(payload.get("tools", [])) if kind == "chat" else None,
        "request_bytes": len(request.content), "requested_dimensions": payload.get("dimensions"),
        "status": None, "usage": {},
    }
    RECORDS.append(row)
    request.extensions["reference_row"] = row


def observe_response(response):
    # Do not consume the response or bypass the core's streaming/size/deadline guards.
    response.request.extensions["reference_row"]["status"] = response.status_code


def numeric_usage(body, row):
    usage = body.get("usage")
    if isinstance(usage, dict):
        row["usage"] = {key: usage[key] for key in TOKEN_KEYS
                        if type(usage.get(key)) is int and usage[key] >= 0}
        details = {}
        for group in ("prompt_tokens_details", "completion_tokens_details"):
            raw = usage.get(group)
            if isinstance(raw, dict):
                details[group] = {key: raw[key] for key in DETAIL_KEYS
                                  if type(raw.get(key)) is int and raw[key] >= 0}
        row["usage_details"] = details
    row["model_echo_matches_request"] = body.get("model") == row["requested_model"]
    if row["kind"] == "embedding":
        data = body.get("data")
        if isinstance(data, list):
            row["returned_vectors"] = len(data)
            row["returned_dimensions"] = sorted({
                len(item["embedding"]) for item in data
                if isinstance(item, dict) and isinstance(item.get("embedding"), list)
            })
    else:
        choices = body.get("choices")
        if isinstance(choices, list):
            row["finish_reasons"] = [
                item.get("finish_reason")
                if item.get("finish_reason") in ("stop", "tool_calls", "length", "content_filter")
                else "other" for item in choices if isinstance(item, dict)
            ]


def main():
    global OUTPUT
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("real",), required=True)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument("--prior-ledger", type=Path)
    parser.add_argument("--query-limit", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    OUTPUT = args.output_dir.resolve()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if os.environ.get("SEMANTIC_EVAL_REAL") != "1":
        raise SystemExit("Real mode requires SEMANTIC_EVAL_REAL=1.")
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        os.environ[name] = "cn-north-1"
    if (OUTPUT / "usage-ledger.jsonl").exists():
        raise ExistingEvidence()
    prior_attempts = 0
    if args.prior_ledger is not None:
        prior = [json.loads(line) for line in args.prior_ledger.read_text().splitlines()]
        prior_status_path = args.prior_ledger.parent / "run-status.json"
        if prior and prior_status_path.exists():
            prior_status = json.loads(prior_status_path.read_text())
            if (prior_status.get("status") == "stopped"
                    and prior_status.get("reason") == "timeout" and not prior[-1]["usage"]):
                prior[-1]["transport_error"] = "timeout"
                prior[-1]["error_source"] = "incomplete run status"
        for row in prior:
            row["phase"] = "incomplete_" + row["phase"]
            RECORDS.append(row)
            STATE[f"http_{row['kind']}_attempts"] += 1
        prior_attempts = len(prior)
        (OUTPUT / "usage-ledger.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in RECORDS)
        )
    logging.disable(logging.CRITICAL)
    original_subprocess_run = runtime.subprocess.run

    def observed_aws(*arguments, **kwargs):
        result = original_subprocess_run(*arguments, **kwargs)
        if "ExpiredToken" in (result.stderr or ""):
            raise CredentialsExpired()
        return result

    runtime.subprocess.run = observed_aws
    credentials = runtime.load_credentials()
    runtime.subprocess.run = original_subprocess_run
    log("aws_prerequisites", status="available", credential_material_logged=False)
    spec = importlib.util.spec_from_file_location("reference_cli", ROOT / "scripts/run_evaluation.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    original_client = httpx.Client
    original_post = _HTTPProvider._post

    class ObservedClient(original_client):
        def __init__(self, *args, **kwargs):
            kwargs["event_hooks"] = {"request": [observe_request], "response": [observe_response]}
            super().__init__(*args, **kwargs)

    def observed_post(self, *args, **kwargs):
        before = len(RECORDS)
        started = time.perf_counter()
        try:
            body = original_post(self, *args, **kwargs)
            if len(RECORDS) > before:
                numeric_usage(body, RECORDS[-1])
            return body
        except ProviderError as error:
            if len(RECORDS) > before:
                RECORDS[-1]["transport_error"] = error.code.value
            raise
        finally:
            for row in RECORDS[before:]:
                row["http_ms"] = (time.perf_counter() - started) * 1000
                with (OUTPUT / "usage-ledger.jsonl").open("a") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                if row["sequence"] % 25 == 0 or row["status"] != 200:
                    log("http_progress", sequence=row["sequence"], phase=STATE["phase"],
                        kind=row["kind"], status=row["status"])

    cli.httpx.Client = ObservedClient
    _HTTPProvider._post = observed_post
    cli.load_credentials = lambda: credentials
    cli.ChatConfig = partial(ChatConfig, extra_body={"enable_thinking": False})
    for function_name in ("evaluate", "run_knee"):
        original_function = getattr(cli, function_name)

        def phase_wrapper(*args, _original=original_function, _phase=function_name, **kwargs):
            STATE["phase"] = _phase
            log("phase_started", phase=_phase)
            result = _original(*args, **kwargs)
            log("phase_completed", phase=_phase)
            return result

        setattr(cli, function_name, phase_wrapper)
    hostname = urlsplit(credentials.base_url).hostname
    dimension = 1024
    transport = OpenAICompatibleConfig(
        credentials.base_url, (hostname,), auth_header_name="Authorization",
        timeout_seconds=args.timeout, max_response_bytes=4_000_000,
    )
    STATE["phase"] = "configuration_probe"
    with ObservedClient(trust_env=False) as client:
        for attempt in range(2):
            provider = OpenAIEmbeddingProvider(
                client, transport,
                EmbeddingConfig(ModelProfile("text-embedding-v4", "configured-eval-v1", dimension),
                                "/embeddings", 10, 4096),
                credential=credentials.api_key,
            )
            try:
                result = provider.embed(["合成评测配置检查"], "document",
                    RequestContext("synthetic-configuration", time.monotonic() + args.timeout))
                log("embedding_configuration", dimensions=dimension, batch_size=10,
                    probe_usage=dict(result.usage))
                break
            except ProviderError:
                actual = RECORDS[-1].get("returned_dimensions", []) if RECORDS else []
                if attempt == 0 and len(actual) == 1 and actual[0] != dimension:
                    dimension = actual[0]
                    log("configuration_adjusted", embedding_dimensions=dimension)
                    continue
                raise
    save("run-config.json", {
        "synthetic": True, "quality_acceptance": False,
        "rerank_enablement_basis": False, "rerank_enabled": False,
        "dataset_file_sha256": hashlib.sha256((ROOT / "data/synthetic-v1.json").read_bytes()).hexdigest(),
        "query_limit": args.query_limit, "queries_per_size": 30,
        "tool_sizes": [20, 50, 100, 200, 500],
        "bootstrap_samples": 1000, "embedding_model": "text-embedding-v4",
        "embedding_dimension": dimension, "embedding_batch_size": 10,
        "embedding_send_dimensions": True, "embedding_encoding_format": "float",
        "chat_model": "qwen-plus", "chat_enable_thinking": False, "chat_max_tokens": 256,
        "timeout_seconds": args.timeout,
        "allow_host": "approved secret host; retained in memory only",
        "max_embedding_rerank_requests_including_probes": 2000,
        "prior_attempts_in_global_ledger": prior_attempts,
        "harness_max_model_calls": 1990 - prior_attempts,
        "conservative_harness_request_bound": 600 + 4 * args.query_limit,
        "examples": "versioned offline mock examples; no live query generation",
        "usage_observer": "numeric provider usage after bounded transport, before adapter validation",
    })
    result = cli.main([
        "--mode", "real", "--allow-host", hostname,
        "--embedding-model", "text-embedding-v4", "--embedding-dimension", str(dimension),
        "--embedding-batch-size", "10", "--chat-model", "qwen-plus",
        "--query-limit", str(args.query_limit), "--queries-per-size", "30",
        "--timeout", str(args.timeout),
        "--max-model-calls", str(1990 - prior_attempts), "--output-dir", str(OUTPUT),
    ])
    save("run-status.json", {"status": "completed", "exit_code": result,
                            "http_attempts": {k: v for k, v in STATE.items() if k != "phase"}})
    log("run_completed", embedding_http_attempts=STATE["http_embedding_attempts"],
        chat_http_attempts=STATE["http_chat_attempts"], rerank_http_attempts=0)


if __name__ == "__main__":
    OUTPUT.mkdir(parents=True, exist_ok=True)
    try:
        main()
    except ExistingEvidence:
        print("Use a clean output directory; existing measured evidence was not changed.")
        sys.exit(2)
    except CredentialsExpired:
        save("run-status.json", {"status": "credentials_expired", "phase": STATE["phase"]})
        log("run_stopped", reason="ExpiredToken", phase=STATE["phase"])
        sys.exit(20)
    except (Exception, SystemExit) as error:
        code = error.code.value if isinstance(error, ProviderError) else type(error).__name__
        save("run-status.json", {"status": "stopped", "reason": code, "phase": STATE["phase"]})
        log("run_stopped", reason=code, phase=STATE["phase"])
        sys.exit(1)

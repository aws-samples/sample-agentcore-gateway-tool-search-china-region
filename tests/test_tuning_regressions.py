"""Red-first accounting and interrupted-evaluation regressions use offline fixtures."""

from __future__ import annotations

import importlib.util
import json
import subprocess  # nosec B404
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from semantic_core.catalog import ToolRecord
from semantic_core.openai_compatible import (
    ChatConfig,
    EmbeddingConfig,
    OpenAIChatProvider,
    OpenAICompatibleConfig,
    OpenAIEmbeddingProvider,
)
from semantic_core.providers import (
    ErrorCode,
    MockEmbeddingProvider,
    ModelProfile,
    ProviderError,
    RequestContext,
)
from semantic_eval import harness, runtime
from semantic_eval.dataset import generate_dataset
from semantic_eval.selection import OpenAISelector, SelectionResult

TRANSPORT = OpenAICompatibleConfig("https://offline.example", ("offline.example",))
PROFILE = ModelProfile("fixture-chat", "fixture-v1")


def context() -> RequestContext:
    return RequestContext("tuning-regression", time.monotonic() + 10)


def tool() -> ToolRecord:
    return harness._records(generate_dataset().tools[:1], False)[0][0]


@pytest.mark.parametrize("phase", ["sts", "secret"])
def test_expired_credentials_are_distinct_safe_and_never_retried(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if phase == "secret" and len(calls) == 1:
            # CompletedProcess only constructs a mock result; it starts no process.
            # nosemgrep: dangerous-subprocess-use-audit
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"Account": "a", "Arn": "b", "UserId": "c"}), ""
            )
        # CompletedProcess only constructs a mock result; it starts no process.
        # nosemgrep: dangerous-subprocess-use-audit
        return subprocess.CompletedProcess(
            command, 255, "private secret", "An error occurred (ExpiredToken): private key"
        )

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(runtime.RealModeUnavailable) as caught:
        runtime.load_credentials()
    assert type(caught.value).__name__ == "CredentialsExpired"
    assert str(caught.value) == "AWS credentials expired; refresh the configured profile"
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert len(calls) == (1 if phase == "sts" else 2)


@pytest.mark.parametrize("malformation", ["selection", "identity"])
def test_selector_validation_error_retains_reported_usage(malformation: str) -> None:
    payload = {
        "model": "unexpected" if malformation == "identity" else PROFILE.model_id,
        "choices": [],
        "usage": {"prompt_tokens": 100, "completion_tokens": 9, "total_tokens": 109},
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        selector = OpenAISelector(
            client, TRANSPORT, ChatConfig(PROFILE, "/chat/completions", 65536, json_mode=False)
        )
        with pytest.raises(ProviderError) as caught:
            selector.select("Find a tool", (tool(),), context())
    assert caught.value.code is ErrorCode.INVALID_RESPONSE
    assert getattr(caught.value, "usage", {}) == payload["usage"]
    assert str(caught.value) == "provider invalid_response"


@pytest.mark.parametrize("malformation", ["vectors", "identity"])
def test_embedding_validation_error_retains_reported_usage(malformation: str) -> None:
    profile = ModelProfile("fixture-embedding", "fixture-v1", 2)
    payload = {
        "model": "unexpected" if malformation == "identity" else profile.model_id,
        "data": [{"index": 0, "embedding": [0, 0]}],
        "usage": {"input_tokens": 12, "total_tokens": 12},
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        provider = OpenAIEmbeddingProvider(
            client, TRANSPORT, EmbeddingConfig(profile, "/embeddings", 10, 1024)
        )
        with pytest.raises(ProviderError) as caught:
            provider.embed(["text"], "query", context())
    assert getattr(caught.value, "usage", {}) == payload["usage"]


@pytest.mark.parametrize("second_failure", ["schema", "envelope", "http", "identity"])
def test_chat_generation_failure_keeps_usage_from_all_received_attempts(
    second_failure: str,
) -> None:
    requests: list[httpx.Request] = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 2 and second_failure == "http":
            return httpx.Response(503, json={"error": "private body"})
        reply: dict[str, object] = {
            "model": "unexpected"
            if len(requests) == 2 and second_failure == "identity"
            else PROFILE.model_id,
            "choices": [{"finish_reason": "stop", "message": {"content": "invalid JSON"}}],
            "usage": {"total_tokens": 17 if len(requests) == 1 else 23},
        }
        if len(requests) == 2 and second_failure == "envelope":
            reply["choices"] = []
        return httpx.Response(200, json=reply)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIChatProvider(
            client, TRANSPORT, ChatConfig(PROFILE, "/chat/completions", 65536)
        )
        with pytest.raises(ProviderError) as caught:
            provider.generate_queries("Find a tool", ("en",), 1, context())
    assert len(requests) == 2
    assert getattr(caught.value, "usage", {}) == {
        "total_tokens": 17 if second_failure == "http" else 40
    }


def test_knee_chat_attempts_consume_budget_and_preserve_failed_usage() -> None:
    data = generate_dataset()
    query = next(q for q in data.queries if len(q.relevant_ids) == 1)
    data = replace(data, queries=(query,))
    attempts: list[str] = []

    class Selector:
        def select(
            self, query: str, tools: Sequence[ToolRecord], request: RequestContext
        ) -> SelectionResult:
            attempts.append(query)
            return SelectionResult(None, {"total_tokens": 7}, usage_source="provider")

    budget = harness.CallBudget(limit=None)
    result = harness.run_knee(
        data, Selector(), sizes=(20,), queries_per_size=1, budget=budget, bootstrap_samples=5
    )
    assert len(attempts) == 2
    assert result["provider_accounting"]["calls"].get("chat") == 2
    assert result["provider_accounting"]["usage"].get("chat") == {"total_tokens": 14}


def test_metered_embedding_error_accounts_usage() -> None:
    payload = {"data": [], "usage": {"total_tokens": 11}}
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        provider = OpenAIEmbeddingProvider(
            client,
            TRANSPORT,
            EmbeddingConfig(ModelProfile("embedding", "v1", 2), "/embeddings", 10, 1024),
        )
        budget = harness.CallBudget(limit=1)
        with pytest.raises(ProviderError):
            harness.MeteredEmbedding(provider, budget).embed(["text"], "query", context())
    assert budget.calls == {"embedding": 1}
    assert budget.usage == {"embedding": {"total_tokens": 11}}
    assert budget.errors == {"embedding_invalid_response": 1}


def load_cli() -> ModuleType:
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_evaluation.py"
    spec = importlib.util.spec_from_file_location("tuning_cli_fixture", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_failure_persists_partial_observations_and_latest_accounting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cli = load_cli()
    monkeypatch.setattr(cli, "load_dataset", lambda _: generate_dataset())

    def fail(data: object, **kwargs: Any) -> dict[str, Any]:
        budget = kwargs["budget"]
        budget.before("embedding")
        partial = {"runs": [{"observations": [{"query_id": "completed-fixture"}]}]}
        callback = kwargs.get("checkpoint")
        if callback is not None:
            callback(partial)
        budget.before("embedding")
        budget.after("embedding", {"total_tokens": 17})
        raise ProviderError(ErrorCode.INVALID_RESPONSE)

    monkeypatch.setattr(cli, "evaluate", fail)
    with pytest.raises(ProviderError):
        cli.main(["--output-dir", str(tmp_path), "--skip-knee"])
    output = tmp_path / "evaluation-partial.json"
    assert output.exists(), "interrupted evaluation must retain its partial results"
    report = json.loads(output.read_text())
    assert report["status"] == "failed"
    assert report["reason"] == "provider invalid_response"
    assert report["runs"][0]["observations"][0]["query_id"] == "completed-fixture"
    assert report["provider_accounting"]["calls"] == {"embedding": 2}
    assert report["provider_accounting"]["usage"] == {"embedding": {"total_tokens": 17}}


def test_evaluate_checkpoints_completed_queries_before_later_failure() -> None:
    data = generate_dataset()
    data = replace(data, tools=data.tools[:1], queries=data.queries[:1])
    snapshots: list[dict[str, Any]] = []
    # First build and vector query succeed. A later build exhausts the budget;
    # completed query rows must already be available to a file checkpoint.
    with pytest.raises(ProviderError):
        harness.evaluate(
            data,
            embedding=MockEmbeddingProvider(),
            budget=harness.CallBudget(limit=2),
            enable_rerank=False,
            bootstrap_samples=5,
            checkpoint=lambda report: snapshots.append(json.loads(json.dumps(report))),
        )
    assert any(row.get("runs") for row in snapshots)
    assert any(
        observation["query_id"] == data.queries[0].id
        for report in snapshots
        for run in report.get("runs", [])
        for observation in run.get("observations", [])
    )


def test_knee_does_not_label_failed_retrieval_usage_as_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = generate_dataset()
    query = next(q for q in data.queries if len(q.relevant_ids) == 1)
    data = replace(data, queries=(query,))

    class Selector:
        def select(
            self, query: str, tools: Sequence[ToolRecord], request: RequestContext
        ) -> SelectionResult:
            return SelectionResult(None, {"total_tokens": 7}, usage_source="provider")

    def failed_retrieval(*args: object, **kwargs: object) -> tuple[tuple[str, ...], str]:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, usage={"total_tokens": 11})

    monkeypatch.setattr(harness, "_retrieve", failed_retrieval)
    result = harness.run_knee(
        data, Selector(), sizes=(20,), queries_per_size=1, bootstrap_samples=5
    )
    search = next(row for row in result["runs"] if row["arm"] == "search_first")
    assert search["observations"][0]["usage"] == {}
    assert search["observations"][0]["usage_source"] == "unavailable"


def test_failed_chat_reports_usage_and_budget_blocks_additional_attempts() -> None:
    attempts: list[str] = []

    class Selector:
        def select(
            self, query: str, tools: Sequence[ToolRecord], request: RequestContext
        ) -> SelectionResult:
            attempts.append(query)
            raise ProviderError(ErrorCode.INVALID_RESPONSE, usage={"total_tokens": 13})

    budget = harness.CallBudget(limit=1)
    selector = harness.MeteredSelector(Selector(), budget)
    with pytest.raises(ProviderError) as first:
        selector.select("query", (tool(),), context())
    assert first.value.usage == {"total_tokens": 13}
    with pytest.raises(ProviderError):
        selector.select("second query", (tool(),), context())
    assert attempts == ["query"]
    assert budget.calls == {"chat": 1}
    assert budget.usage == {"chat": {"total_tokens": 13}}
    assert budget.errors == {"chat_invalid_response": 1, "budget_exhausted": 1}

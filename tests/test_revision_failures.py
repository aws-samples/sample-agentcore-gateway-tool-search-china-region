"""Offline regressions for index integrity, credentials and authorization boundaries."""

from __future__ import annotations

import importlib.util
import json
import subprocess  # nosec B404
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from semantic_core.openai_compatible import OpenAICompatibleConfig
from semantic_core.providers import (
    ErrorCode,
    ModelProfile,
    ProviderError,
    RequestContext,
    RerankDocument,
)
from semantic_core.rerank import HTTPRerankProvider, RerankCodec, RerankConfig
from semantic_eval import runtime
from semantic_eval.dataset import Dataset, generate_dataset
from semantic_eval.harness import CallBudget, MeteredRerank


def load_cli() -> ModuleType:
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_evaluation.py"
    spec = importlib.util.spec_from_file_location("revision_evaluation_cli", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def data() -> Dataset:
    return generate_dataset()


@pytest.mark.parametrize(
    "malformation",
    ["identity", "envelope", "length", "object", "index", "score", "duplicate", "order"],
)
@pytest.mark.parametrize("envelope", ["flat", "bailian-native"])
def test_rerank_all_response_failures_keep_usage_and_stop_at_budget(
    malformation: str, envelope: str
) -> None:
    rows: list[object] = [
        {"index": 0, "relevance_score": 0.9},
        {"index": 1, "relevance_score": 0.3},
    ]
    if malformation == "length":
        rows.pop()
    elif malformation == "object":
        rows[0] = None
    elif malformation == "index":
        rows[0] = {"index": -1, "relevance_score": 0.9}
    elif malformation == "score":
        rows[0] = {"index": 0, "relevance_score": "private invalid score"}
    elif malformation == "duplicate":
        rows[1] = {"index": 0, "relevance_score": 0.3}
    elif malformation == "order":
        rows.reverse()
    result_body: dict[str, object] = {"results": {} if malformation == "envelope" else rows}
    payload = {
        "model": "wrong-model" if malformation == "identity" else "fixture-reranker",
        "usage": {"prompt_tokens": 17, "total_tokens": 17},
        **({"output": result_body} if envelope == "bailian-native" else result_body),
    }
    attempts = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def respond(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(200, json=payload)

    codec = RerankCodec(envelope=envelope)  # type: ignore[arg-type]
    documents = (RerankDocument("first", "a"), RerankDocument("second", "b"))
    budget = CallBudget(limit=1)
    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        provider = MeteredRerank(
            HTTPRerankProvider(
                client,
                OpenAICompatibleConfig("https://offline.example", ("offline.example",)),
                RerankConfig(ModelProfile("fixture-reranker", "fixture-v1"), codec),
            ),
            budget,
        )
        with pytest.raises(ProviderError) as first:
            provider.rerank(
                "query", documents, 2, RequestContext("revision", time.monotonic() + 10)
            )
        assert first.value.code is ErrorCode.INVALID_RESPONSE
        assert dict(first.value.usage) == payload["usage"]
        assert str(first.value) == "provider invalid_response"
        with pytest.raises(ProviderError):
            provider.rerank(
                "query", documents, 2, RequestContext("revision", time.monotonic() + 10)
            )
    assert len(attempts) == 1
    assert budget.calls == {"rerank": 1}
    assert budget.usage == {"rerank": payload["usage"]}
    assert budget.errors == {"rerank_invalid_response": 1, "budget_exhausted": 1}


@pytest.mark.parametrize(
    "code", ["ExpiredToken", "ExpiredTokenException", "RequestExpired", "TokenRefreshRequired"]
)
@pytest.mark.parametrize("phase", ["sts", "secret"])
def test_expired_variants_stop_at_each_credential_stage_without_exposing_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: str,
    phase: str,
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert "--region" in command and "cn-north-1" in command
        if phase == "secret" and len(calls) == 1:
            # CompletedProcess only constructs a mock result; it starts no process.
            # nosemgrep: dangerous-subprocess-use-audit
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"Account": "a", "Arn": "b", "UserId": "c"}), ""
            )
        # CompletedProcess only constructs a mock result; it starts no process.
        # nosemgrep: dangerous-subprocess-use-audit
        return subprocess.CompletedProcess(
            command, 255, "private fixture stdout", f"An error occurred ({code}): private stderr"
        )

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(runtime.CredentialsExpired) as caught:
        runtime.load_credentials()
    assert str(caught.value) == "AWS credentials expired; refresh the configured profile"
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert len(calls) == (1 if phase == "sts" else 2)
    assert "private" not in repr(caught.value)
    assert capsys.readouterr() == ("", "")


def prepare_cli(cli: ModuleType, data: Dataset, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("offline revision tests must never load credentials or open HTTP")

    monkeypatch.setattr(cli, "load_dataset", lambda _: data)
    monkeypatch.setattr(cli, "load_credentials", forbidden)
    monkeypatch.setattr(cli.httpx, "Client", forbidden)

    def completed(*args: object, **kwargs: Any) -> dict[str, Any]:
        report = {"runs": [{"observations": [{"query_id": "completed-fixture"}]}]}
        kwargs["budget"].before("embedding")
        kwargs["checkpoint"](report)
        return report

    monkeypatch.setattr(cli, "evaluate", completed)
    monkeypatch.setattr(cli, "run_knee", completed)
    monkeypatch.setattr(cli, "render_markdown", lambda _: "completed retrieval\n")
    monkeypatch.setattr(cli, "render_knee_markdown", lambda _: "completed knee\n")


@pytest.mark.parametrize("skip_knee", [False, True])
def test_success_removes_partial_only_after_all_reports_are_published(
    data: Dataset, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, skip_knee: bool
) -> None:
    cli = load_cli()
    prepare_cli(cli, data, monkeypatch)
    arguments = ["--output-dir", str(tmp_path)]
    if skip_knee:
        arguments.append("--skip-knee")
    assert cli.main(arguments) == 0
    assert (tmp_path / "offline-regression.json").exists()
    assert (tmp_path / "offline-regression.md").exists()
    assert (tmp_path / "offline-knee.json").exists() is not skip_knee
    assert not (tmp_path / "evaluation-partial.json").exists()
    assert not (tmp_path / "evaluation-partial.json.tmp").exists()


@pytest.mark.parametrize(
    "filename",
    [
        "offline-regression.json",
        "offline-regression.md",
        "offline-knee.json",
        "offline-knee.md",
    ],
)
def test_final_report_write_failure_preserves_last_complete_checkpoint(
    data: Dataset, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, filename: str
) -> None:
    cli = load_cli()
    prepare_cli(cli, data, monkeypatch)
    original = Path.write_text

    def fail_write(path: Path, *args: Any, **kwargs: Any) -> int:
        if path.name == filename:
            raise OSError("fixture report write failed")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_write)
    with pytest.raises(OSError, match="fixture report write failed"):
        cli.main(["--output-dir", str(tmp_path)])
    partial = json.loads((tmp_path / "evaluation-partial.json").read_text())
    assert partial["status"] == "complete"
    assert partial["runs"][0]["observations"][0]["query_id"] == "completed-fixture"
    assert partial["provider_accounting"]["calls"]["embedding"] == (2 if "knee" in filename else 1)


@pytest.mark.parametrize("enable_rerank", [False, True])
def test_preflight_counts_query_embedding_separately_from_two_knee_chat_arms(
    data: Dataset, enable_rerank: bool
) -> None:
    cli = load_cli()
    base = cli.planned_model_calls(
        data, query_count=3, batch_size=10, enable_rerank=enable_rerank, knee_queries=0
    )
    knee = cli.planned_model_calls(
        data, query_count=3, batch_size=10, enable_rerank=enable_rerank, knee_queries=2
    )
    assert base["chat"] == 0
    assert base["rerank"] == (6 if enable_rerank else 0)
    assert knee["embedding"] - base["embedding"] == 50 + 5 * 2
    assert knee["chat"] - base["chat"] == 5 * 2 * 2
    assert knee["rerank"] == base["rerank"]
    assert sum(knee.values()) - sum(base.values()) == 50 + 15 * 2

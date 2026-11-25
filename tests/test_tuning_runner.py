"""Runner guard tests use isolated directories and never invoke a model."""

from __future__ import annotations

import importlib.util
import json
import sys
from functools import partial
from pathlib import Path
from types import ModuleType

import pytest

from semantic_core.providers import MockEmbeddingProvider, MockRerankProvider
from semantic_eval.dataset import generate_dataset
from semantic_eval.tuning_runtime import Ledger as RuntimeLedger
from semantic_eval.tuning_runtime import RunStopped

Ledger = partial(
    RuntimeLedger,
    models={
        "embedding": "example-embedding-model",
        "rerank": "example-rerank-model",
        "chat": "example-chat-model",
    },
    prices={"embedding": (0.5, 0.0), "rerank": (0.5, 0.0), "chat": (0.8, 2.0)},
)


@pytest.fixture
def runner() -> ModuleType:
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_tuning.py"
    spec = importlib.util.spec_from_file_location("tuning_runner_guard_fixture", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dev_refuses_test_claim_before_preflight_or_model_work(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "test-claim.json").write_text('{"recommendation_sha256":"fixture"}')
    budget = Ledger(tmp_path)

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("claimed test output must stop all later dev work")

    monkeypatch.setattr(budget, "preflight", unexpected)
    monkeypatch.setattr(runner, "build", unexpected)
    with pytest.raises(RunStopped, match="further tuning is forbidden"):
        runner.dev(
            generate_dataset(),
            MockEmbeddingProvider(),
            MockRerankProvider(),
            tmp_path,
            budget,
        )
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "accounting-config.json",
        "test-claim.json",
    ]


@pytest.mark.parametrize("default_directory", [False, True])
def test_smoke_refuses_existing_real_evidence_and_reserved_output(
    runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    default_directory: bool,
) -> None:
    root = tmp_path / "isolated-project"
    output = root / "reports" / "tuning" if default_directory else tmp_path / "real-results"
    monkeypatch.setattr(runner, "ROOT", root)
    phases: list[str] = []
    monkeypatch.setattr(runner, "dev", lambda *args: phases.append("dev"))
    monkeypatch.setattr(runner, "test", lambda *args: phases.append("test"))
    monkeypatch.setattr(runner, "load_dataset", lambda _: generate_dataset())

    def no_credentials() -> None:
        pytest.fail("smoke must never load credentials")

    monkeypatch.setattr(runner, "load_credentials", no_credentials)
    if not default_directory:
        prior = Ledger(output)
        prior.start("embedding", 10)
        (output / "run-status.json").write_text(json.dumps({"status": "completed", "phase": "dev"}))
        (output / "recommendation.json").write_text('{"frozen_before_test":true}')
        (output / "usage-cost.json").write_text('{"prior_real_accounting":true}')
    before = {path.name: path.read_bytes() for path in output.iterdir()} if output.exists() else {}
    arguments = ["run_tuning.py", "--phase", "smoke"]
    if not default_directory:
        arguments.extend(["--output-dir", str(output)])
    monkeypatch.setattr(sys, "argv", arguments)

    assert runner.main() == 1
    assert phases == [], "smoke must refuse before dev/test could overwrite real artifacts"
    after = {path.name: path.read_bytes() for path in output.iterdir()} if output.exists() else {}
    assert after == before, "refusing smoke must preserve all real evidence byte-for-byte"

"""Future tuning provenance and refusal paths use local fixtures only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import ModuleType

import pytest

from semantic_core.catalog import canonical_json
from semantic_core.providers import MockEmbeddingProvider, MockRerankProvider
from semantic_core.search import SearchConfig
from semantic_eval.dataset import generate_dataset
from semantic_eval.selection import MockSelector
from semantic_eval.tuning import split_dataset
from semantic_eval.tuning_runtime import Ledger as RuntimeLedger
from semantic_eval.tuning_runtime import RunStopped, claim_test, endpoint_fingerprint, save_json

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
    path = Path(__file__).resolve().parents[1] / "scripts/run_tuning.py"
    spec = importlib.util.spec_from_file_location("tuning_provenance_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def frozen_files(output: Path, manifest: object | None = None) -> Path:
    save_json(output / "split-manifest.json", manifest or {"source": "fixture"})
    finished = "2026-01-01T00:00:00Z"
    recommendation = output / "recommendation.json"
    split_hash = hashlib.sha256((output / "split-manifest.json").read_bytes()).hexdigest()
    save_json(
        recommendation,
        {
            "config": asdict(SearchConfig(fusion_strategy="vector", min_vector_similarity=0.6)),
            "dev_finished_at": finished,
            "split_manifest_sha256": split_hash,
        },
    )
    save_json(
        output / "freeze-boundary.json",
        {
            "dev_finished_at": finished,
            "split_manifest_sha256": split_hash,
            "recommendation_sha256": hashlib.sha256(recommendation.read_bytes()).hexdigest(),
            "external_time_attestation": False,
        },
    )
    return recommendation


def contents(output: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in output.iterdir()}


def test_ledger_future_attempts_have_utc_start_and_completion(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    before = datetime.now(UTC)
    row = ledger.start("embedding", 1)
    assert row.get("started_at"), "HTTP attempt must persist a UTC start timestamp"
    assert row.get("completed_at") is None
    ledger.finish(row, {"usage": {"total_tokens": 1}}, 2.0)
    after = datetime.now(UTC)
    started = datetime.fromisoformat(row["started_at"])
    completed = datetime.fromisoformat(row["completed_at"])
    assert started.utcoffset() == completed.utcoffset() == before.utcoffset()
    assert before <= started <= completed <= after


@pytest.mark.parametrize("corruption", ["unknown_kind", "duplicate_sequence", "missing_usage"])
def test_invalid_persisted_ledger_fails_closed(tmp_path: Path, corruption: str) -> None:
    ledger = Ledger(tmp_path)
    ledger.start("embedding", 1)
    rows = [json.loads(line) for line in ledger.path.read_text().splitlines()]
    if corruption == "unknown_kind":
        rows[0]["kind"] = "embeddings"
    elif corruption == "duplicate_sequence":
        rows.append(rows[0])
    else:
        del rows[0]["usage"]
    ledger.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    before = ledger.path.read_bytes()
    with pytest.raises(RunStopped, match="ledger"):
        Ledger(tmp_path).preflight({"embedding": 1})
    assert ledger.path.read_bytes() == before
    assert not (tmp_path / "preflight-preflight.json").exists()


@pytest.mark.parametrize("missing", ["split-manifest.json", "freeze-boundary.json"])
def test_holdout_refuses_missing_provenance_without_writing_claim(
    tmp_path: Path, missing: str
) -> None:
    recommendation = frozen_files(tmp_path)
    (tmp_path / missing).unlink()
    before = contents(tmp_path)
    with pytest.raises(RunStopped, match="provenance"):
        claim_test(tmp_path, recommendation)
    assert contents(tmp_path) == before


@pytest.mark.parametrize("tampered", ["split-manifest.json", "recommendation.json"])
def test_holdout_refuses_changed_frozen_bytes_without_writing_claim(
    tmp_path: Path, tampered: str
) -> None:
    recommendation = frozen_files(tmp_path)
    path = tmp_path / tampered
    path.write_bytes(path.read_bytes() + b"\n")
    before = contents(tmp_path)
    with pytest.raises(RunStopped, match="provenance"):
        claim_test(tmp_path, recommendation)
    assert contents(tmp_path) == before


def test_holdout_requires_external_capture_reference(tmp_path: Path) -> None:
    recommendation = frozen_files(tmp_path)
    before = contents(tmp_path)
    with pytest.raises(RunStopped, match="external"):
        claim_test(tmp_path, recommendation)
    assert contents(tmp_path) == before


@pytest.mark.parametrize("phase", ["dev", "test"])
def test_refused_real_phase_preserves_completed_evidence_before_credentials(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    data = generate_dataset()
    _, _, manifest = split_dataset(data)
    frozen_files(tmp_path, manifest)
    save_json(tmp_path / "test-claim.json", {"recommendation_sha256": "historical"})
    save_json(tmp_path / "run-status.json", {"status": "completed", "phase": "test"})
    save_json(tmp_path / "usage-cost.json", {"historical_cost": "unknown"})
    before = contents(tmp_path)
    attempted: list[bool] = []
    monkeypatch.setattr(runner, "load_dataset", lambda _: data)
    monkeypatch.setattr(runner, "load_credentials", lambda: attempted.append(True))
    monkeypatch.setenv("SEMANTIC_EVAL_REAL", "1")
    refusals: list[str] = []
    guard_name = "guard_dev" if phase == "dev" else "validate_test_provenance"
    guard = getattr(runner, guard_name)

    def checked_guard(*args, **kwargs):
        try:
            return guard(*args, **kwargs)
        except RunStopped as error:
            refusals.append(str(error))
            raise

    monkeypatch.setattr(runner, guard_name, checked_guard)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_tuning.py",
            "--phase",
            phase,
            "--real",
            "--output-dir",
            str(tmp_path),
            "--embedding-model",
            "example-embedding-model",
            "--chat-model",
            "example-chat-model",
            "--rerank-model",
            "example-rerank-model",
            "--rerank-path",
            "/rerank",
        ],
    )
    assert runner.main() == 1
    assert attempted == [], "refusal must occur before loading any credentials"
    assert len(refusals) == 1
    assert ("further tuning is forbidden" if phase == "dev" else "already claimed") in refusals[0]
    assert contents(tmp_path) == before


def test_future_dev_freeze_records_split_hash_and_finer_grid(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = generate_dataset()
    dev, _, manifest = split_dataset(data)
    save_json(tmp_path / "split-manifest.json", manifest)
    captured: dict[str, dict[str, SearchConfig]] = {}
    monkeypatch.setattr(runner, "build", lambda *args: {})

    def retrieval(data, snapshots, configs, provider, reranker, output, phase):
        captured[phase] = configs
        return [
            {
                "name": name,
                "config": asdict(config),
                "summary": {
                    "overall": {
                        "primary": {
                            metric: {"value": 1.0}
                            for metric in ("recall_at_5", "hit_at_5", "mrr_at_5")
                        },
                        "empty": {"false_recommendation_rate": {"value": 1.0}},
                        "latency_ms": {"p50": {"value": 0.0}},
                    }
                },
            }
            for name, config in configs.items()
        ]

    monkeypatch.setattr(runner, "retrieval", retrieval)
    runner.dev(dev, MockEmbeddingProvider(), MockRerankProvider(), tmp_path, Ledger(tmp_path))
    selected = json.loads((tmp_path / "recommendation.json").read_text())
    assert selected.get("dev_finished_at"), "future recommendation needs the dev completion time"
    assert (
        selected.get("split_manifest_sha256")
        == hashlib.sha256((tmp_path / "split-manifest.json").read_bytes()).hexdigest()
    )
    assert "frozen_before_test" not in selected, "a constant boolean is not external proof"
    boundary = json.loads((tmp_path / "freeze-boundary.json").read_text())
    assert boundary["external_time_attestation"] is False
    assert (
        boundary["recommendation_sha256"]
        == hashlib.sha256((tmp_path / "recommendation.json").read_bytes()).hexdigest()
    )
    cosines = {
        config.min_vector_similarity
        for config in captured["dev-threshold"].values()
        if config.min_vector_similarity is not None
    }
    assert cosines == {value / 100 for value in range(20, 71, 5)}


def test_future_test_adds_ungated_vector_without_extra_query_embedding(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = generate_dataset()
    _, heldout, manifest = split_dataset(data)
    heldout = replace(
        heldout,
        queries=tuple(
            q
            for q in heldout.queries
            if len(q.relevant_ids) == 1 and set(q.relevant_ids) <= set(q.allowed_ids)
        )[:1],
    )
    manifest["test_dataset_sha256"] = hashlib.sha256(canonical_json(asdict(heldout))).hexdigest()
    frozen_files(tmp_path, manifest)
    configs_seen: dict[str, SearchConfig] = {}
    actual_retrieval = runner.retrieval
    monkeypatch.setattr(runner, "claim_test", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "knee", lambda *args: None)
    mock = MockEmbeddingProvider()
    query_calls: list[str] = []
    actual_embed = mock.embed

    def embed(texts, purpose, context):
        if purpose == "query":
            query_calls.extend(texts)
        return actual_embed(texts, purpose, context)

    monkeypatch.setattr(mock, "embed", embed)

    def retrieval(data, snapshots, configs, *args):
        configs_seen.update(configs)
        return actual_retrieval(data, snapshots, configs, *args)

    monkeypatch.setattr(runner, "retrieval", retrieval)
    runner.test(
        heldout,
        mock,
        MockRerankProvider(),
        MockSelector(),
        tmp_path,
        Ledger(tmp_path),
        freeze_evidence_ref="fixture-capture-reference",
    )
    assert "vector_ungated" in configs_seen
    ungated = configs_seen["vector_ungated"]
    assert ungated.fusion_strategy == "vector"
    assert ungated.min_vector_similarity is ungated.min_vector_margin is None
    assert ungated.rerank_enabled is False
    assert query_calls == [heldout.queries[0].text]


@pytest.mark.parametrize("phase", ["dev", "test"])
def test_wrong_partition_refuses_before_preflight_or_models(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    data = generate_dataset()
    development, heldout, manifest = split_dataset(data)
    if phase == "dev":
        save_json(tmp_path / "split-manifest.json", manifest)
    else:
        frozen_files(tmp_path, manifest)
    ledger = Ledger(tmp_path)
    before = contents(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("invalid split must refuse before preflight or model work")

    monkeypatch.setattr(ledger, "preflight", forbidden)
    monkeypatch.setattr(runner, "build", forbidden)
    with pytest.raises(RunStopped, match="partition"):
        if phase == "dev":
            runner.dev(heldout, MockEmbeddingProvider(), MockRerankProvider(), tmp_path, ledger)
        else:
            runner.test(
                development,
                MockEmbeddingProvider(),
                MockRerankProvider(),
                MockSelector(),
                tmp_path,
                ledger,
                freeze_evidence_ref="fixture-capture-reference",
            )
    assert contents(tmp_path) == before


def test_mock_smoke_preserves_disjoint_partition_boundary(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    queries: dict[str, set[str]] = {}
    monkeypatch.setattr(runner, "load_dataset", lambda _: generate_dataset())
    monkeypatch.setattr(
        runner, "dev", lambda data, *args: queries.update(dev={q.id for q in data.queries})
    )
    monkeypatch.setattr(
        runner,
        "test",
        lambda data, *args, **kwargs: queries.update(test={q.id for q in data.queries}),
    )
    monkeypatch.setattr(
        sys, "argv", ["run_tuning.py", "--phase", "smoke", "--output-dir", str(tmp_path)]
    )
    assert runner.main() == 0
    assert queries["dev"] and queries["test"]
    assert queries["dev"].isdisjoint(queries["test"])


@pytest.mark.parametrize(
    "override",
    [
        ["--embedding-dimension", "2"],
        ["--embedding-model", "another-embedding-model"],
        ["--chat-model", "another-chat-model"],
        ["--rerank-path", "/another-rerank"],
        [],
    ],
)
def test_changed_model_configuration_refuses_before_credentials_and_claim(
    runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    override: list[str],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    private_root = tmp_path_factory.mktemp("tuning-private")
    monkeypatch.setattr(runner, "ROOT", private_root)
    endpoint_hash = endpoint_fingerprint(
        "https://original.example.com/v1", private_root / ".local/tuning-endpoint-salt", create=True
    )
    data = generate_dataset()
    _, _, manifest = split_dataset(data)
    recommendation = frozen_files(tmp_path, manifest)
    model_path = tmp_path / "model-config.json"
    save_json(
        model_path,
        {
            "models": {
                "embedding": "example-embedding-model",
                "chat": "example-chat-model",
                "rerank": "example-rerank-model",
            },
            "revision": "tuning-v1",
            "endpoint_hmac_sha256": endpoint_hash,
            "embedding_dimension": 1024,
            "paths": {
                "embedding": "/embeddings",
                "chat": "/chat/completions",
                "rerank": "/rerank",
            },
        },
    )
    model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
    frozen = json.loads(recommendation.read_text())
    frozen["model_config_sha256"] = model_hash
    save_json(recommendation, frozen)
    boundary_path = tmp_path / "freeze-boundary.json"
    boundary = json.loads(boundary_path.read_text())
    boundary["model_config_sha256"] = model_hash
    boundary["recommendation_sha256"] = hashlib.sha256(recommendation.read_bytes()).hexdigest()
    save_json(boundary_path, boundary)
    RuntimeLedger(
        tmp_path,
        models={
            "embedding": "example-embedding-model",
            "chat": "example-chat-model",
            "rerank": "example-rerank-model",
        },
    )
    before = contents(tmp_path)
    refusals = []

    class ObservedStop(RunStopped):
        def __init__(self, message):
            refusals.append(message)
            super().__init__(message)

    monkeypatch.setattr(runner, "RunStopped", ObservedStop)
    if override:
        monkeypatch.setattr(runner, "load_credentials", lambda: pytest.fail("credentials touched"))
    else:
        from semantic_eval.runtime import Credentials

        monkeypatch.setattr(
            runner,
            "load_credentials",
            lambda: Credentials("https://changed.example.com/v1", "fixture-key"),
        )
        monkeypatch.setattr(runner, "load_dataset", lambda _: data)
        monkeypatch.setattr(runner.httpx, "Client", lambda **kw: pytest.fail("HTTP touched"))
    monkeypatch.setenv("SEMANTIC_EVAL_REAL", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_tuning.py",
            "--phase",
            "test",
            "--real",
            "--output-dir",
            str(tmp_path),
            "--embedding-model",
            "example-embedding-model",
            "--chat-model",
            "example-chat-model",
            "--rerank-model",
            "example-rerank-model",
            "--rerank-path",
            "/rerank",
            "--freeze-evidence-ref",
            "fixture-capture",
            *override,
        ],
    )
    assert runner.main() == 1
    assert refusals == [
        "frozen model configuration mismatch"
        if override
        else "frozen endpoint configuration mismatch"
    ]
    assert contents(tmp_path) == before


def test_model_configuration_bytes_are_bound_to_freeze(tmp_path: Path) -> None:
    recommendation = frozen_files(tmp_path)
    model_path = tmp_path / "model-config.json"
    save_json(model_path, {"embedding_dimension": 1024})
    # A newly added or replaced configuration cannot retroactively bind old evidence.
    with pytest.raises(RunStopped, match="model configuration provenance"):
        claim_test(tmp_path, recommendation, freeze_evidence_ref="fixture-capture")
    assert not (tmp_path / "test-claim.json").exists()

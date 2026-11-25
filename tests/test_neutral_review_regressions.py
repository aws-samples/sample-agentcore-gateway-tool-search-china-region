"""Offline regressions for independent review findings."""

import importlib.util
import json
import runpy
import sys
import time
from pathlib import Path

import httpx
import pytest

from semantic_core.providers import RequestContext
from semantic_eval.dataset import generate_dataset
from semantic_eval.runtime import Credentials
from semantic_eval.tuning_runtime import Ledger, RunStopped

ROOT = Path(__file__).resolve().parents[1]
MODELS = {role: "example-" + role + "-model" for role in ("embedding", "rerank", "chat")}


def load_module(relative):
    spec = importlib.util.spec_from_file_location("review_fixture", ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tuning_document_cache_separates_endpoint_vector_spaces(tmp_path, monkeypatch):
    runner = load_module("scripts/run_tuning.py")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    data = generate_dataset()
    monkeypatch.setattr(runner, "load_dataset", lambda _: data)
    endpoints = iter(("https://first.example.com/v1", "https://second.example.com/v1"))
    monkeypatch.setattr(
        runner, "load_credentials", lambda: Credentials(next(endpoints), "fixture-key")
    )
    observed = []
    client_type = httpx.Client

    def response(request):
        observed.append(request.url.host)
        vector = [1.0, 0.0] if len(observed) == 1 else [0.0, 1.0]
        return httpx.Response(
            200,
            json={
                "model": MODELS["embedding"],
                "data": [{"index": 0, "embedding": vector}],
                "usage": {"total_tokens": 1},
            },
        )

    monkeypatch.setattr(
        runner.httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(response), **kwargs),
    )
    vectors = []

    def development(data, provider, reranker, output, ledger):
        vectors.append(
            provider.embed(
                ["same document"],
                "document",
                RequestContext("cache-fixture", time.monotonic() + 10),
            ).vectors.tolist()
        )

    monkeypatch.setattr(runner, "dev", development)
    monkeypatch.setenv("SEMANTIC_EVAL_REAL", "1")
    for directory in ("first", "second"):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "run_tuning.py",
                "--phase",
                "dev",
                "--real",
                "--output-dir",
                str(tmp_path / directory),
                "--embedding-model",
                MODELS["embedding"],
                "--embedding-dimension",
                "2",
                "--rerank-model",
                MODELS["rerank"],
                "--rerank-path",
                "/rerank",
                "--chat-model",
                MODELS["chat"],
            ],
        )
        assert runner.main() == 0
    assert observed == ["first.example.com", "second.example.com"]
    assert vectors == [[[1.0, 0.0]], [[0.0, 1.0]]]


def test_reference_cost_requires_explicit_prices():
    module = runpy.run_path(str(ROOT / "reports/real/summarize_cost.py"))
    row = {
        "kind": "embedding",
        "requested_model": "arbitrary-model",
        "status": 200,
        "model_echo_matches_request": True,
        "usage": {"total_tokens": 1_000_000},
    }
    result = module["analyze"](row)
    assert result["cost"] is None
    assert "missing_price" in result["unknown_cost_reasons"]


@pytest.mark.parametrize(
    ("options", "expected"),
    [([], None), (["--prices-json", "zero.json"], 0.0), (["--prices-json", "prices.json"], 2.0)],
)
def test_reference_report_price_modes_match_rendered_output(
    tmp_path, monkeypatch, options, expected
):
    module = runpy.run_path(str(ROOT / "reports/real/summarize_cost.py"))
    row = {
        "sequence": 1,
        "phase": "evaluate",
        "kind": "embedding",
        "requested_model": "example-embedding-model",
        "status": 200,
        "model_echo_matches_request": True,
        "usage": {"total_tokens": 1_000_000},
    }
    (tmp_path / "usage-ledger.jsonl").write_text(json.dumps(row) + "\n")
    (tmp_path / "run-config.json").write_text("{}")
    (tmp_path / "zero.json").write_text('{"embedding": [0, 0]}')
    (tmp_path / "prices.json").write_text('{"embedding": [2, 0]}')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["summarize_cost.py", "--report-dir", str(tmp_path), *options])
    module["main"]()
    report = json.loads((tmp_path / "usage-cost.json").read_text())
    rendered = (tmp_path / "usage-cost.md").read_text()
    total = report["total"]
    assert total["unknown_cost_requests"] == (1 if expected is None else 0)
    assert total["standard_undiscounted_list_estimate_rmb"] == expected
    if expected is None:
        assert "费用未知" in rendered
        assert "人民币 0.00000000 元" not in rendered
    if expected == 2:
        assert "| embedding | 2 | 0 |" in rendered
        assert "| embedding | 0.5 |" not in rendered


@pytest.mark.parametrize(
    "relative", ["reports/real/summarize_cost.py", "reports/tuning/summarize_usage.py"]
)
def test_historical_pricing_option_is_not_accepted(relative, monkeypatch):
    monkeypatch.setattr(sys, "argv", [relative, "--historical-prices"])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(ROOT / relative), run_name="__main__")
    assert error.value.code == 2


@pytest.mark.parametrize("invalid", [[], [1], [float("nan"), 0], [-1, 0], [True, 0]])
def test_unused_invalid_prices_preserve_existing_reports(tmp_path, monkeypatch, invalid):
    module = runpy.run_path(str(ROOT / "reports/real/summarize_cost.py"))
    (tmp_path / "usage-ledger.jsonl").write_text("")
    (tmp_path / "run-config.json").write_text("{}")
    (tmp_path / "prices.json").write_text(json.dumps({"chat": invalid}))
    for name in ("usage-cost.json", "usage-cost.md"):
        (tmp_path / name).write_text("existing evidence")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "summarize_cost.py",
            "--report-dir",
            str(tmp_path),
            "--prices-json",
            str(tmp_path / "prices.json"),
        ],
    )
    with pytest.raises(ValueError, match="price"):
        module["main"]()
    for name in ("usage-cost.json", "usage-cost.md"):
        assert (tmp_path / name).read_text() == "existing evidence"
    audit_module = runpy.run_path(str(ROOT / "reports/tuning/summarize_usage.py"))
    with pytest.raises(ValueError, match="price"):
        audit_module["build_audit"]([], {"prices_cny_per_million_tokens": {"chat": invalid}})


def test_tuning_audit_missing_metadata_never_implies_historical_prices():
    module = runpy.run_path(str(ROOT / "reports/tuning/summarize_usage.py"))
    row = {
        "sequence": 1,
        "phase": "dev",
        "kind": "embedding",
        "model": "arbitrary-model",
        "complete_response": True,
        "usage": {"total_tokens": 1_000_000},
    }
    audit = module["build_audit"]([row], {})
    assert audit["known_public_list_price_cny"] == 0
    assert audit["unknown_cost_requests"] == 1


def test_tuning_markdown_uses_actual_rates(tmp_path):
    module = runpy.run_path(str(ROOT / "reports/tuning/summarize_usage.py"))
    audit = module["build_audit"](
        [],
        {"models": MODELS, "prices_cny_per_million_tokens": {"embedding": [2, 0]}},
    )
    audit["generated_at_utc"] = "fixture"
    rendered = module["render_markdown"](audit, tmp_path)
    assert "| embedding | example-embedding-model | 2 | 0 |" in rendered
    assert "¥0.50" not in rendered


@pytest.mark.parametrize("replacement", [None, {"embedding": [2, 0]}])
def test_ledger_resume_rejects_changed_price_basis(tmp_path, replacement):
    ledger = Ledger(tmp_path, models=MODELS, prices={"embedding": [1, 0]})
    row = ledger.start("embedding", 1)
    ledger.finish(row, {"usage": {"total_tokens": 1_000_000}}, 1)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(RunStopped, match="accounting"):
        Ledger(tmp_path, models=MODELS, prices=replacement)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
    assert (
        Ledger(tmp_path, models=MODELS, prices={"embedding": [1, 0]}).summary()[
            "public_list_price_cny"
        ]
        == 1
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["--embedding-model", " ", "--chat-model", "example-chat-model"],
        ["--embedding-model", "example-embedding-model", "--chat-model", " "],
        [
            "--embedding-model",
            "example-embedding-model",
            "--chat-model",
            "example-chat-model",
            "--embedding-dimension",
            "0",
        ],
    ],
)
def test_reference_config_refuses_before_credentials_or_output(tmp_path, monkeypatch, arguments):
    runner = load_module("reports/real/run_reference.py")
    monkeypatch.setenv("SEMANTIC_EVAL_REAL", "1")
    monkeypatch.setattr(
        runner.runtime, "load_credentials", lambda: pytest.fail("credentials touched")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_reference.py", "--mode", "real", "--output-dir", str(tmp_path / "new"), *arguments],
    )
    with pytest.raises((ValueError, SystemExit)):
        runner.main()
    assert not (tmp_path / "new").exists()

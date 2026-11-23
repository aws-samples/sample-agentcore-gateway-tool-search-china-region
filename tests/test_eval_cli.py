"""CLI wiring and opt-in boundaries; all AWS and provider calls are replaced."""

import importlib.util
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType

import pytest

from semantic_eval.dataset import Dataset, generate_dataset
from semantic_eval.runtime import Credentials, RealModeUnavailable
from semantic_eval.selection import MockSelector

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_evaluation.py"


@pytest.fixture
def cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location("evaluation_cli_fixture", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def data() -> Dataset:
    return generate_dataset()


def unavailable(*args, **kwargs):
    pytest.fail("credentials and HTTP must not be touched")


def test_default_embedding_batch_stays_within_observed_proxy_limit(cli: ModuleType) -> None:
    args = cli.parser().parse_args([])
    assert args.embedding_dimension == 1024
    assert args.embedding_batch_size == 10


@pytest.mark.parametrize(
    ("rerank", "expected"),
    [
        (False, {"embedding": 900, "rerank": 0, "chat": 100}),
        (True, {"embedding": 1100, "rerank": 200, "chat": 100}),
    ],
)
def test_observed_batch_limit_default_run_fits_existing_hard_budget(
    cli: ModuleType, data: Dataset, rerank: bool, expected: dict[str, int]
) -> None:
    args = cli.parser().parse_args([])
    selected = cli.subset(data, 100)
    planned = cli.planned_model_calls(
        data,
        query_count=len(selected.queries),
        batch_size=args.embedding_batch_size,
        enable_rerank=rerank,
        knee_queries=args.queries_per_size,
    )
    assert planned == expected
    assert sum(planned.values()) <= args.max_model_calls == 2000


def test_full_fixture_rerank_exceeds_default_budget_before_credentials(
    cli: ModuleType, data: Dataset, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prepare(cli, data, monkeypatch)
    monkeypatch.setenv("SEMANTIC_EVAL_REAL", "1")
    args = cli.parser().parse_args([])
    planned = cli.planned_model_calls(
        data,
        query_count=len(data.queries),
        batch_size=args.embedding_batch_size,
        enable_rerank=True,
        knee_queries=args.queries_per_size,
    )
    assert planned == {"embedding": 2540, "rerank": 680, "chat": 100}
    assert sum(planned.values()) == 3320 > args.max_model_calls
    with pytest.raises(SystemExit, match="call budget"):
        cli.main(
            [
                "--mode",
                "real",
                "--allow-host",
                "offline.example",
                "--query-limit",
                str(len(data.queries)),
                "--enable-rerank",
                "--rerank-path",
                "/rerank",
                "--output-dir",
                str(tmp_path),
            ]
        )
    assert not list(tmp_path.iterdir())


def prepare(cli, data, monkeypatch):
    monkeypatch.setattr(cli, "load_dataset", lambda _: data)
    monkeypatch.setattr(cli, "load_credentials", unavailable)
    monkeypatch.setattr(cli.httpx, "Client", unavailable)
    monkeypatch.setattr(cli, "render_markdown", lambda report: json.dumps(report))
    monkeypatch.setattr(cli, "render_knee_markdown", lambda report: json.dumps(report))
    calls = {}

    def evaluate(selected, **kwargs):
        calls["evaluate"] = (selected, kwargs)
        return {"synthetic": True, "quality_acceptance": False}

    def knee(selected, selector, **kwargs):
        calls["knee"] = (selected, selector, kwargs)
        return {"synthetic": True, "quality_acceptance": False}

    monkeypatch.setattr(cli, "evaluate", evaluate)
    monkeypatch.setattr(cli, "run_knee", knee)
    return calls


@pytest.mark.parametrize("explicit_output", [False, True], ids=["no-arguments", "explicit-output"])
def test_cli_preserves_frozen_reports_while_writing_current_results(
    cli: ModuleType,
    data: Dataset,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    explicit_output: bool,
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    frozen = {
        name: b"synthetic frozen report: synonyms-v1\n"
        for name in (
            "offline-regression.json",
            "offline-regression.md",
            "offline-knee.json",
            "offline-knee.md",
        )
    }
    for name, content in frozen.items():
        (reports / name).write_bytes(content)
    prepare(cli, data, monkeypatch)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    output = tmp_path / ("custom-results" if explicit_output else ".artifacts/offline-current")
    monkeypatch.setattr(
        sys, "argv", [str(SCRIPT), *(["--output-dir", str(output)] if explicit_output else [])]
    )

    assert cli.main() == 0

    assert {path.name for path in reports.iterdir()} == set(frozen)
    changed = [name for name, content in frozen.items() if (reports / name).read_bytes() != content]
    assert changed == [], "默认评测不得改写迁移前冻结基线"
    for name in ("offline-regression.md", "offline-knee.md"):
        assert b"synonyms-v1" in (reports / name).read_bytes()
    assert {path.name for path in output.iterdir()} == set(frozen)
    for name in ("offline-regression.json", "offline-knee.json"):
        report = json.loads((output / name).read_text())
        assert report["synthetic"] is True
        assert report["quality_acceptance"] is False
    if explicit_output:
        assert not (tmp_path / ".artifacts").exists()


@pytest.mark.parametrize("flag", [None, "1"])
def test_mock_default_never_loads_credentials_even_if_environment_is_enabled(
    cli: ModuleType,
    data: Dataset,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag: str | None,
) -> None:
    calls = prepare(cli, data, monkeypatch)
    if flag is None:
        monkeypatch.delenv("SEMANTIC_EVAL_REAL", raising=False)
    else:
        monkeypatch.setenv("SEMANTIC_EVAL_REAL", flag)
    assert cli.main(["--output-dir", str(tmp_path), "--queries-per-size", "3"]) == 0
    selected, kwargs = calls["evaluate"]
    assert selected == data
    assert kwargs["budget"].limit is None
    assert isinstance(calls["knee"][1], MockSelector)
    assert calls["knee"][2]["queries_per_size"] == 3
    assert (tmp_path / "offline-regression.json").exists()
    knee = json.loads((tmp_path / "offline-knee.json").read_text())
    assert knee["mode"] == "mock"
    assert knee["model"] == "deterministic_mock"
    assert knee["quality_acceptance"] is False


def test_real_mode_requires_both_switches_before_aws_or_http(
    cli: ModuleType,
    data: Dataset,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare(cli, data, monkeypatch)
    monkeypatch.delenv("SEMANTIC_EVAL_REAL", raising=False)
    with pytest.raises(SystemExit, match="SEMANTIC_EVAL_REAL"):
        cli.main(["--mode", "real", "--output-dir", str(tmp_path)])
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ([], "allow-host"),
        (["--allow-host", "offline.example", "--enable-rerank"], "rerank-path"),
        (["--allow-host", "offline.example", "--max-model-calls", "1"], "call budget"),
        (["--allow-host", "offline.example", "--queries-per-size", "51"], "1..50"),
    ],
)
def test_invalid_real_request_stops_before_credentials(
    cli: ModuleType,
    data: Dataset,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arguments: list[str],
    message: str,
) -> None:
    prepare(cli, data, monkeypatch)
    monkeypatch.setenv("SEMANTIC_EVAL_REAL", "1")
    with pytest.raises(SystemExit, match=message):
        cli.main(["--mode", "real", "--output-dir", str(tmp_path), *arguments])
    assert not list(tmp_path.iterdir())


def test_caller_cannot_increase_hard_real_call_budget(
    cli: ModuleType,
    data: Dataset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare(cli, data, monkeypatch)
    monkeypatch.setenv("SEMANTIC_EVAL_REAL", "1")
    with pytest.raises(ValueError, match="1..2000"):
        cli.main(["--mode", "real", "--allow-host", "offline.example", "--max-model-calls", "2001"])


def test_unavailable_prerequisites_create_only_safe_skipped_reports(
    cli: ModuleType,
    data: Dataset,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = prepare(cli, data, monkeypatch)
    monkeypatch.setenv("SEMANTIC_EVAL_REAL", "1")

    def failed():
        raise RealModeUnavailable()

    monkeypatch.setattr(cli, "load_credentials", failed)
    assert (
        cli.main(
            ["--mode", "real", "--allow-host", "offline.example", "--output-dir", str(tmp_path)]
        )
        == 0
    )
    assert calls == {}
    status = json.loads((tmp_path / "real-mode-status.json").read_text())
    assert status["status"] == "skipped"
    assert status["model_calls"] == 0
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "real-mode-status.json",
        "real-mode-status.md",
    ]
    assert "skipped" in capsys.readouterr().out


@pytest.mark.parametrize("rerank", [False, True])
def test_real_wiring_uses_explicit_transport_and_default_stratified_query_limit(
    cli: ModuleType,
    data: Dataset,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    rerank: bool,
) -> None:
    calls = prepare(cli, data, monkeypatch)
    monkeypatch.setenv("SEMANTIC_EVAL_REAL", "1")
    credentials = Credentials("https://offline.example/tenant/v1", "fixture-private-key")
    monkeypatch.setattr(cli, "load_credentials", lambda: credentials)
    client = object()
    providers = {}

    def make_client(**kwargs):
        assert kwargs == {"trust_env": False}
        return nullcontext(client)

    def provider(kind):
        def create(injected_client, transport, config, **kwargs):
            assert injected_client is client
            assert kwargs == {"credential": credentials.api_key}
            providers[kind] = (transport, config)
            return object()

        return create

    monkeypatch.setattr(cli.httpx, "Client", make_client)
    monkeypatch.setattr(cli, "OpenAIEmbeddingProvider", provider("embedding"))
    monkeypatch.setattr(cli, "HTTPRerankProvider", provider("rerank"))
    monkeypatch.setattr(cli, "OpenAISelector", provider("selection"))
    arguments = [
        "--mode",
        "real",
        "--allow-host",
        "offline.example",
        "--output-dir",
        str(tmp_path),
        "--queries-per-size",
        "5",
        "--chat-model",
        "fixture-chat",
    ]
    if rerank:
        arguments += ["--enable-rerank", "--rerank-path", "/verified/rerank"]
    assert cli.main(arguments) == 0
    selected, options = calls["evaluate"]
    assert len(selected.queries) == 100
    assert selected == cli.subset(data, 100)
    assert options["real"] is True
    assert options["enable_rerank"] is rerank
    assert options["budget"].limit == 2000
    assert calls["knee"][0] == data
    assert calls["knee"][2]["budget"] is options["budget"]
    assert calls["knee"][2]["queries_per_size"] == 5
    for transport, _ in providers.values():
        assert transport.base_url == credentials.base_url
        assert transport.allowed_hosts == ("offline.example",)
        assert transport.auth_header_name == "Authorization"
    assert providers["selection"][1].json_mode is False
    assert providers["selection"][1].profile.model_id == "fixture-chat"
    assert ("rerank" in providers) is rerank
    if rerank:
        assert providers["rerank"][1].codec.path == "/verified/rerank"
        assert providers["rerank"][1].profile.model_id == "qwen3-rerank"
    else:
        assert options["reranker"] is None
    knee = json.loads((tmp_path / "real-knee.json").read_text())
    assert knee["mode"] == "real"
    assert knee["model"] == "fixture-chat"
    assert knee["quality_acceptance"] is False
    for path in tmp_path.iterdir():
        assert credentials.api_key not in path.read_text()
        assert credentials.base_url not in path.read_text()
    captured = capsys.readouterr()
    assert credentials.api_key not in captured.out + captured.err

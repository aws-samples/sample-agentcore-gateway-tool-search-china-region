"""初始化基准必须拒绝自比和不满足测量前提的样本。"""

from __future__ import annotations

import importlib.util
import json
import subprocess  # nosec B404
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


class BenchmarkHarness:
    """只替换脚本的 Git/子进程适配器，不加载运行时或访问网络。"""

    def __init__(self, module: ModuleType, directory: Path, monkeypatch: pytest.MonkeyPatch):
        self.module = module
        self.directory = directory
        self.monkeypatch = monkeypatch
        self.baseline = "# baseline runtime\n"
        self.current = "# current runtime\n"
        self.child_calls: list[dict[str, Any]] = []
        self.overrides: dict[tuple[str, str], dict[str, object]] = {}
        self.output = directory / "result.json"
        runtime = directory / module.RUNTIME
        runtime.parent.mkdir(parents=True)
        runtime.write_text(self.current)
        monkeypatch.setattr(module, "ROOT", directory)
        monkeypatch.setattr(module, "subprocess", SimpleNamespace(run=self.run))

    def run(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["shell"] is False
        if Path(command[0]).name == "git" and command[1:3] == ["rev-parse", "--verify"]:
            assert "--end-of-options" in command
            # CompletedProcess only constructs a mock result; it starts no process.
            # nosemgrep: dangerous-subprocess-use-audit
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
        if Path(command[0]).name == "git" and command[1] == "show":
            # CompletedProcess only constructs a mock result; it starts no process.
            # nosemgrep: dangerous-subprocess-use-audit
            return subprocess.CompletedProcess(command, 0, self.baseline, "")
        assert command[-1] == "--child"
        payload = json.loads(kwargs["input"])
        variant = "baseline" if payload["source"] == self.baseline else "current"
        mode = payload["mode"]
        self.child_calls.append({"variant": variant, "mode": mode, "env": kwargs["env"]})
        # 当前版本故意更慢：正确性前提不能变成没有统计支撑的速度门槛。
        total = 100.0 if variant == "baseline" else 120.0
        row = {
            "mode": mode,
            "import_ms": total / 2,
            "factory_ms": total / 2,
            "total_ms": total,
            "stages": {},
            "source_compile_count": 0,
            "unused_modules": ["boto3", "s3transfer"] if variant == "baseline" else [],
            "configuration_requests": 1,
            **self.overrides.get((variant, mode), {}),
        }
        # CompletedProcess only constructs a mock result; it starts no process.
        # nosemgrep: dangerous-subprocess-use-audit
        return subprocess.CompletedProcess(command, 0, json.dumps(row), "")

    def invoke(self, *arguments: str) -> None:
        self.monkeypatch.setattr(
            sys,
            "argv",
            ["benchmark", "--output", str(self.output), *arguments],
        )
        self.module.main()


@pytest.fixture
def benchmark(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BenchmarkHarness:
    spec = importlib.util.spec_from_file_location(
        "runtime_benchmark_under_test", ROOT / "scripts/benchmark_runtime_initialization.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return BenchmarkHarness(module, tmp_path, monkeypatch)


def test_baseline_ref_must_be_explicit(
    benchmark: BenchmarkHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        benchmark.invoke()
    assert error.value.code == 2
    assert "--baseline-ref" in capsys.readouterr().err
    assert benchmark.child_calls == []
    assert not benchmark.output.exists()


def test_identical_runtime_source_is_rejected(benchmark: BenchmarkHarness) -> None:
    benchmark.baseline = benchmark.current
    with pytest.raises(RuntimeError, match="identical runtime source"):
        benchmark.invoke("--baseline-ref", "a" * 40)
    assert benchmark.child_calls == []
    assert not benchmark.output.exists()


@pytest.mark.parametrize("variant", ["baseline", "current"])
@pytest.mark.parametrize("mode", ["source_compile", "normal_import"])
@pytest.mark.parametrize("requests", [0, 2, True])
def test_each_arm_requires_exactly_one_configuration_request(
    benchmark: BenchmarkHarness, variant: str, mode: str, requests: object
) -> None:
    benchmark.overrides[(variant, mode)] = {"configuration_requests": requests}
    with pytest.raises(RuntimeError, match="configuration_requests"):
        benchmark.invoke("--baseline-ref", "a" * 40)
    assert not benchmark.output.exists()


@pytest.mark.parametrize("mode", ["source_compile", "normal_import"])
@pytest.mark.parametrize(
    ("variant", "modules"),
    [
        ("baseline", []),
        ("baseline", ["boto3"]),
        ("baseline", ["s3transfer"]),
        ("current", ["boto3"]),
        ("current", ["s3transfer"]),
    ],
)
def test_each_arm_requires_its_expected_import_behavior(
    benchmark: BenchmarkHarness, variant: str, modules: list[str], mode: str
) -> None:
    benchmark.overrides[(variant, mode)] = {"unused_modules": modules}
    with pytest.raises(RuntimeError, match="unused_modules"):
        benchmark.invoke("--baseline-ref", "a" * 40)
    assert not benchmark.output.exists()


def test_valid_preconditions_preserve_negative_savings(benchmark: BenchmarkHarness) -> None:
    benchmark.invoke("--baseline-ref", "a" * 40)
    report = json.loads(benchmark.output.read_text())
    assert report["baseline_ref"] == "a" * 40
    assert report["baseline_sha256"] != report["current_sha256"]
    assert len(report["rows"]) == len(benchmark.child_calls) == 80
    for mode in ("source_compile", "normal_import"):
        assert report["summaries"][mode]["paired_total_saving_ms"]["p50"] == -20.0
    assert report["preconditions"]["validated"] is True
    assert report["preconditions"]["configuration_requests_per_sample"] == 1
    assert report["preconditions"]["unused_modules"] == {
        "baseline": ["boto3", "s3transfer"],
        "current": [],
    }
    for call in benchmark.child_calls:
        assert call["env"] == benchmark.module._offline_environment()
        assert call["env"]["AWS_SHARED_CREDENTIALS_FILE"] == str(Path("/dev/null"))


@pytest.mark.parametrize("ref", ["--help", "HEAD~1", "main; echo unsafe", "not-a-commit"])
def test_baseline_reference_rejects_nonliteral_revisions(benchmark, ref):
    with pytest.raises(SystemExit):
        benchmark.invoke("--baseline-ref=" + ref)
    assert benchmark.child_calls == []
    assert not benchmark.output.exists()

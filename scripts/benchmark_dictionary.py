"""离线交替测量固定词典解码；包含字典恢复，不把本机收益当作 Lambda 收益。"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from semantic_core import _dictionary  # noqa: E402
from semantic_core._timing import collect  # noqa: E402

BASELINE_SHA256 = "75eb5695a9b214e6a68f194ed97f8bdee34da1fab7b8383c649609580c86db2e"
Load = Callable[[], tuple[dict[str, int], int, float]]


@dataclass(frozen=True)
class Sample:
    block: int
    position: int
    lane: str
    decode_ms: float
    full_load_ms: float


def baseline_data() -> tuple[bytes, dict[str, int], int]:
    """独立从固定输入重建 v1；摘要同时绑定旧结构和完整元数据。"""
    raw = (ROOT / "build_inputs/jieba-0.42.1/dict.txt").read_bytes()
    if hashlib.sha256(raw).hexdigest() != _dictionary.BASE_DICTIONARY_SHA256:
        raise ValueError("基础词典与固定摘要不符")
    frequencies: dict[str, int] = {}
    total = 0
    for line in raw.decode("utf-8").splitlines():
        word, text = line.strip().split(" ")[:2]
        frequency = int(text)
        frequencies[word] = frequency
        total += frequency
        for position in range(len(word)):
            frequencies.setdefault(word[: position + 1], 0)
    document = {
        "schema_version": 1,
        "jieba_version": "0.42.1",
        "base_dictionary_sha256": _dictionary.BASE_DICTIONARY_SHA256,
        "total": total,
        "frequencies": frequencies,
    }
    data = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(data).hexdigest() != BASELINE_SHA256:
        raise ValueError("重建的 v1 与固定摘要不符")
    return data, frequencies, total


def checked_load(loader: Load, expected: dict[str, int], expected_total: int) -> float:
    restored, total, elapsed = loader()
    if restored != expected or total != expected_total or total != 60101967:
        raise ValueError("完整词频或 total 不等价")
    return elapsed


def measure(
    loaders: dict[str, Load], expected: dict[str, int], total: int, blocks: int
) -> tuple[list[Sample], dict[str, dict[str, float]]]:
    for loader in loaders.values():
        for _ in range(2):
            checked_load(loader, expected, total)
    samples: list[Sample] = []
    for block in range(blocks):
        for position, lane in enumerate(("baseline", "current", "current", "baseline")):
            gc.collect()
            started = time.perf_counter()
            restored, loaded_total, elapsed = loaders[lane]()
            full_ms = (time.perf_counter() - started) * 1000
            if restored != expected or loaded_total != total:
                raise ValueError("完整词频或 total 不等价")
            samples.append(Sample(block, position, lane, elapsed, full_ms))
            del restored
    summaries: dict[str, dict[str, float]] = {}
    for lane, loader in loaders.items():
        gc.collect()
        tracemalloc.start()
        restored, loaded_total, _ = loader()
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        if restored != expected or loaded_total != total:
            raise ValueError("内存测量时完整词频或 total 不等价")
        del restored
        values = sorted(row.decode_ms for row in samples if row.lane == lane)
        full = sorted(row.full_load_ms for row in samples if row.lane == lane)
        summaries[lane] = {
            "n": len(values),
            "decode_p50_ms": statistics.median(values),
            "decode_p95_ms": values[(95 * len(values) + 99) // 100 - 1],
            "full_load_p50_ms": statistics.median(full),
            "full_load_p95_ms": full[(95 * len(full) + 99) // 100 - 1],
            "retained_bytes": current,
            "peak_bytes": peak,
        }
    return samples, summaries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=int, default=20)
    parser.add_argument(
        "--output", type=Path, default=ROOT / ".artifacts/dictionary-benchmark.json"
    )
    args = parser.parse_args()
    if args.blocks < 20:
        parser.error("至少执行二十组 ABBA")
    output = cast(Path, args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    raw, expected, total = baseline_data()
    with tempfile.TemporaryDirectory(prefix="dictionary-benchmark-", dir=output.parent) as temp:
        baseline_path = Path(temp) / "baseline-v1.json"
        baseline_path.write_bytes(raw)

        def old_load() -> tuple[dict[str, int], int, float]:
            data = baseline_path.read_bytes()
            if hashlib.sha256(data).hexdigest() != BASELINE_SHA256:
                raise ValueError("v1 基准文件摘要不符")
            started = time.perf_counter()
            value = json.loads(data)
            restored = cast(dict[str, int], value["frequencies"])
            loaded_total = cast(int, value["total"])
            elapsed = (time.perf_counter() - started) * 1000
            return restored, loaded_total, elapsed

        def current_load() -> tuple[dict[str, int], int, float]:
            with collect() as captured:
                restored, loaded_total = _dictionary.load_dictionary()
            elapsed = float(captured.snapshot()["tokenizer.dictionary.decode"]["duration_ms"])
            return restored, loaded_total, elapsed

        samples, summaries = measure(
            {"baseline": old_load, "current": current_load}, expected, total, args.blocks
        )
    ratio = summaries["current"]["decode_p50_ms"] / summaries["baseline"]["decode_p50_ms"]
    result = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "measurement": "本机已导入解码器；ABBA；包含字典恢复；不代表 Lambda 冷启动",
        "artifact": _dictionary.ARTIFACT_NAME,
        "baseline_sha256": BASELINE_SHA256,
        "current_sha256": _dictionary.ARTIFACT_SHA256,
        "entries": len(expected),
        "total": total,
        "blocks": args.blocks,
        "all_entries_equal": True,
        "summaries": summaries,
        "current_to_baseline_ratio": ratio,
        "threshold": 0.85,
        "passed": ratio <= 0.85,
        "samples": [asdict(row) for row in samples],
    }
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    print(f"当前/v1 解码中位数：{ratio:.4f}；要求 ≤ 0.8500；完整词频与 total 全部相等")
    print("结果仅描述本机解码收益；目标平台收益需要部署后验证。")
    return 0 if ratio <= 0.85 else 1


if __name__ == "__main__":
    raise SystemExit(main())

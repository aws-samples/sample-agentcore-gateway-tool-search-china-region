"""从固定 jieba 原始词典生成可复现资源，不导入 jieba 或目标原生模块。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from semantic_core._dictionary import (  # noqa: E402
    ARTIFACT_NAME,
    ARTIFACT_SHA256,
    BASE_DICTIONARY_SHA256,
    JIEBA_VERSION,
)

SOURCE_DICTIONARY = ROOT / "build_inputs" / f"jieba-{JIEBA_VERSION}" / "dict.txt"


def build_dictionary(output_dir: Path, *, dictionary_path: Path | None = None) -> Path:
    """校验固定输入和完整输出后，再把字典写入指定构建目录。"""
    if dictionary_path is None:
        # Source trees and sdists carry this immutable input. A PEP 517 build
        # environment needs only the declared backend, never runtime packages.
        dictionary_path = SOURCE_DICTIONARY
    raw = dictionary_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != BASE_DICTIONARY_SHA256:
        raise ValueError("Dictionary source SHA-256 does not match the pin")
    frequencies: dict[str, int] = {}
    total = 0
    # Preserve jieba 0.42.1 gen_pfdict's insertion order and accumulation rule.
    # Duplicate entries replace FREQ[word], but still contribute to total.
    for line in raw.decode("utf-8").splitlines():
        word, frequency_text = line.strip().split(" ")[:2]
        frequency = int(frequency_text)
        frequencies[word] = frequency
        total += frequency
        for position in range(len(word)):
            prefix = word[: position + 1]
            if prefix not in frequencies:
                frequencies[prefix] = 0
    document = {
        "schema_version": 2,
        "jieba_version": JIEBA_VERSION,
        "base_dictionary_sha256": BASE_DICTIONARY_SHA256,
        "total": total,
        "words": list(frequencies),
        "frequencies": list(frequencies.values()),
    }
    artifact = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(artifact).hexdigest() != ARTIFACT_SHA256:
        raise ValueError("Generated dictionary SHA-256 does not match the pin")
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / ARTIFACT_NAME
    destination.write_bytes(artifact)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dictionary", type=Path, help="显式指定已锁定目标包的原始词典")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "src" / "semantic_core" / "data",
        help="生成资源所在目录",
    )
    args = parser.parse_args()
    artifact = build_dictionary(args.output_dir, dictionary_path=args.dictionary)
    print(f"预构建字典已生成：{artifact.stat().st_size} bytes；SHA-256 {ARTIFACT_SHA256}")


if __name__ == "__main__":
    main()

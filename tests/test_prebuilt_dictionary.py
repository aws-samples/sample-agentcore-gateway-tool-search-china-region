"""冷进程必须直接加载构建产物，禁止 jieba 初始化或重建基础词典。"""

import json
import os
import subprocess  # nosec B404
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_dictionary_cache():
    """故障注入必须触发真实加载，配置标签不再制造词典缓存 miss。"""
    from semantic_core import tokenization

    tokenization._shared_cut.cache_clear()
    yield
    tokenization._shared_cut.cache_clear()


@pytest.mark.parametrize("custom", [False, True])
def test_cold_tokenizer_never_initializes_or_builds_dictionary(tmp_path, custom):
    code = """
import json, socket, sys
from pathlib import Path
def forbidden(*args, **kwargs):
    raise AssertionError("runtime dictionary construction forbidden")
socket.getaddrinfo = socket.socket.connect = forbidden
import jieba
jieba.Tokenizer.initialize = forbidden
jieba.Tokenizer.gen_pfdict = forbidden
from semantic_core.tokenization import ChineseTokenizer, TokenizerConfig
custom = sys.argv[1] == "True"
config = TokenizerConfig(
    dictionary=(("星际订单", 1000000),) if custom else (),
    synonyms=(("星际订单", ("order",)),) if custom else (),
)
tokenizer = ChineseTokenizer(config)
tokens = tokenizer.tokenize("取消星际订单" if custom else "取消订单")
assert ("星际订单" in tokens and "order" in tokens) if custom else tokens == ("取消", "订单")
assert not list(Path(sys.argv[2]).iterdir()), "runtime must not write a dictionary cache"
print(json.dumps(tokens, ensure_ascii=False))
"""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AWS_")
        and key not in {"SEMANTIC_CORE_RUN_INTEGRATION", "SEMANTIC_EVAL_REAL", "PYTHONPATH"}
    }
    env.update(
        PYTHONPATH=str(ROOT / "src"),
        TMPDIR=str(tmp_path),
        AWS_CONFIG_FILE="/dev/null",
        AWS_SHARED_CREDENTIALS_FILE="/dev/null",
        AWS_EC2_METADATA_DISABLED="true",
    )
    # Fixed local test program and fixture paths; argv is not request-controlled.
    # nosemgrep: dangerous-subprocess-use-audit
    result = subprocess.run(  # nosec B603
        [sys.executable, "-c", code, str(custom), str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)


def test_prebuilt_table_matches_pinned_jieba_including_total():
    """独立调用上游算法作参照；total 不能错误地替换成最终词频之和。"""
    import jieba

    from semantic_core._dictionary import load_dictionary

    reference = jieba.Tokenizer()
    expected_frequencies, expected_total = reference.gen_pfdict(reference.get_dict_file())
    frequencies, total = load_dictionary()
    assert frequencies == expected_frequencies
    assert total == expected_total == 60101967
    assert total != sum(frequencies.values())


def test_decode_timer_includes_frequency_table_restoration(monkeypatch):
    from semantic_core import _dictionary
    from semantic_core._timing import collect

    restoring = []
    with collect() as captured:

        class Words(list):
            def __iter__(self):
                # 阶段尚未结束，才能保证记录覆盖 JSON 解析后的词频表恢复。
                restoring.append("tokenizer.dictionary.decode" not in captured.snapshot())
                return super().__iter__()

        value = {"words": Words(["订单", "查询"]), "frequencies": [3, 7], "total": 10}
        monkeypatch.setattr(_dictionary, "json", SimpleNamespace(loads=lambda raw: value))
        frequencies, total = _dictionary.load_dictionary()

    assert restoring == [True]
    assert frequencies == {"订单": 3, "查询": 7}
    assert total == 10
    assert captured.snapshot()["tokenizer.dictionary.decode"]["count"] == 1


@pytest.mark.parametrize("damage", ["missing", "truncated", "version", "frequency"])
def test_missing_or_damaged_artifact_never_falls_back(tmp_path, monkeypatch, damage):
    import jieba

    from semantic_core import _dictionary, tokenization

    resource = ROOT / "src/semantic_core/data" / _dictionary.ARTIFACT_NAME
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    if damage != "missing":
        data = resource.read_bytes()
        if damage == "truncated":
            data = data[:100]
        else:
            value = json.loads(data)
            if damage == "version":
                value["jieba_version"] = "different"
            else:
                value["frequencies"][value["words"].index("订单")] += 1
            data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        (data_dir / resource.name).write_bytes(data)
    monkeypatch.setattr(
        _dictionary,
        "importlib",
        SimpleNamespace(resources=SimpleNamespace(files=lambda name: tmp_path)),
    )
    monkeypatch.setattr(
        tokenization, "_base_dictionary_digest", lambda: _dictionary.BASE_DICTIONARY_SHA256
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("runtime dictionary construction forbidden")

    monkeypatch.setattr(jieba.Tokenizer, "initialize", forbidden)
    monkeypatch.setattr(jieba.Tokenizer, "gen_pfdict", forbidden)
    config = tokenization.TokenizerConfig(dictionary_version=f"damaged-{damage}")
    with pytest.raises(ValueError, match="prebuilt tokenizer dictionary"):
        tokenization.ChineseTokenizer(config)


def test_changed_installed_base_dictionary_is_rejected(monkeypatch):
    from semantic_core import tokenization

    monkeypatch.setattr(tokenization, "_base_dictionary_digest", lambda: "0" * 64)
    with pytest.raises(ValueError, match="installed jieba dictionary"):
        tokenization.ChineseTokenizer(
            tokenization.TokenizerConfig(dictionary_version="changed-installed-base")
        )


def test_custom_words_keep_original_total_and_do_not_mutate_other_profiles(monkeypatch):
    import jieba

    from semantic_core import tokenization

    factory = jieba.Tokenizer
    created = []

    def recording_factory():
        segmenter = factory()
        created.append(segmenter)
        return segmenter

    monkeypatch.setattr(jieba, "Tokenizer", recording_factory)
    baseline = tokenization.ChineseTokenizer(
        tokenization.TokenizerConfig(dictionary_version="prebuilt-isolation-base")
    )
    custom = tokenization.ChineseTokenizer(
        tokenization.TokenizerConfig(
            dictionary_version="prebuilt-isolation-custom",
            dictionary=(("北京", 7), ("火星专用交换指令", 1000000)),
        )
    )
    assert created[1].total == created[0].total + 7 + 1000000
    assert created[0].FREQ["北京"] != 7
    assert created[1].FREQ["北京"] == 7
    assert custom.tokenize("火星专用交换指令") == ("火星专用交换指令",)
    assert baseline.tokenize("火星专用交换指令") != ("火星专用交换指令",)

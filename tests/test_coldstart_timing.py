"""阶段计时只进入内部日志，不改变响应、异常、缓存或请求隔离。"""

import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import lru_cache
from types import SimpleNamespace

import pytest
from service.test_identity import (
    BUSINESS,
    SEARCH,
    Authorizer,
    Reader,
    codec,
    context,
    event,
)

from semantic_core import tokenization
from semantic_service import runtime
from semantic_service.auth import ServiceError
from semantic_service.search import SearchHandler
from semantic_service.secrets import SecretsCache


def timing_module():
    return importlib.import_module("semantic_core._timing")


def test_runtime_separates_factory_work_and_module_init_without_response_changes(
    monkeypatch, capsys
):
    search = SearchHandler(SEARCH, codec(), Authorizer(), Reader())
    monkeypatch.setattr(runtime, "_search", lru_cache(maxsize=1)(lambda: search))
    monkeypatch.setattr(runtime, "_FIRST_INVOCATION", True, raising=False)
    ctx = context()
    ctx.aws_request_id = "01234567-89ab-cdef-0123-456789abcdef"
    for _ in range(2):
        assert runtime.search_handler(event(), ctx) == {"tools": [BUSINESS.descriptor]}
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["first_invocation"] for row in rows] == [True, False]
    for row in rows:
        assert row["aws_request_id"] == ctx.aws_request_id
        assert row["runtime_module_init_ms"] >= 0
        assert "lambda_init_ms" not in row  # 应用计时不可冒充平台 Init Duration。
        stages = row["stage_timings"]
        assert stages["handler.factory"]["count"] == stages["handler.work"]["count"] == 1
        assert stages["search.authorize"]["count"] == 5
        assert stages["search.bm25"]["count"] == stages["search.response"]["count"] == 1
        assert "model.embedding" not in stages
        assert "query" not in row and "tools" not in row


def test_factory_failure_keeps_partial_stages_and_original_service_error(monkeypatch, capsys):
    def broken():
        raise ValueError("private query credential endpoint")

    monkeypatch.setattr(runtime, "_request", broken)
    with pytest.raises(ServiceError, match="internal failure"):
        runtime.request_handler({}, SimpleNamespace(aws_request_id="private input"))
    raw = capsys.readouterr().out
    row = json.loads(raw)
    assert row["stage_timings"]["handler.factory"]["errors"] == 1
    assert "handler.work" not in row["stage_timings"]
    assert row["aws_request_id"] is None
    assert all(value not in raw for value in ["private", "credential", "endpoint"])


def test_request_scopes_isolate_threads_nested_collectors_and_failed_stages():
    timing = timing_module()

    def run(stage_name):
        with timing.collect() as captured:
            for _ in range(3):
                with timing.stage(stage_name):
                    pass
            with pytest.raises(ValueError), timing.stage(stage_name):
                raise ValueError("not logged")
        return captured.snapshot()

    with timing.collect() as outer:
        with ThreadPoolExecutor(max_workers=2) as pool:
            left, right = list(pool.map(run, ["auth.authorize", "snapshot.load"]))
        with timing.stage("handler.work"):
            with timing.collect() as inner:
                with timing.stage("runtime.config"):
                    pass
    assert set(left) == {"auth.authorize"} and set(right) == {"snapshot.load"}
    assert left["auth.authorize"]["count"] == 4
    assert left["auth.authorize"]["errors"] == 1
    assert set(outer.snapshot()) == {"handler.work"}
    assert set(inner.snapshot()) == {"runtime.config"}
    with timing.stage("handler.work"):
        pass
    assert outer.snapshot()["handler.work"]["count"] == 1


def test_only_static_stage_names_can_enter_logs_and_recording_failure_is_non_fatal(monkeypatch):
    timing = timing_module()
    with timing.collect() as captured:
        with timing.stage("private query and key"):
            pass
        with timing.stage("handler.work"):
            pass
        before = captured.snapshot()

        def broken(*args):
            raise RuntimeError("private telemetry error")

        monkeypatch.setattr(captured, "record", broken)
        with timing.stage("handler.work"):
            pass
        with pytest.raises(ValueError, match="business failure"), timing.stage("handler.work"):
            raise ValueError("business failure")
    assert set(before) == {"handler.work"}
    assert before["handler.work"]["duration_ms"] >= 0
    assert before["handler.work"]["max_ms"] >= 0


def test_jieba_dictionary_timer_records_only_actual_cache_misses(monkeypatch):
    timing = timing_module()
    calls = []

    class Segmenter:
        def initialize(self):
            raise AssertionError("runtime dictionary construction forbidden")

        def add_word(self, word, freq):
            pass

        def cut(self, sentence, cut_all, HMM):
            return [sentence]

    original_import = tokenization.importlib.import_module
    original_load = tokenization.load_dictionary

    def loading():
        calls.append("load")
        return original_load()

    monkeypatch.setattr(tokenization, "load_dictionary", loading)
    monkeypatch.setattr(
        tokenization, "_base_dictionary_digest", lambda: tokenization.BASE_DICTIONARY_SHA256
    )
    monkeypatch.setattr(
        tokenization,
        "importlib",
        SimpleNamespace(
            import_module=lambda name: SimpleNamespace(Tokenizer=Segmenter)
            if name == "jieba"
            else original_import(name),
            resources=importlib.resources,
            metadata=importlib.metadata,
        ),
    )
    config = tokenization.TokenizerConfig(dictionary_version="timing-isolated")
    tokenization._shared_cut.cache_clear()
    try:
        with timing.collect() as first:
            tokenization.ChineseTokenizer(config)
        with timing.collect() as second:
            tokenization.ChineseTokenizer(config)
        assert calls == ["load"]
        assert first.snapshot()["tokenizer.dictionary"]["count"] == 1
        assert first.snapshot()["tokenizer.dictionary.read"]["count"] == 1
        assert first.snapshot()["tokenizer.dictionary.decode"]["count"] == 1
        assert first.snapshot()["tokenizer.import"]["count"] == 1
        assert "tokenizer.dictionary" not in second.snapshot()
    finally:
        tokenization._shared_cut.cache_clear()


@pytest.mark.parametrize("fails", [False, True])
def test_base_dictionary_timer_covers_resource_resolution_and_only_cache_misses(monkeypatch, fails):
    timing = timing_module()
    calls = []

    class Resource:
        def joinpath(self, name):
            assert name == "dict.txt"
            return self

        def read_bytes(self):
            calls.append("read")
            return b"fixed dictionary input"

    fail_next = fails

    def files(name):
        nonlocal fail_next
        assert name == "jieba"
        calls.append("resolve")
        if fail_next:
            fail_next = False
            raise OSError("resource unavailable")
        return Resource()

    monkeypatch.setattr(
        tokenization,
        "importlib",
        SimpleNamespace(
            import_module=importlib.import_module,
            resources=SimpleNamespace(files=files),
            metadata=importlib.metadata,
        ),
    )
    tokenization._base_dictionary_digest.cache_clear()
    try:
        with timing.collect() as first:
            if fails:
                with pytest.raises(OSError, match="resource unavailable"):
                    tokenization._base_dictionary_digest()
            else:
                expected = tokenization.hashlib.sha256(b"fixed dictionary input").hexdigest()
                assert tokenization._base_dictionary_digest() == expected
        recorded = first.snapshot()["tokenizer.base_dictionary"]
        assert recorded["count"] == 1
        assert recorded["errors"] == int(fails)
        expected = tokenization.hashlib.sha256(b"fixed dictionary input").hexdigest()
        with timing.collect() as retried:
            assert tokenization._base_dictionary_digest() == expected
        if fails:
            assert retried.snapshot()["tokenizer.base_dictionary"]["count"] == 1
            assert retried.snapshot()["tokenizer.base_dictionary"]["errors"] == 0
            assert calls == ["resolve", "resolve", "read"]
        else:
            assert not retried.snapshot()
            assert calls == ["resolve", "read"]
        with timing.collect() as reused:
            assert tokenization._base_dictionary_digest() == expected
        assert not reused.snapshot()
    finally:
        tokenization._base_dictionary_digest.cache_clear()


@pytest.mark.parametrize("path", ["direct", "from_dict"])
def test_tokenizer_import_and_digest_have_the_same_scope_on_both_paths(monkeypatch, path):
    """真实两条入口下，隐式资源导入不能挪入摘要阶段或嵌入词典解码。"""
    timing = timing_module()
    clock = [0.0]
    imported = [False]
    active = []
    resolution_scopes = []
    contents = b"fixed dictionary input"
    digest = tokenization.hashlib.sha256(contents).hexdigest()

    class Segmenter:
        def cut(self, sentence, cut_all, HMM):
            return [sentence]

    def load(name):
        assert name == "jieba"
        if not imported[0]:
            imported[0] = True
            clock[0] += 0.050
        return SimpleNamespace(Tokenizer=Segmenter)

    class Resource:
        def joinpath(self, name):
            assert name == "dict.txt"
            return self

        def read_bytes(self):
            clock[0] += 0.005
            return contents

    def files(name):
        resolution_scopes.append(tuple(active))
        load(name)  # importlib.resources.files("jieba") 的首次隐式导入行为。
        return Resource()

    @contextmanager
    def observed_stage(name):
        active.append(name)
        try:
            with timing.stage(name):
                yield
        finally:
            active.pop()

    monkeypatch.setattr(timing, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    monkeypatch.setattr(tokenization, "stage", observed_stage)
    monkeypatch.setattr(
        tokenization,
        "importlib",
        SimpleNamespace(
            import_module=load,
            resources=SimpleNamespace(files=files),
            metadata=SimpleNamespace(version=lambda name: "0.42.1"),
        ),
    )
    monkeypatch.setattr(tokenization, "BASE_DICTIONARY_SHA256", digest)
    monkeypatch.setattr(tokenization, "load_dictionary", lambda: ({"订单": 1}, 1))
    tokenization._base_dictionary_digest.cache_clear()
    tokenization._shared_cut.cache_clear()
    try:
        with timing.collect() as captured:
            if path == "direct":
                tokenizer = tokenization.ChineseTokenizer()
            else:
                tokenizer = tokenization.ChineseTokenizer.from_dict(
                    {
                        "config": tokenization.TokenizerConfig().to_dict(),
                        "jieba_version": "0.42.1",
                        "base_dictionary_sha256": digest,
                        "mode": "exact",
                        "hmm": False,
                        "stopwords": [],
                    }
                )
        assert tokenizer.tokenize("订单") == ("订单",)
        stages = captured.snapshot()
        assert stages["tokenizer.import"]["duration_ms"] == 50.0
        assert stages["tokenizer.base_dictionary"]["duration_ms"] == 5.0
        assert resolution_scopes == [("tokenizer.base_dictionary",)]
    finally:
        tokenization._base_dictionary_digest.cache_clear()
        tokenization._shared_cut.cache_clear()


def test_secret_timer_counts_remote_fetches_without_logging_identifiers_or_values():
    timing = timing_module()
    calls = []

    class Client:
        def get_secret_value(self, **kwargs):
            calls.append(kwargs)
            return {"VersionId": "v1", "SecretString": '{"api_key":"private-value"}'}

    cache = SecretsCache(Client())
    with timing.collect() as captured:
        for _ in range(2):
            assert cache.get("private-secret").value["api_key"] == "private-value"
    stages = captured.snapshot()
    assert stages["aws.secrets"]["count"] == 1
    assert len(calls) == 1
    assert "private" not in json.dumps(stages)


def test_application_module_init_is_measured_in_fresh_process(tmp_path):
    """新进程的启动计时必须只依赖本地导入，不能加载 AWS 配置或运行工厂。"""
    import os
    import subprocess  # nosec B404
    import sys
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src"
    code = """
import json, socket
def blocked(*args, **kwargs):
    raise AssertionError("network forbidden")
socket.getaddrinfo = socket.socket.connect = blocked
from semantic_service import runtime
runtime._request = lambda: (lambda event, context: {"messageVersion": "1.0"})
runtime.request_handler({}, object())
"""
    env = dict(
        os.environ,
        PYTHONPATH=str(source),
        AWS_CONFIG_FILE=str(tmp_path / "absent-config"),
        AWS_SHARED_CREDENTIALS_FILE=str(tmp_path / "absent-credentials"),
        AWS_EC2_METADATA_DISABLED="true",
    )
    for name in tuple(env):
        if name.startswith("AWS_") and name not in {
            "AWS_CONFIG_FILE",
            "AWS_SHARED_CREDENTIALS_FILE",
            "AWS_EC2_METADATA_DISABLED",
        }:
            env.pop(name)
    # Fixed local test program and fixture paths; argv is not request-controlled.
    # nosemgrep: dangerous-subprocess-use-audit
    result = subprocess.run(  # nosec B603
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)
    assert row["first_invocation"] is True
    assert row["runtime_module_init_ms"] > 0
    assert set(row["stage_timings"]) == {"handler.factory", "handler.work"}

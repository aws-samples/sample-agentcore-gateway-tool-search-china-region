"""Embedding 计数过滤器必须匹配真实 Search 日志，且只统计固定错误类别。"""

from __future__ import annotations

import json
import re
import time
from functools import partial
from typing import get_args

import httpx
from service.test_identity import BUSINESS, GATEWAY, SEARCH, Authorizer, codec, context, event
from test_components import components  # noqa: F401

from semantic_core.openai_compatible import (
    EmbeddingConfig,
    OpenAICompatibleConfig,
    OpenAIEmbeddingProvider,
)
from semantic_core.providers import (
    ErrorCode,
    FailureKind,
    ModelProfile,
    ProviderError,
    RequestContext,
)
from semantic_core.snapshot import IndexSnapshot
from semantic_core.vectors import VectorIndex
from semantic_service import runtime
from semantic_service.search import SearchHandler

PROFILE = ModelProfile("fixture-embedding", "fixture-v1", 2)
PRIVATE = "private-metric-fixture"
HTTP_STATUSES = {
    "http_rate_limited": 429,
    "http_client_error": 401,
    "http_server_error": 503,
    "http_status_error": 302,
}
TRANSPORT_FAILURES = {
    "connect_timeout": httpx.ConnectTimeout,
    "read_timeout": httpx.ReadTimeout,
    "write_timeout": httpx.WriteTimeout,
    "pool_timeout": httpx.PoolTimeout,
    "timeout": httpx.TimeoutException,
    "network_error": httpx.ConnectError,
}
FALLBACK_FAILURES = {
    "authentication": ErrorCode.AUTHENTICATION,
    "invalid_request": ErrorCode.INVALID_REQUEST,
    "unavailable": ErrorCode.UNAVAILABLE,
    "rate_limited": ErrorCode.RATE_LIMITED,
}
CASES = (
    *HTTP_STATUSES,
    *TRANSPORT_FAILURES,
    *FALLBACK_FAILURES,
    "deadline_exceeded",
    "response_parse",
    "unknown",
)
MISSING = object()


def _response(kind, request):
    if kind in HTTP_STATUSES:
        return httpx.Response(
            HTTP_STATUSES[kind],
            text=PRIVATE,
            headers={"Retry-After": "17", "x-ratelimit-remaining-requests": "123"},
        )
    if kind in TRANSPORT_FAILURES:
        raise TRANSPORT_FAILURES[kind](PRIVATE, request=request)
    if kind == "response_parse":
        return httpx.Response(200, text=PRIVATE)
    if kind == "unknown":
        raise RuntimeError(PRIVATE)
    assert kind is None
    return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})


class _FallbackProvider:
    """覆盖保留的 ErrorCode 映射与真实 deadline 校验，不伪造 Search 日志。"""

    def __init__(self, kind):
        self.kind = kind

    def describe(self):
        return PROFILE

    def embed(self, texts, purpose, request_context):
        if self.kind == "deadline_exceeded":
            RequestContext("expired-fixture", time.monotonic() - 1).remaining()
            raise AssertionError("expired deadline was accepted")
        raise ProviderError(FALLBACK_FAILURES[self.kind])


def _search_log(kind, monkeypatch, capsys):
    index = IndexSnapshot.build(
        GATEWAY,
        (BUSINESS,),
        vectors=VectorIndex([[1, 0]], [0], 1, 2),
        model_profile=PROFILE,
    )

    class Reader:
        def current(self, control):
            return index

    with httpx.Client(
        transport=httpx.MockTransport(partial(_response, kind)), trust_env=False
    ) as client:
        provider = (
            _FallbackProvider(kind)
            if kind in FALLBACK_FAILURES or kind == "deadline_exceeded"
            else OpenAIEmbeddingProvider(
                client,
                OpenAICompatibleConfig(
                    f"https://{PRIVATE}.invalid",
                    (f"{PRIVATE}.invalid",),
                    auth_header_name="Authorization",
                ),
                EmbeddingConfig(PROFILE, "/embeddings", 10, 8192),
                credential=PRIVATE,
            )
        )
        search = SearchHandler(SEARCH, codec(), Authorizer(), Reader(), provider)
        monkeypatch.setattr(runtime, "_search", lambda: search)
        assert runtime.search_handler(event(), context()) == {"tools": [BUSINESS.descriptor]}
    captured = capsys.readouterr()
    assert PRIVATE not in captured.out + captured.err
    rows = [json.loads(line) for line in captured.out.splitlines() if line.startswith("{")]
    assert len(rows) == 1
    row = rows[0]
    assert row["operation"] == "search"
    if kind is None:
        assert row["embedding_error"] is None
        assert row["degraded"] is False
    else:
        assert row["embedding_error"]["kind"] == kind
        assert row["degraded"] is True
        assert row["ranking_mode"] == "bm25"
    return row


def _conditions(pattern):
    assert pattern.startswith("{ ") and pattern.endswith(" }"), pattern
    result = []
    for condition in pattern[2:-2].split(" && "):
        match = re.fullmatch(r'(\$\.[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*) = "([^"]+)"', condition)
        assert match is not None, condition
        result.append(match.groups())
    return result


def _lookup(row, selector):
    value = row
    for field in selector.removeprefix("$.").split("."):
        if not isinstance(value, dict) or field not in value:
            return MISSING
        value = value[field]
    return value


def test_embedding_filters_match_real_stdout_for_the_closed_failure_set(
    components,  # noqa: F811
    monkeypatch,
    capsys,
):
    assert len(CASES) == len(set(CASES))
    assert set(CASES) == set(get_args(FailureKind))
    failures = {kind: _search_log(kind, monkeypatch, capsys) for kind in CASES}
    success = _search_log(None, monkeypatch, capsys)
    search = next(
        resource["Properties"]
        for resource in components.values()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource["Properties"]["Handler"] == "semantic_service.runtime.search_handler"
    )
    filters = [
        resource["Properties"]
        for resource in components.values()
        if resource["Type"] == "AWS::Logs::MetricFilter"
        and (
            "$.embedding_error" in json.dumps(resource["Properties"])
            or resource["Properties"]["MetricTransformations"][0]["MetricName"].startswith(
                "embedding-error-"
            )
        )
    ]
    assert len(filters) == len(CASES)
    observed = []
    for metric in filters:
        assert metric["LogGroupName"] == search["LoggingConfig"]["LogGroup"]
        assert len(metric["MetricTransformations"]) == 1
        transformation = metric["MetricTransformations"][0]
        assert transformation["MetricValue"] == "1"
        assert not transformation.get("Dimensions")
        conditions = _conditions(metric["FilterPattern"])
        assert {selector for selector, _ in conditions} == {
            "$.operation",
            "$.embedding_error.kind",
        }
        matching = [
            kind
            for kind, row in failures.items()
            if all(_lookup(row, selector) == expected for selector, expected in conditions)
        ]
        assert len(matching) == 1, transformation["MetricName"]
        kind = matching[0]
        assert transformation["MetricName"] == f"embedding-error-{kind.replace('_', '-')}"
        assert all(_lookup(failures[kind], selector) is not MISSING for selector, _ in conditions)
        assert not all(_lookup(success, selector) == expected for selector, expected in conditions)
        observed.append(kind)
    assert len(observed) == len(set(observed))
    assert set(observed) == set(CASES)

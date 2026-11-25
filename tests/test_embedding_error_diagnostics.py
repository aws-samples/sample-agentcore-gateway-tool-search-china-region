"""错误分类经过真实 HTTP 与服务边界，业务响应和既有调用次数保持不变。"""

from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace

import httpx
import pytest
from service.test_identity import (
    BINDING,
    BUSINESS,
    GATEWAY,
    PRINCIPAL,
    SEARCH,
    Authorizer,
    codec,
    context,
)

from semantic_core.openai_compatible import (
    ChatConfig,
    EmbeddingConfig,
    OpenAIChatProvider,
    OpenAICompatibleConfig,
    OpenAIEmbeddingProvider,
)
from semantic_core.providers import (
    ErrorCode,
    ModelProfile,
    ProviderError,
    RequestContext,
    RerankDocument,
)
from semantic_core.rerank import HTTPRerankProvider, RerankCodec, RerankConfig
from semantic_core.search import SearchConfig, SearchEngine
from semantic_core.snapshot import IndexSnapshot
from semantic_core.vectors import VectorIndex
from semantic_eval.selection import OpenAISelector
from semantic_service import runtime
from semantic_service.auth import InputError
from semantic_service.models import ModelSettings, RotatingModelProviders
from semantic_service.search import SearchHandler
from semantic_service.secrets import SecretsCache

HOST = "private-host-marker.invalid"
KEY = "private-key-marker"
QUERY = "cancel private-query-marker"
PROFILE = ModelProfile("fixture-embedding", "fixture-v1", 2)
SENTINELS = (
    HOST,
    KEY,
    "private-query-marker",
    "private-body-marker",
    "private-header-marker",
    "private-exception-marker",
    "private-route-marker",
)
RATE_HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
)


def request_context():
    return RequestContext("safe-fixture-request", time.monotonic() + 10)


def transport():
    return OpenAICompatibleConfig(
        f"https://{HOST}",
        (HOST,),
        auth_header_name="Authorization",
    )


def success():
    return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})


def snapshot():
    return IndexSnapshot.build(
        GATEWAY,
        (BUSINESS,),
        vectors=VectorIndex([[1, 0]], [0], 1, 2),
        model_profile=PROFILE,
    )


def private_event():
    binding = replace(BINDING, query=QUERY)
    return {
        "query": QUERY,
        "_auth_context": codec().issue(PRINCIPAL, binding, policy_epoch=1),
    }


@contextmanager
def service(handler, *, transport_config=None):
    requests = []

    def observed(request):
        requests.append(request)
        return handler(request)

    with httpx.Client(transport=httpx.MockTransport(observed), trust_env=False) as client:
        embedding = OpenAIEmbeddingProvider(
            client,
            transport_config or transport(),
            EmbeddingConfig(PROFILE, "/private-route-marker/embeddings", 10, 8192),
            credential=KEY,
        )
        index = snapshot()

        class Reader:
            def current(self, control):
                return index

        search = SearchHandler(SEARCH, codec(), Authorizer(), Reader(), embedding)
        yield search, requests


def log_record(capsys):
    captured = capsys.readouterr()
    raw = captured.out + captured.err
    rows = [json.loads(line) for line in captured.out.splitlines() if line.startswith("{")]
    assert len(rows) == 1
    assert rows[0]["operation"] == "search"
    for sentinel in SENTINELS:
        assert sentinel not in raw
    return rows[0]


def test_log_guard_rejects_stderr_leak_from_real_search_call(monkeypatch, capsys):
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def leaky_transport(request):
        print(f"private-exception-marker {request.url} {QUERY}", file=sys.stderr)
        return httpx.Response(503)

    with service(leaky_transport) as (search, requests):
        monkeypatch.setattr(runtime, "_search", lambda: search)
        assert runtime.search_handler(private_event(), context()) == {
            "tools": [BUSINESS.descriptor]
        }
        assert_bounded_request_timeouts(requests)
    with pytest.raises(AssertionError):
        log_record(capsys)


def assert_bounded_request_timeouts(requests, count=1, *, timeout=1.0):
    assert len(requests) == count
    for request in requests:
        budgets = request.extensions["timeout"]
        assert set(budgets) == {"connect", "read", "write", "pool"}
        assert len(set(budgets.values())) == 1
        assert 0 < budgets["read"] <= timeout


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, "http_client_error"),
        (401, "http_client_error"),
        (403, "http_client_error"),
        (408, "http_client_error"),
        (429, "http_rate_limited"),
        (500, "http_server_error"),
        (503, "http_server_error"),
        (504, "http_server_error"),
        (302, "http_status_error"),
    ],
)
def test_http_failure_reaches_search_stdout_without_changing_business_response(
    status, kind, monkeypatch, capsys
):
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def fail(_):
        return httpx.Response(
            status,
            text="private-body-marker",
            headers={"Location": "https://private-header-marker.invalid"},
        )

    with service(fail) as (search, requests):
        monkeypatch.setattr(runtime, "_search", lambda: search)
        assert runtime.search_handler(private_event(), context()) == {
            "tools": [BUSINESS.descriptor]
        }
        assert_bounded_request_timeouts(requests)
    row = log_record(capsys)
    assert row["ranking_mode"] == "bm25"
    assert row["degraded"] is True
    assert row["vector_availability"] == "failed"
    assert row["embedding_error"]["kind"] == kind
    assert row["embedding_error"]["http_status"] == status
    assert row["embedding_error"]["retry_after_seconds"] is None
    assert row["embedding_error"]["rate_limit_headers"] == {}


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (httpx.ConnectTimeout, "connect_timeout"),
        (httpx.ReadTimeout, "read_timeout"),
        (httpx.WriteTimeout, "write_timeout"),
        (httpx.PoolTimeout, "pool_timeout"),
        (httpx.ConnectError, "network_error"),
        (httpx.ReadError, "network_error"),
        (None, "response_parse"),
    ],
)
def test_transport_and_parse_failures_are_distinct_without_raw_details(
    failure, kind, monkeypatch, capsys
):
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def fail(request):
        if failure is None:
            return httpx.Response(200, content=b"private-body-marker not JSON")
        raise failure(f"private-exception-marker {request.url}", request=request)

    with service(fail) as (search, requests):
        monkeypatch.setattr(runtime, "_search", lambda: search)
        assert runtime.search_handler(private_event(), context()) == {
            "tools": [BUSINESS.descriptor]
        }
        assert_bounded_request_timeouts(requests)
    row = log_record(capsys)
    assert row["embedding_error"]["kind"] == kind
    assert row["embedding_error"]["http_status"] == (200 if failure is None else None)
    assert row["embedding_error"]["rate_limit_headers"] == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", 0),
        ("42", 42),
        ("2147483647", 2147483647),
        ("2147483648", None),
        ("00000000000", None),
        ("1.0", None),
        ("1s", None),
        ("-1", None),
        ("+1", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", None),
        ("1, 2", None),
        ("private-header-marker", None),
        ("", None),
        ("１２", None),
    ],
)
def test_retry_after_only_accepts_bounded_ascii_integer(value, expected, monkeypatch, capsys):
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def fail(_):
        return httpx.Response(429, headers=[(b"Retry-After", value.encode("utf-8"))])

    with service(fail) as (search, requests):
        monkeypatch.setattr(runtime, "_search", lambda: search)
        assert runtime.search_handler(private_event(), context()) == {
            "tools": [BUSINESS.descriptor]
        }
        assert_bounded_request_timeouts(requests)
    assert log_record(capsys)["embedding_error"]["retry_after_seconds"] == expected


def test_rate_limit_headers_are_allowlisted_numeric_and_not_repeated(monkeypatch, capsys):
    headers = [(name, str(index)) for index, name in enumerate(RATE_HEADERS)]
    headers.extend(
        [
            ("Retry-After", "2"),
            ("Retry-After", "3"),
            ("x-ratelimit-limit-requests", "99"),
            ("x-ratelimit-secret", "private-header-marker"),
            ("Authorization", KEY),
        ]
    )
    with service(lambda _: httpx.Response(429, headers=headers)) as (search, _):
        monkeypatch.setattr(runtime, "_search", lambda: search)
        runtime.search_handler(private_event(), context())
    error = log_record(capsys)["embedding_error"]
    assert error["retry_after_seconds"] is None
    assert error["rate_limit_headers"] == {
        name: index for index, name in enumerate(RATE_HEADERS) if index
    }


def test_invalid_rate_limit_values_are_omitted_together(monkeypatch, capsys):
    headers = dict(
        zip(
            RATE_HEADERS,
            ["-1", "1s", "2147483648", "1.0", "Wed, 21 Oct 2015 07:28:00 GMT", ""],
            strict=True,
        )
    )
    with service(lambda _: httpx.Response(429, headers=headers)) as (search, _):
        monkeypatch.setattr(runtime, "_search", lambda: search)
        runtime.search_handler(private_event(), context())
    assert log_record(capsys)["embedding_error"]["rate_limit_headers"] == {}


def test_service_overload_records_retry_delay_but_not_rate_counters(monkeypatch, capsys):
    headers = {name: "7" for name in RATE_HEADERS}
    headers.update({"Retry-After": "30", "x-private": "private-header-marker"})
    with service(lambda _: httpx.Response(503, headers=headers)) as (search, requests):
        monkeypatch.setattr(runtime, "_search", lambda: search)
        assert runtime.search_handler(private_event(), context()) == {
            "tools": [BUSINESS.descriptor]
        }
        assert_bounded_request_timeouts(requests)
    row = log_record(capsys)
    assert row["ranking_mode"] == "bm25"
    assert row["degraded"] is True
    assert row["embedding_error"] == {
        "kind": "http_server_error",
        "http_status": 503,
        "retry_after_seconds": 30,
        "rate_limit_headers": {},
    }


@pytest.mark.parametrize("first_status", [429, 401])
def test_rotating_provider_only_keeps_existing_authentication_refresh(first_status):
    class SyntheticSecrets:
        calls = 0

        def get_secret_value(self, **kwargs):
            self.calls += 1
            return {
                "VersionId": f"fixture-{self.calls}",
                "SecretString": json.dumps({"base_url": f"https://{HOST}", "api_key": KEY}),
            }

    requests = []

    def fail(request):
        requests.append(request)
        return httpx.Response(
            first_status if len(requests) == 1 else 429,
            headers={"Retry-After": "2"},
        )

    secrets = SyntheticSecrets()
    settings = ModelSettings.from_env(
        {
            "MODEL_ALLOWED_HOSTS": HOST,
            "MODEL_EMBEDDING_MODEL": "example-embedding-model",
            "MODEL_CHAT_MODEL": "example-chat-model",
            "MODEL_RERANK_MODEL": "example-rerank-model",
            "MODEL_EMBEDDING_DIMENSIONS": "2",
        }
    )
    with httpx.Client(transport=httpx.MockTransport(fail), trust_env=False) as client:
        provider = RotatingModelProviders(SecretsCache(secrets), settings, client)
        with pytest.raises(ProviderError) as caught:
            provider.embedding.embed([QUERY], "query", request_context())
    count = 1 if first_status == 429 else 2
    assert_bounded_request_timeouts(requests, count=count)
    assert secrets.calls == count
    assert caught.value.code is ErrorCode.RATE_LIMITED
    assert caught.value.failure.kind == "http_rate_limited"
    assert caught.value.failure.http_status == 429
    assert caught.value.failure.retry_after_seconds == 2


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("refresh_failure", ["secret", "endpoint"])
def test_rotation_refresh_failure_retains_authentication_in_real_search_log(
    status, refresh_failure, monkeypatch, capsys
):
    class SyntheticSecrets:
        calls = 0

        def get_secret_value(self, **kwargs):
            self.calls += 1
            if self.calls > 1 and refresh_failure == "secret":
                raise RuntimeError(f"private-exception-marker {KEY}")
            base = f"https://{HOST}" + ("/private-route-marker" if self.calls > 1 else "")
            return {
                "VersionId": f"fixture-{self.calls}",
                "SecretString": json.dumps({"base_url": base, "api_key": KEY}),
            }

    requests = []

    def fail(request):
        requests.append(request)
        return httpx.Response(status, text="private-body-marker")

    settings = ModelSettings.from_env(
        {
            "MODEL_ALLOWED_HOSTS": HOST,
            "MODEL_CHAT_MODEL": "example-chat-model",
            "MODEL_RERANK_MODEL": "example-rerank-model",
            "MODEL_EMBEDDING_MODEL": PROFILE.model_id,
            "MODEL_EMBEDDING_REVISION": PROFILE.revision,
            "MODEL_EMBEDDING_DIMENSIONS": "2",
        }
    )
    index = snapshot()

    class Reader:
        def current(self, control):
            return index

    secrets = SyntheticSecrets()
    with httpx.Client(transport=httpx.MockTransport(fail), trust_env=False) as client:
        provider = RotatingModelProviders(SecretsCache(secrets), settings, client)
        search = SearchHandler(SEARCH, codec(), Authorizer(), Reader(), provider.embedding)
        monkeypatch.setattr(runtime, "_search", lambda: search)
        rows = []
        for _ in range(2):
            assert runtime.search_handler(private_event(), context()) == {
                "tools": [BUSINESS.descriptor]
            }
            rows.append(log_record(capsys))
        assert_bounded_request_timeouts(requests)
    assert rows[0]["embedding_error"] == {
        "kind": "http_client_error",
        "http_status": status,
        "retry_after_seconds": None,
        "rate_limit_headers": {},
    }
    assert rows[1]["embedding_error"] == {
        "kind": "unavailable" if refresh_failure == "secret" else "invalid_request",
        "http_status": None,
        "retry_after_seconds": None,
        "rate_limit_headers": {},
    }
    assert all(row["degraded"] and row["ranking_mode"] == "bm25" for row in rows)
    assert secrets.calls == (3 if refresh_failure == "secret" else 2)


def consumer(kind, client):
    if kind == "embedding":
        provider = OpenAIEmbeddingProvider(
            client,
            transport(),
            EmbeddingConfig(PROFILE, "/embeddings", 10, 8192),
            credential=KEY,
        )
        return lambda: provider.embed([QUERY], "query", request_context())
    chat = ChatConfig(ModelProfile("fixture-chat", "v1"), "/chat/completions", 65536)
    if kind == "chat":
        provider = OpenAIChatProvider(client, transport(), chat, credential=KEY)
        return lambda: provider.generate_queries(
            "private-body-marker", ("en",), 1, request_context()
        )
    if kind == "rerank":
        provider = HTTPRerankProvider(
            client,
            transport(),
            RerankConfig(ModelProfile("fixture-rerank", "v1"), RerankCodec()),
            credential=KEY,
        )
        return lambda: provider.rerank(
            QUERY,
            (RerankDocument("fixture-document", "private-body-marker"),),
            1,
            request_context(),
        )
    provider = OpenAISelector(client, transport(), replace(chat, json_mode=False), credential=KEY)
    return lambda: provider.select(QUERY, (BUSINESS,), request_context())


@pytest.mark.parametrize("kind", ["embedding", "chat", "rerank", "selector"])
@pytest.mark.parametrize(
    ("status", "code", "failure_kind"),
    [
        (429, ErrorCode.RATE_LIMITED, "http_rate_limited"),
        (503, ErrorCode.UNAVAILABLE, "http_server_error"),
    ],
)
def test_all_shared_http_consumers_preserve_failure_and_error_code(
    kind, status, code, failure_kind
):
    calls = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def fail(request):
        calls.append(request)
        return httpx.Response(
            status,
            text="private-body-marker",
            headers={"Retry-After": "7", "x-ratelimit-remaining-requests": "0"},
        )

    with httpx.Client(transport=httpx.MockTransport(fail), trust_env=False) as client:
        with pytest.raises(ProviderError) as caught:
            consumer(kind, client)()
    assert_bounded_request_timeouts(calls)
    assert caught.value.code is code
    assert caught.value.usage == {}
    assert str(caught.value) == f"provider {code.value}"
    failure = caught.value.failure
    assert failure.kind == failure_kind
    assert failure.http_status == status
    fields = failure.as_log_fields()
    assert fields["retry_after_seconds"] == 7
    assert fields["rate_limit_headers"] == (
        {"x-ratelimit-remaining-requests": 0} if status == 429 else {}
    )
    assert all(sentinel not in json.dumps(fields) for sentinel in SENTINELS)


@pytest.mark.parametrize("kind", ["embedding", "chat", "rerank", "selector"])
@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize("malformation", ["identity", "payload"])
def test_response_validation_wrappers_keep_failure_and_usage(kind, status, malformation):
    payload = {
        "embedding": {"data": [{"index": 0, "embedding": [0, 0]}]},
        "chat": {"choices": []},
        "rerank": {"results": []},
        "selector": {"choices": []},
    }[kind]
    payload["usage"] = {"total_tokens": 17}
    if malformation == "identity":
        payload["model"] = "wrong-model"
    calls = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def rejected_response(request):
        calls.append(request)
        return httpx.Response(status, json=payload)

    with httpx.Client(
        transport=httpx.MockTransport(rejected_response),
        trust_env=False,
    ) as client:
        with pytest.raises(ProviderError) as caught:
            consumer(kind, client)()
    assert_bounded_request_timeouts(calls)
    assert caught.value.code is ErrorCode.INVALID_RESPONSE
    assert caught.value.usage == {"total_tokens": 17}
    assert caught.value.failure.kind == "response_parse"
    assert caught.value.failure.http_status == status
    assert caught.value.failure.retry_after_seconds is None
    assert caught.value.failure.rate_limit_headers == ()


@pytest.mark.parametrize("kind", ["embedding", "chat", "rerank", "selector"])
@pytest.mark.parametrize("status", [200, 201])
def test_invalid_usage_keeps_valid_counters_and_actual_response_status(kind, status):
    payload = {"usage": {"total_tokens": 17, "input_tokens": -1}}
    calls = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def invalid_usage(request):
        calls.append(request)
        return httpx.Response(status, json=payload)

    with httpx.Client(transport=httpx.MockTransport(invalid_usage), trust_env=False) as client:
        with pytest.raises(ProviderError) as caught:
            consumer(kind, client)()
    assert_bounded_request_timeouts(calls)
    assert caught.value.code is ErrorCode.INVALID_RESPONSE
    assert caught.value.usage == {"total_tokens": 17}
    assert caught.value.failure.kind == "response_parse"
    assert caught.value.failure.http_status == status


@pytest.mark.parametrize("status", [429, 503])
def test_chat_repair_then_http_failure_keeps_first_usage_and_latest_failure(status):
    calls = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def fail(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": "private-body-marker"}}
                    ],
                    "usage": {"total_tokens": 17},
                },
            )
        return httpx.Response(status, headers={"Retry-After": "3"})

    with httpx.Client(transport=httpx.MockTransport(fail), trust_env=False) as client:
        with pytest.raises(ProviderError) as caught:
            consumer("chat", client)()
    assert_bounded_request_timeouts(calls, count=2)
    assert caught.value.usage == {"total_tokens": 17}
    assert caught.value.failure.http_status == status
    assert caught.value.failure.kind == (
        "http_rate_limited" if status == 429 else "http_server_error"
    )
    assert caught.value.failure.retry_after_seconds == 3


def test_chat_repair_then_network_failure_does_not_inherit_previous_http_status():
    calls = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def failed_second_attempt(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": "private-body-marker"}}
                    ],
                    "usage": {"total_tokens": 17},
                },
            )
        raise httpx.ConnectError(f"private-exception-marker {request.url}", request=request)

    with httpx.Client(
        transport=httpx.MockTransport(failed_second_attempt), trust_env=False
    ) as client:
        with pytest.raises(ProviderError) as caught:
            consumer("chat", client)()
    assert_bounded_request_timeouts(calls, count=2)
    assert caught.value.code is ErrorCode.UNAVAILABLE
    assert caught.value.usage == {"total_tokens": 17}
    assert caught.value.failure.kind == "network_error"
    assert caught.value.failure.http_status is None
    assert caught.value.failure.retry_after_seconds is None
    assert caught.value.failure.rate_limit_headers == ()
    fields = json.dumps(caught.value.failure.as_log_fields())
    assert all(sentinel not in fields for sentinel in SENTINELS)


def test_cached_service_failure_success_and_early_failure_have_isolated_error(monkeypatch, capsys):
    replies = iter([httpx.Response(429), httpx.Response(503), success()])
    with service(lambda _: next(replies)) as (search, requests):
        monkeypatch.setattr(runtime, "_search", lambda: search)
        observed = []
        for _ in range(3):
            assert runtime.search_handler(private_event(), context()) == {
                "tools": [BUSINESS.descriptor]
            }
            observed.append(log_record(capsys))
        with pytest.raises(InputError):
            runtime.search_handler({"query": ""}, context())
        observed.append(log_record(capsys))
        assert_bounded_request_timeouts(requests, count=3)
    assert observed[0]["embedding_error"]["http_status"] == 429
    assert observed[1]["embedding_error"]["http_status"] == 503
    assert observed[2]["embedding_error"] is None
    assert observed[2]["degraded"] is False
    assert observed[3]["embedding_error"] is None
    assert observed[3]["vector_availability"] == "not_attempted"


def test_deadline_failure_is_not_mislabeled_as_a_network_timeout(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("semantic_core._http.time.monotonic", lambda: clock[0])

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def late(_):
        clock[0] += 0.46
        return success()

    with service(late, transport_config=replace(transport(), timeout_seconds=0.45)) as (
        search,
        requests,
    ):
        diagnostics = []
        result = SearchEngine(
            search.snapshots.current(None),
            search.embedding,
            config=SearchConfig(embedding_timeout=0.45),
        ).search(
            QUERY,
            frozenset({BUSINESS.tool_id}),
            RequestContext("deadline-fixture", 101.0),
            diagnostics=diagnostics.append,
        )
        assert_bounded_request_timeouts(requests, timeout=0.45)
    assert result.mode == "bm25"
    assert diagnostics[0].embedding_error.kind == "deadline_exceeded"


def test_actual_search_request_uses_default_one_second_embedding_budget(monkeypatch, capsys):
    monkeypatch.setattr("semantic_core.search.time.monotonic", lambda: 100.0)
    with service(lambda _: success()) as (search, requests):
        monkeypatch.setattr(runtime, "_search", lambda: search)
        assert runtime.search_handler(private_event(), context()) == {
            "tools": [BUSINESS.descriptor]
        }
        assert len(requests) == 1
        assert search.config.embedding_timeout == 1.0
        assert requests[0].extensions["timeout"] == {
            name: pytest.approx(1.0, abs=1e-12) for name in ("connect", "read", "write", "pool")
        }
    row = log_record(capsys)
    assert row["degraded"] is False
    assert row["embedding_error"] is None


def test_bad_gzip_response_is_parse_failure_in_search_log(monkeypatch, capsys):
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def corrupt(_):
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=httpx.ByteStream(b"private-body-marker not gzip"),
        )

    with service(corrupt) as (search, requests):
        monkeypatch.setattr(runtime, "_search", lambda: search)
        assert runtime.search_handler(private_event(), context()) == {
            "tools": [BUSINESS.descriptor]
        }
        assert_bounded_request_timeouts(requests)
    error = log_record(capsys)["embedding_error"]
    assert error["kind"] == "response_parse"
    assert error["http_status"] == 200


def test_bad_gzip_response_keeps_original_unavailable_error_code():
    calls = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def corrupt(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=httpx.ByteStream(b"private-body-marker not gzip"),
        )

    with httpx.Client(transport=httpx.MockTransport(corrupt), trust_env=False) as client:
        with pytest.raises(ProviderError) as caught:
            consumer("embedding", client)()
    assert_bounded_request_timeouts(calls)
    assert caught.value.code is ErrorCode.UNAVAILABLE
    assert caught.value.failure.kind == "response_parse"
    assert caught.value.failure.http_status == 200


@pytest.mark.parametrize(
    "invalid",
    [
        RuntimeError("private-exception-marker"),
        {"kind": "private-header-marker"},
        "private-key-marker",
        "",
        0,
        False,
    ],
)
def test_provider_error_rejects_invalid_failure_with_fixed_safe_message(invalid):
    with pytest.raises(TypeError) as caught:
        ProviderError(ErrorCode.UNAVAILABLE, failure=invalid)
    assert str(caught.value) == "failure must be ProviderFailure or None"
    assert all(sentinel not in str(caught.value) for sentinel in SENTINELS)


def test_unknown_exception_is_bounded_and_keeps_bm25(monkeypatch, capsys):
    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def broken(_):
        raise RuntimeError("private-exception-marker private-key-marker")

    with service(broken) as (search, requests):
        monkeypatch.setattr(runtime, "_search", lambda: search)
        assert runtime.search_handler(private_event(), context()) == {
            "tools": [BUSINESS.descriptor]
        }
        assert_bounded_request_timeouts(requests)
    assert log_record(capsys)["embedding_error"]["kind"] == "unknown"


def test_failure_is_immutable_and_log_conversion_cannot_mutate_retained_headers():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"x-ratelimit-remaining-requests": "2"})
        ),
        trust_env=False,
    ) as client:
        with pytest.raises(ProviderError) as caught:
            consumer("embedding", client)()
    failure = caught.value.failure
    with pytest.raises(FrozenInstanceError):
        failure.kind = "unknown"
    exported = failure.as_log_fields()
    exported["rate_limit_headers"]["x-ratelimit-remaining-requests"] = 999
    assert failure.as_log_fields()["rate_limit_headers"] == {"x-ratelimit-remaining-requests": 2}

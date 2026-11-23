"""通过正式汇总日志验证鉴权计数，测试不依赖收集器的实现。"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from test_catalog_auth_refresh import OAuthFixture

from semantic_core.providers import RequestContext
from semantic_service import runtime
from semantic_service.auth import ServiceError

FIELDS = (
    "gateway_401_count",
    "gateway_403_count",
    "token_proactive_refresh_count",
    "gateway_replay_count",
    "gateway_replay_success_count",
    "gateway_replay_failure_count",
    "gateway_replay_skipped_count",
    "gateway_auth_refresh_failure_count",
)
CONTEXT = SimpleNamespace(
    aws_request_id="00000000-0000-0000-0000-000000000001",
    get_remaining_time_in_millis=lambda: 45_500,
)


def observe(monkeypatch, capsys, action, *, failed=False, event=None):
    def run(request):
        action(request)
        return "reconciled"

    monkeypatch.setattr(runtime, "_indexer", lambda: SimpleNamespace(run=run))
    if failed:
        with pytest.raises(ServiceError, match="internal failure"):
            runtime.indexer_handler(event or {}, CONTEXT)
    else:
        result = runtime.indexer_handler(event or {}, CONTEXT)
        assert result == {"status": "expired" if event else "reconciled"}
    raw = capsys.readouterr().out
    rows = [json.loads(line) for line in raw.splitlines() if line.startswith("{")]
    rows = [row for row in rows if row["operation"] == "indexer"]
    assert len(rows) == 1
    for sensitive in (
        "synthetic-token",
        "synthetic-secret",
        "synthetic-client",
        "Authorization",
        "Bearer",
        "private-response",
    ):
        assert sensitive not in raw
    return rows[0]


def assert_counts(row, **expected):
    assert {field: row[field] for field in FIELDS} == {
        field: expected.get(field, 0) for field in FIELDS
    }
    assert all(type(row[field]) is int and row[field] >= 0 for field in FIELDS)
    assert row["gateway_replay_count"] == (
        row["gateway_replay_success_count"] + row["gateway_replay_failure_count"]
    )


def response(request, result=None):
    payload = json.loads(request.content)
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": payload["id"], "result": result or {"tools": []}},
    )


@pytest.mark.parametrize("expires_in", [90, 301, 540])
def test_proactive_refresh_and_cached_client_reset(monkeypatch, capsys, expires_in):
    fixture = OAuthFixture(monkeypatch, expires_in)
    mcp = fixture.mcp(response)

    def action(request):
        return mcp._request("tools/list", {}, request)

    assert_counts(observe(monkeypatch, capsys, action))
    fixture.now += expires_in / 2 - 0.001
    assert_counts(observe(monkeypatch, capsys, action))
    assert fixture.token_posts == 1
    fixture.now += 0.001
    assert_counts(observe(monkeypatch, capsys, action), token_proactive_refresh_count=1)
    assert fixture.token_posts == 2
    assert_counts(observe(monkeypatch, capsys, action))


def test_failed_proactive_refresh_is_counted_once(monkeypatch, capsys):
    fixture = OAuthFixture(monkeypatch)
    fixture.tokens.token(fixture.context())
    fixture.now += 150
    fixture.token_status = 503
    mcp = fixture.mcp(lambda _: pytest.fail("换取失败不应访问 Gateway"))
    row = observe(
        monkeypatch, capsys, lambda request: mcp._request("tools/list", {}, request), failed=True
    )
    assert_counts(row, token_proactive_refresh_count=1)
    assert row["status"] == "internal failure"


@pytest.mark.parametrize("denial", [401, 403])
@pytest.mark.parametrize(
    "method,status",
    [
        ("initialize", 200),
        ("tools/list", 200),
        ("notifications/initialized", 200),
        ("notifications/initialized", 202),
        ("notifications/initialized", 204),
    ],
)
def test_retry_success_counts_actual_replay(monkeypatch, capsys, denial, method, status):
    fixture = OAuthFixture(monkeypatch)
    attempts = []

    def gateway(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(denial, text="private-response")
        if method == "notifications/initialized":
            return httpx.Response(status)
        return response(request)

    mcp = fixture.mcp(gateway)
    row = observe(
        monkeypatch,
        capsys,
        lambda request: mcp._request(
            method, {}, request, notification=method == "notifications/initialized"
        ),
    )
    assert_counts(
        row,
        **{f"gateway_{denial}_count": 1},
        gateway_replay_count=1,
        gateway_replay_success_count=1,
    )
    assert len(attempts) == fixture.token_posts == 2
    assert attempts[0].content == attempts[1].content
    assert attempts[0].headers["Authorization"] != attempts[1].headers["Authorization"]


@pytest.mark.parametrize("statuses", [(401, 401), (401, 403), (403, 401), (403, 403)])
def test_retry_denials_count_both_responses(monkeypatch, capsys, statuses):
    fixture = OAuthFixture(monkeypatch)
    attempts = []

    def gateway(request):
        attempts.append(request)
        return httpx.Response(statuses[len(attempts) - 1], text="private-response")

    mcp = fixture.mcp(gateway)
    row = observe(
        monkeypatch, capsys, lambda request: mcp._request("tools/list", {}, request), failed=True
    )
    assert_counts(
        row,
        gateway_401_count=statuses.count(401),
        gateway_403_count=statuses.count(403),
        gateway_replay_count=1,
        gateway_replay_failure_count=1,
    )
    assert len(attempts) == 2


class BrokenStream(httpx.SyncByteStream):
    def __init__(self, request, failure):
        self.request, self.failure = request, failure

    def __iter__(self):
        if self.failure == "read":
            raise httpx.ReadError("private-response")
        yield response(self.request).content

    def close(self):
        if self.failure == "close":
            raise httpx.ReadError("private-response")


@pytest.mark.parametrize("failure", ["connect", "http", "envelope", "read", "close"])
def test_started_retry_failure_is_reported(monkeypatch, capsys, failure):
    fixture = OAuthFixture(monkeypatch)
    attempts = []

    def gateway(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(403)
        if failure == "connect":
            raise httpx.ConnectError("private-response")
        if failure == "http":
            return httpx.Response(503, text="private-response")
        if failure == "envelope":
            return httpx.Response(200, json={"error": "private-response"})
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=BrokenStream(request, failure)
        )

    mcp = fixture.mcp(gateway)
    row = observe(
        monkeypatch, capsys, lambda request: mcp._request("tools/list", {}, request), failed=True
    )
    assert_counts(row, gateway_403_count=1, gateway_replay_count=1, gateway_replay_failure_count=1)
    assert len(attempts) == 2


@pytest.mark.parametrize(
    "failure", ["gateway_budget", "refresh_budget", "unknown_budget", "token", "post_token_budget"]
)
def test_no_retry_without_gateway_replay(monkeypatch, capsys, failure):
    fixture = OAuthFixture(monkeypatch)
    fixture.tokens.token(fixture.context())
    attempts = []
    deadlines = []

    def exchange(request):
        fixture.now = deadlines[0] + 0.001
        return fixture.issue_token(request)

    if failure == "post_token_budget":
        fixture.tokens.client = httpx.Client(transport=httpx.MockTransport(exchange))

    def gateway(request):
        attempts.append(request)
        if failure == "gateway_budget":
            fixture.now = deadlines[0] - 10
        elif failure == "refresh_budget":
            fixture.now = deadlines[0] - 19
        elif failure == "unknown_budget":
            fixture.meta.config = None
        elif failure == "token":
            fixture.token_status = 503
        return httpx.Response(403)

    mcp = fixture.mcp(gateway)

    def action(request):
        deadlines.append(request.deadline)
        mcp._request("tools/list", {}, request)

    row = observe(monkeypatch, capsys, action, failed=True)
    assert_counts(
        row,
        gateway_403_count=1,
        gateway_auth_refresh_failure_count=int(failure == "token"),
        gateway_replay_skipped_count=int(failure != "token"),
    )
    assert len(attempts) == 1


def test_locked_secret_cache_does_not_count_a_retry(monkeypatch, capsys):
    fixture = OAuthFixture(monkeypatch)
    fixture.tokens.token(fixture.context())
    held, release = threading.Event(), threading.Event()

    def hold():
        with fixture._lock:
            held.set()
            assert release.wait(timeout=5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(timeout=1)
        mcp = fixture.mcp(lambda _: httpx.Response(403))
        row = observe(
            monkeypatch,
            capsys,
            lambda request: mcp._request("tools/list", {}, request),
            failed=True,
        )
        assert_counts(row, gateway_403_count=1, gateway_replay_skipped_count=1)
        assert fixture.token_posts == 1
    finally:
        release.set()
        holder.join(timeout=1)
    assert not holder.is_alive()


@pytest.mark.parametrize("status", [401, 403])
def test_oauth_status_is_not_a_gateway_status(monkeypatch, capsys, status):
    fixture = OAuthFixture(monkeypatch)
    fixture.token_status = status
    mcp = fixture.mcp(lambda _: pytest.fail("OAuth 失败不应访问 Gateway"))
    row = observe(
        monkeypatch, capsys, lambda request: mcp._request("tools/list", {}, request), failed=True
    )
    assert_counts(row)


def test_two_catalog_reads_accumulate_each_page_retry(monkeypatch, capsys):
    fixture = OAuthFixture(monkeypatch)
    counts = {}

    def gateway(request):
        payload = json.loads(request.content)
        key = payload.get("id", "notification")
        counts[key] = counts.get(key, 0) + 1
        if payload["method"] == "initialize":
            return response(
                request, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        if counts[key] == 1:
            return httpx.Response(403)
        page = {"tools": []}
        if "cursor" not in payload["params"]:
            page["nextCursor"] = "synthetic-next"
        return response(request, page)

    mcp = fixture.mcp(gateway)

    def action(request):
        assert mcp.list_tools(request) == []
        assert mcp.list_tools(request) == []

    row = observe(monkeypatch, capsys, action)
    assert_counts(row, gateway_403_count=4, gateway_replay_count=4, gateway_replay_success_count=4)


def test_expired_tick_emits_zero_counts(monkeypatch, capsys):
    row = observe(
        monkeypatch,
        capsys,
        lambda _: pytest.fail("不应启动过期任务"),
        event={"time": "2000-01-01T00:00:00Z"},
    )
    assert_counts(row)


def test_initialization_failure_emits_zero_counts(monkeypatch, capsys):
    def broken():
        raise RuntimeError("synthetic-secret")

    monkeypatch.setattr(runtime, "_indexer", broken)
    with pytest.raises(ServiceError):
        runtime.indexer_handler({}, CONTEXT)
    raw = capsys.readouterr().out
    assert "synthetic-secret" not in raw
    assert_counts(json.loads(raw))


def test_following_failure_resets_previous_success_counts(monkeypatch, capsys):
    fixture = OAuthFixture(monkeypatch)
    attempts = []

    def gateway(request):
        attempts.append(request)
        return httpx.Response(401) if len(attempts) == 1 else response(request)

    mcp = fixture.mcp(gateway)
    assert_counts(
        observe(monkeypatch, capsys, lambda request: mcp._request("tools/list", {}, request)),
        gateway_401_count=1,
        gateway_replay_count=1,
        gateway_replay_success_count=1,
    )
    fixture.now += 150
    fixture.token_status = 503
    assert_counts(
        observe(
            monkeypatch,
            capsys,
            lambda request: mcp._request("tools/list", {}, request),
            failed=True,
        ),
        token_proactive_refresh_count=1,
    )


def test_other_handlers_do_not_gain_indexer_fields(monkeypatch, capsys):
    monkeypatch.setattr(runtime, "_request", lambda: lambda *_: {})
    monkeypatch.setattr(runtime, "_search", lambda: lambda *_, **__: {})
    runtime.request_handler({}, CONTEXT)
    runtime.search_handler({}, CONTEXT)
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(rows) == 2
    assert all(not set(FIELDS).intersection(row) for row in rows)


def test_overlapping_invocations_keep_separate_counts(monkeypatch, capsys):
    fixture = OAuthFixture(monkeypatch)
    fixture.tokens.token(fixture.context())
    barrier = threading.Barrier(2)

    def run(request):
        status = 401 if request.request_id == "a" else 403
        attempts = []

        def gateway(http_request):
            attempts.append(http_request)
            if len(attempts) == 1:
                barrier.wait(timeout=5)
                return httpx.Response(status)
            return response(http_request)

        mcp = fixture.mcp(gateway)
        # 此用例只验证调用隔离，不引入共享凭据缓存的非阻塞锁竞争。
        mcp.tokens = SimpleNamespace(token=lambda *_, **__: "synthetic-token")
        mcp._request("tools/list", {}, RequestContext(request.request_id, 1045))
        return "reconciled"

    monkeypatch.setattr(runtime, "_indexer", lambda: SimpleNamespace(run=run))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda name: runtime.indexer_handler({}, SimpleNamespace(aws_request_id=name)),
                ("a", "b"),
            )
        )
    assert results == [{"status": "reconciled"}] * 2
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(rows) == 2
    assert sorted((row["gateway_401_count"], row["gateway_403_count"]) for row in rows) == [
        (0, 1),
        (1, 0),
    ]
    for row in rows:
        assert row["gateway_replay_count"] == row["gateway_replay_success_count"] == 1
        assert row["gateway_replay_failure_count"] == 0


def test_budget_expires_after_token_returns_before_replay_count(monkeypatch, capsys):
    fixture = OAuthFixture(monkeypatch)
    original_token = fixture.tokens.token
    deadlines, exchanges, attempts = [], [], []

    def token(context, *, force_refresh=False):
        value = original_token(context, force_refresh=force_refresh)
        if force_refresh:
            assert fixture.now < context.deadline
            exchanges.append(value)
            # 真实 token() 已成功返回；模拟随后的长停顿跨过原始 Gateway 截止时间。
            fixture.now = deadlines[0] + 0.001
        return value

    monkeypatch.setattr(fixture.tokens, "token", token)

    def gateway(request):
        attempts.append(request)
        return httpx.Response(403)

    mcp = fixture.mcp(gateway)

    def action(request):
        deadlines.append(request.deadline)
        mcp._request("tools/list", {}, request)

    row = observe(monkeypatch, capsys, action, failed=True)
    assert len(exchanges) == 1 and fixture.token_posts == 2
    assert len(attempts) == 1
    assert row["gateway_replay_count"] == 0
    assert row["gateway_replay_success_count"] == row["gateway_replay_failure_count"] == 0
    assert row["gateway_replay_skipped_count"] == 1
    assert row["gateway_auth_refresh_failure_count"] == 0

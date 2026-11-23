"""按 OAuth 寿命刷新；MCP 鉴权恢复有界，权限拒绝不会被吞掉。"""

import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from botocore.config import Config

from semantic_core.providers import RequestContext
from semantic_service.catalog import CatalogError, MCPClient, OAuthTokenSource
from semantic_service.secrets import SecretsCache


class OAuthFixture(SecretsCache):
    def __init__(self, monkeypatch, expires_in=300):
        self.now = 1000.0
        self.expires_in = expires_in
        self.token_posts = 0
        self.token_status = 200
        self.secret_refreshes = []
        monkeypatch.setattr("time.monotonic", lambda: self.now)
        self.meta = SimpleNamespace(
            config=Config(
                connect_timeout=1,
                read_timeout=3,
                retries={"total_max_attempts": 1, "mode": "standard"},
            )
        )
        super().__init__(self, clock=lambda: self.now)
        self.tokens = OAuthTokenSource(
            self,
            "synthetic-secret-reference",
            "https://idp.invalid/tenant/oauth2/v2.0/token",
            ("idp.invalid",),
            ("api://synthetic-resource/.default",),
            httpx.Client(transport=httpx.MockTransport(self.issue_token)),
        )

    def get(self, secret_id, *, stage="AWSCURRENT", force_refresh=False):
        self.secret_refreshes.append(force_refresh)
        return super().get(secret_id, stage=stage, force_refresh=force_refresh)

    def get_secret_value(self, **kwargs):
        return {
            "VersionId": "synthetic-version",
            "SecretString": json.dumps(
                {"client_id": "synthetic-client", "client_secret": "synthetic-secret"}
            ),
        }

    def issue_token(self, request):
        assert request.url.host == "idp.invalid"
        assert b"grant_type=client_credentials" in request.content
        self.token_posts += 1
        return httpx.Response(
            self.token_status,
            json={
                "access_token": f"synthetic-token-{self.token_posts}",
                "token_type": "Bearer",
                "expires_in": self.expires_in,
            },
        )

    def context(self):
        return RequestContext("synthetic-request", self.now + 45)

    def mcp(self, handler):
        return MCPClient(
            httpx.Client(transport=httpx.MockTransport(handler)),
            "https://gateway.invalid/mcp",
            ("gateway.invalid",),
            self.tokens,
        )


@pytest.mark.parametrize("expires_in", [90, 301, 540])
def test_token_refreshes_at_half_lifetime(monkeypatch, expires_in):
    fixture = OAuthFixture(monkeypatch, expires_in)
    original = fixture.tokens.token(fixture.context())
    fixture.now += expires_in / 2 - 0.001
    assert fixture.tokens.token(fixture.context()) == original
    assert fixture.token_posts == 1

    fixture.now += 0.001
    assert fixture.tokens.token(fixture.context()) != original
    assert fixture.token_posts == 2


@pytest.mark.parametrize("expires_in,refresh_after", [(1, 0), (40, 10), (3600, 300)])
def test_token_preserves_earlier_safety_limits(monkeypatch, expires_in, refresh_after):
    fixture = OAuthFixture(monkeypatch, expires_in)
    original = fixture.tokens.token(fixture.context())
    if refresh_after:
        fixture.now += refresh_after - 0.001
        assert fixture.tokens.token(fixture.context()) == original
        fixture.now += 0.001
    assert fixture.tokens.token(fixture.context()) != original
    assert fixture.token_posts == 2


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("method", ["initialize", "notifications/initialized", "tools/list"])
def test_mcp_refreshes_once_and_replays_same_request(monkeypatch, status, method):
    fixture = OAuthFixture(monkeypatch)
    attempts = []
    notification = method == "notifications/initialized"

    def gateway(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(status)
        if notification:
            return httpx.Response(202)
        payload = json.loads(request.content)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}}
        )

    mcp = fixture.mcp(gateway)
    mcp._session, mcp._protocol = "synthetic-session", "2025-03-26"
    result = mcp._request(
        method, {"cursor": "synthetic-cursor"}, fixture.context(), notification=notification
    )
    assert result == ({} if notification else {"tools": []})
    assert fixture.token_posts == 2
    assert fixture.secret_refreshes == [False, True]
    assert len(attempts) == 2
    assert attempts[0].content == attempts[1].content
    assert attempts[0].headers["Authorization"] != attempts[1].headers["Authorization"]
    for request in attempts:
        assert request.headers["Mcp-Session-Id"] == "synthetic-session"
        assert request.headers["MCP-Protocol-Version"] == "2025-03-26"


@pytest.mark.parametrize("statuses", [(403, 403), (401, 403), (403, 401)])
def test_persistent_denial_raises_after_one_shared_retry(monkeypatch, statuses):
    fixture = OAuthFixture(monkeypatch)
    attempts = []

    def gateway(request):
        attempts.append(request)
        return httpx.Response(
            statuses[len(attempts) - 1],
            headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
        )

    with pytest.raises(CatalogError, match="MCP request failed"):
        fixture.mcp(gateway)._request("tools/list", {}, fixture.context())
    assert len(attempts) == 2
    assert fixture.token_posts == 2


@pytest.mark.parametrize("failure", ["token_unavailable", "deadline"])
def test_failed_refresh_never_resends_old_token(monkeypatch, failure):
    fixture = OAuthFixture(monkeypatch)
    attempts = []

    def gateway(request):
        attempts.append(request)
        if failure == "deadline":
            fixture.now += 46
        else:
            fixture.token_status = 503
        return httpx.Response(403)

    with pytest.raises(CatalogError):
        fixture.mcp(gateway)._request("tools/list", {}, fixture.context())
    assert len(attempts) == 1
    assert fixture.token_posts == (1 if failure == "deadline" else 2)


def test_denied_later_page_does_not_return_partial_catalog(monkeypatch):
    fixture = OAuthFixture(monkeypatch)
    later_page_attempts = 0

    def gateway(request):
        nonlocal later_page_attempts
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}
        elif payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        elif "cursor" not in payload["params"]:
            result = {
                "tools": [{"name": "synthetic-tool", "inputSchema": {"type": "object"}}],
                "nextCursor": "synthetic-next",
            }
        else:
            later_page_attempts += 1
            return httpx.Response(403)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    with pytest.raises(CatalogError, match="MCP request failed"):
        fixture.mcp(gateway).list_tools(fixture.context())
    assert later_page_attempts == 2
    assert fixture.token_posts == 2


class SlowSecrets:
    """真实缓存之下的假 SDK；仅刷新读取推进时钟，不执行网络或 sleep。"""

    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = 0
        self.meta = SimpleNamespace(
            config=Config(
                connect_timeout=1,
                read_timeout=3,
                retries={"total_max_attempts": 1, "mode": "standard"},
            )
        )

    def get_secret_value(self, **kwargs):
        self.calls += 1
        if self.calls > 1:
            self.fixture.now += self.meta.config.read_timeout
        return {
            "VersionId": "synthetic-version",
            "SecretString": json.dumps(
                {"client_id": "synthetic-client", "client_secret": "synthetic-secret"}
            ),
        }


@pytest.mark.parametrize("status", [401, 403])
def test_recovery_does_not_start_slow_secret_fetch_with_little_time_left(monkeypatch, status):
    fixture = OAuthFixture(monkeypatch)
    sdk = SlowSecrets(fixture)
    fixture.tokens.cache = SecretsCache(sdk, clock=lambda: fixture.now)
    old_token = fixture.tokens.token(fixture.context())
    context = RequestContext("synthetic-short-budget", fixture.now + 1)
    attempts = []

    def gateway(request):
        attempts.append(request)
        fixture.now = context.deadline - 0.1
        return httpx.Response(status)

    with pytest.raises(CatalogError) as error:
        fixture.mcp(gateway)._request("tools/list", {}, context)

    assert (
        sdk.calls,
        fixture.token_posts,
        len(attempts),
        fixture.now,
        str(error.value),
    ) == (1, 1, 1, context.deadline - 0.1, "MCP request failed")
    assert attempts[0].headers["Authorization"] == f"Bearer {old_token}"
    assert fixture.now < context.deadline


@pytest.mark.parametrize("connect,read,exchange", [(1, 3, 5), (0.5, 1.5, 2), (2, 5, 3)])
@pytest.mark.parametrize("slack", [-0.01, 0, 0.01])
def test_recovery_admission_uses_configured_timeouts(monkeypatch, connect, read, exchange, slack):
    fixture = OAuthFixture(monkeypatch)
    sdk = SlowSecrets(fixture)
    sdk.meta.config = Config(
        connect_timeout=connect,
        read_timeout=read,
        retries={"total_max_attempts": 1, "mode": "standard"},
    )
    fixture.tokens.cache = SecretsCache(sdk, clock=lambda: fixture.now)
    fixture.tokens.timeout = exchange
    fixture.tokens.token(fixture.context())
    # Gateway 单次上限仍为已有的 10 秒，SDK/交换预算来自本用例配置。
    minimum = connect + read + exchange + 10
    context = RequestContext("synthetic-admission", fixture.now + minimum + slack)
    attempts = []

    def gateway(request):
        attempts.append(request)
        assert request.extensions["timeout"]["read"] <= context.deadline - fixture.now
        if len(attempts) == 1:
            return httpx.Response(403)
        payload = json.loads(request.content)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}}
        )

    if slack <= 0:
        with pytest.raises(CatalogError, match="^MCP request failed$"):
            fixture.mcp(gateway)._request("tools/list", {}, context)
        assert (sdk.calls, fixture.token_posts, len(attempts)) == (1, 1, 1)
    else:
        assert fixture.mcp(gateway)._request("tools/list", {}, context) == {"tools": []}
        assert (sdk.calls, fixture.token_posts, len(attempts)) == (2, 2, 2)
        assert attempts[0].headers["Authorization"] != attempts[1].headers["Authorization"]
    assert fixture.now < context.deadline


def test_recovery_rechecks_budget_before_rotating_secret_again(monkeypatch):
    fixture = OAuthFixture(monkeypatch)
    sdk = SlowSecrets(fixture)
    fixture.tokens.cache = SecretsCache(sdk, clock=lambda: fixture.now)
    fixture.tokens.token(fixture.context())
    context = RequestContext("synthetic-rotation-budget", fixture.now + 23)
    attempts = []
    token_attempts = []

    def exchange(request):
        token_attempts.append(request)
        assert request.extensions["timeout"]["read"] <= context.deadline - 10 - fixture.now
        fixture.now += 4
        return httpx.Response(401)

    fixture.tokens.client = httpx.Client(transport=httpx.MockTransport(exchange))

    def gateway(request):
        attempts.append(request)
        return httpx.Response(403)

    with pytest.raises(CatalogError, match="^MCP request failed$"):
        fixture.mcp(gateway)._request("tools/list", {}, context)
    assert (sdk.calls, len(token_attempts), len(attempts)) == (2, 1, 1)
    assert fixture.now < context.deadline - 10


@pytest.mark.parametrize(
    "config",
    [
        None,
        Config(connect_timeout=None, read_timeout=3),
        Config(connect_timeout=1, read_timeout=float("inf")),
        Config(connect_timeout=1, read_timeout=3, retries={"total_max_attempts": 2}),
        Config(
            connect_timeout=1,
            read_timeout=3,
            retries={"total_max_attempts": 1, "mode": "adaptive"},
        ),
    ],
)
def test_unknown_sdk_budget_does_not_start_recovery(monkeypatch, config):
    fixture = OAuthFixture(monkeypatch)
    sdk = SlowSecrets(fixture)
    fixture.tokens.cache = SecretsCache(sdk, clock=lambda: fixture.now)
    fixture.tokens.token(fixture.context())
    sdk.meta.config = config
    attempts = []

    def gateway(request):
        attempts.append(request)
        return httpx.Response(403)

    with pytest.raises(CatalogError, match="^MCP request failed$"):
        fixture.mcp(gateway)._request("tools/list", {}, fixture.context())
    assert (sdk.calls, fixture.token_posts, len(attempts)) == (1, 1, 1)


def test_custom_secret_source_without_known_budget_does_not_refresh(monkeypatch):
    fixture = OAuthFixture(monkeypatch)
    fixture.tokens.token(fixture.context())
    reads = []

    def read(*args, **kwargs):
        reads.append(True)
        pytest.fail("未知同步实现不应启动刷新")

    fixture.tokens.cache = SimpleNamespace(get=read)
    with pytest.raises(CatalogError, match="^MCP request failed$"):
        fixture.mcp(lambda request: httpx.Response(403))._request(
            "tools/list", {}, fixture.context()
        )
    assert reads == []


def test_low_budget_cached_token_does_not_require_secret_io(monkeypatch):
    fixture = OAuthFixture(monkeypatch)
    original = fixture.tokens.token(fixture.context())
    assert (
        fixture.tokens.token(RequestContext("synthetic-cache-hit", fixture.now + 0.1)) == original
    )
    assert fixture.secret_refreshes == [False]
    assert fixture.token_posts == 1


def test_recovery_does_not_wait_for_another_secret_fetch(monkeypatch):
    fixture = OAuthFixture(monkeypatch)
    fixture.tokens.token(fixture.context())
    held, release = threading.Event(), threading.Event()

    def hold_cache():
        with fixture._lock:
            held.set()
            assert release.wait(timeout=5)

    holder = threading.Thread(target=hold_cache)
    holder.start()
    try:
        assert held.wait(timeout=1)
        with pytest.raises(CatalogError, match="^MCP request failed$"):
            fixture.mcp(lambda request: httpx.Response(403))._request(
                "tools/list", {}, fixture.context()
            )
        assert fixture.secret_refreshes == [False]
        assert fixture.token_posts == 1
    finally:
        release.set()
        holder.join(timeout=1)
    assert not holder.is_alive()

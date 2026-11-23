"""未发出 Gateway 重放的两类恢复失败须可区分，重放名称须忠实于观测点。"""

import httpx
import pytest
from test_catalog_auth_refresh import OAuthFixture
from test_indexer_telemetry import observe, response


@pytest.mark.parametrize("reason", ["token", "budget", "admission"])
def test_auth_refresh_failure_and_skipped_replay_are_distinct(monkeypatch, capsys, reason):
    fixture = OAuthFixture(monkeypatch)
    attempts, deadlines = [], []

    def gateway(request):
        attempts.append(request)
        if reason == "token":
            fixture.token_status = 503
        elif reason == "budget":
            fixture.now = deadlines[0] - 10
        else:
            fixture.meta.config = None
        return httpx.Response(403)

    mcp = fixture.mcp(gateway)

    def action(request):
        deadlines.append(request.deadline)
        mcp._request("tools/list", {}, request)

    row = observe(monkeypatch, capsys, action, failed=True)
    assert len(attempts) == 1
    assert row["gateway_403_count"] == 1
    assert row["gateway_replay_count"] == 0
    assert row["gateway_replay_success_count"] == row["gateway_replay_failure_count"] == 0
    assert row["gateway_auth_refresh_failure_count"] == int(reason == "token")
    assert row["gateway_replay_skipped_count"] == int(reason != "token")
    assert not any(field.startswith("token_retry_") for field in row)


def test_successful_gateway_replay_has_no_refresh_failure_or_skip(monkeypatch, capsys):
    fixture = OAuthFixture(monkeypatch)
    attempts = []

    def gateway(request):
        attempts.append(request)
        return httpx.Response(401) if len(attempts) == 1 else response(request)

    mcp = fixture.mcp(gateway)
    row = observe(monkeypatch, capsys, lambda request: mcp._request("tools/list", {}, request))
    assert row["gateway_replay_count"] == row["gateway_replay_success_count"] == 1
    assert row["gateway_replay_failure_count"] == 0
    assert row["gateway_auth_refresh_failure_count"] == row["gateway_replay_skipped_count"] == 0
    assert not any(field.startswith("token_retry_") for field in row)

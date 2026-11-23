"""Deployment settings and scheduler inputs contain no inferred identities."""

from datetime import UTC, datetime

import pytest

from semantic_service.config import ServiceSettings, scheduled_tick_is_current


@pytest.fixture
def settings_env():
    return {
        "SEMSEARCH_ACCOUNT": "000000000000",
        "AWS_REGION": "cn-north-1",
        "SEMSEARCH_GATEWAY_ID": "fixture-gateway",
        "SEMSEARCH_SEARCH_TARGET_ID": "fixture-target",
        "SEMSEARCH_SEARCH_TOOL_NAME": "toolsearch___search_tools",
        "SEMSEARCH_BUCKET": "fixture-snapshots",
        "SEMSEARCH_TABLE": "fixture-control",
        "SEMSEARCH_PROTECTED_TABLE": "fixture-protected",
    }


def test_service_defaults_and_secret_not_in_configuration(settings_env):
    settings = ServiceSettings.from_env(settings_env | {"OPENAI_API_KEY": "must-not-be-read"})
    assert settings.poll_seconds == 60
    assert settings.indexer_budget_seconds == 45
    assert settings.enhancer_budget_seconds == 240
    assert settings.search_tool_name == "toolsearch___search_tools"
    assert settings.table == "fixture-control"
    assert settings.protected_table == "fixture-protected"
    assert "must-not-be-read" not in repr(settings)


def test_physical_protected_table_binding_is_required(settings_env):
    settings_env.pop("SEMSEARCH_PROTECTED_TABLE")
    with pytest.raises(ValueError, match="SEMSEARCH_PROTECTED_TABLE"):
        ServiceSettings.from_env(settings_env)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("SEMSEARCH_ACCOUNT", "../other"),
        ("AWS_REGION", "us-east-1"),
        ("SEMSEARCH_GATEWAY_ID", "../gateway"),
        ("SEMSEARCH_SEARCH_TARGET_ID", ""),
        ("SEMSEARCH_SEARCH_TOOL_NAME", "search_tools"),
        ("SEMSEARCH_BUCKET", ""),
        ("SEMSEARCH_TABLE", ""),
        ("SEMSEARCH_PROTECTED_TABLE", ""),
        ("SEMSEARCH_PROTECTED_TABLE", "../protected"),
        ("SEMSEARCH_PROTECTED_TABLE", "fixture-control"),
        ("SEMSEARCH_POLL_SECONDS", "0"),
        ("SEMSEARCH_POLL_SECONDS", "nan"),
    ],
)
def test_service_rejects_invalid_namespace_or_budget(settings_env, key, value):
    with pytest.raises(ValueError):
        ServiceSettings.from_env(settings_env | {key: value})


def test_tick_age_and_gateway_binding():
    now = datetime(2026, 1, 1, 0, 2, tzinfo=UTC)
    assert scheduled_tick_is_current({"time": "2026-01-01T00:01:30Z"}, now=now)
    assert not scheduled_tick_is_current({"time": "2026-01-01T00:00:00Z"}, now=now)
    assert not scheduled_tick_is_current({"time": "2026-01-01T00:03:00Z"}, now=now)
    assert scheduled_tick_is_current({}, now=now)
    for event in ({"gatewayId": "other"}, {"time": "bad"}, {"time": 1}, [], None):
        with pytest.raises(ValueError):
            scheduled_tick_is_current(event, now=now)

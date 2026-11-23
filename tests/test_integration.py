"""The integration harness may override the fixture only in an explicitly authorized harness."""

import time

import pytest

from semantic_core.providers import RequestContext, validate_embedding_result


@pytest.fixture
def live_embedding_provider():
    pytest.skip("Live provider fixture is not configured; no credentials are discovered")


@pytest.mark.integration
def test_live_embedding_contract(live_embedding_provider):
    provider = live_embedding_provider
    profile = provider.describe()
    result = provider.embed(
        ["合成测试"], "query", RequestContext("integration-fixture", time.monotonic() + 10)
    )
    validate_embedding_result(result, profile, 1)

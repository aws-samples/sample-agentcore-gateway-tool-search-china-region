"""A reported model name does not select or verify the proxy's rerank route."""

import json
import time

import httpx
import pytest

from semantic_core.openai_compatible import OpenAICompatibleConfig
from semantic_core.providers import ModelProfile, RequestContext, RerankDocument
from semantic_core.rerank import HTTPRerankProvider, RerankCodec, RerankConfig


@pytest.mark.parametrize("path", ["/rerank", "/ranking/custom"])
@pytest.mark.parametrize("score_field", ["relevance_score", "score"])
def test_proxy_model_uses_only_the_configured_route_and_score_field(path, score_field):
    """Both routes are synthetic contracts, not evidence for a real endpoint."""
    requests = []
    transport = OpenAICompatibleConfig(
        "https://proxy.fixture.invalid/v1",
        ("proxy.fixture.invalid",),
        auth_header_name="Authorization",
        auth_header_format="Bearer {token}",
    )
    settings = RerankConfig(
        profile=ModelProfile("example-rerank-model", "configured-proxy-revision"),
        codec=RerankCodec(path=path, envelope="flat", score_field=score_field),
        max_documents=2,
        max_query_bytes=32,
        max_document_bytes=64,
        max_total_bytes=256,
    )

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://proxy.fixture.invalid/v1" + path
        assert request.headers["Authorization"] == "Bearer offline-fixture-only"
        assert json.loads(request.content) == {
            "model": "example-rerank-model",
            "query": "cancel",
            "documents": ["order status", "cancel order"],
            "top_n": 2,
            "return_documents": False,
        }
        return httpx.Response(
            200,
            json={
                "model": "example-rerank-model",
                "results": [
                    {"index": 1, score_field: 0.9},
                    {"index": 0, score_field: 0.2},
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        result = HTTPRerankProvider(
            client, transport, settings, credential="offline-fixture-only"
        ).rerank(
            "cancel",
            [RerankDocument("status", "order status"), RerankDocument("cancel", "cancel order")],
            2,
            RequestContext("proxy-rerank-fixture", time.monotonic() + 10),
        )
    assert [item.id for item in result.items] == ["cancel", "status"]
    assert result.profile == settings.profile
    assert len(requests) == 1

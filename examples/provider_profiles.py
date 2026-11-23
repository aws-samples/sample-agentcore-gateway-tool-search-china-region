"""Compare proxy, Bailian and portable configurations using MockTransport only.

The generic and vLLM model IDs are synthetic served-model names. These fixtures
demonstrate request/response contracts, not service or model availability. The
example never reads credentials and cannot select a real network transport.
The proxy uses a caller-supplied /v1 base, never a discovered or fixed real host.
Its 1024 dimensions describe a planned fixture index space, not evidence from a
model listing. qwen3-rerank remains disabled with its route awaiting a real probe.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, replace
from typing import cast

import httpx
import numpy as np

from semantic_core.openai_compatible import (
    ChatConfig,
    EmbeddingConfig,
    OpenAIChatProvider,
    OpenAICompatibleConfig,
    OpenAIEmbeddingProvider,
    bailian_chat_config,
    bailian_embedding_config,
)
from semantic_core.providers import ModelProfile, RequestContext

_PROFILE_NAMES = ("proxy", "bailian", "openai-compatible", "vllm")
_SYNTHETIC_CREDENTIAL = "offline-fixture-only"
_EMBEDDING_INPUT = ("取消未发货订单", "Find order cancellation")
_DESCRIPTION = "Cancel an unshipped order using its order ID."


@dataclass(frozen=True)
class ExampleProfile:
    transport: OpenAICompatibleConfig
    embedding: EmbeddingConfig
    chat: ChatConfig


def select_profile(
    name: str, *, proxy_transport: OpenAICompatibleConfig | None = None
) -> ExampleProfile:
    if name == "proxy":
        if not isinstance(proxy_transport, OpenAICompatibleConfig):
            raise ValueError("proxy profile requires caller-supplied transport")
        return ExampleProfile(
            proxy_transport,
            EmbeddingConfig(
                ModelProfile("text-embedding-v4", "configured-fixture-v1", 1024),
                "/embeddings",
                max_batch=4,
                max_text_bytes=512,
            ),
            ChatConfig(
                ModelProfile("qwen-plus", "configured-fixture-v1"),
                "/chat/completions",
                max_text_bytes=512,
                max_tokens=256,
            ),
        )
    if name == "bailian":
        return ExampleProfile(
            OpenAICompatibleConfig(
                "https://bailian.fixture.invalid",
                ("bailian.fixture.invalid",),
                auth_header_name="Authorization",
            ),
            bailian_embedding_config("configured-fixture-v1"),
            bailian_chat_config(),
        )
    if name == "openai-compatible":
        return ExampleProfile(
            OpenAICompatibleConfig(
                "https://compatible.fixture.invalid/tenant",
                ("compatible.fixture.invalid",),
                auth_header_name="api-key",
                auth_header_format="{token}",
            ),
            EmbeddingConfig(
                ModelProfile("fixture-embedding-small", "fixture-v1", 4),
                "/v1/embeddings",
                max_batch=4,
                max_text_bytes=512,
            ),
            ChatConfig(
                ModelProfile("fixture-chat-small", "fixture-v1"),
                "/v1/chat/completions",
                max_text_bytes=512,
                max_tokens=256,
            ),
        )
    if name == "vllm":
        return ExampleProfile(
            OpenAICompatibleConfig(
                "https://vllm.fixture.invalid:8443/serving",
                ("vllm.fixture.invalid",),
                auth_header_name="Authorization",
            ),
            EmbeddingConfig(
                ModelProfile("fixture-served-embedding", "fixture-v1", 3),
                "/v1/embeddings",
                max_batch=4,
                max_text_bytes=512,
                send_dimensions=False,
                encoding_format=None,
            ),
            ChatConfig(
                ModelProfile("fixture-served-chat", "fixture-v1"),
                "/v1/chat/completions",
                max_text_bytes=512,
                max_tokens=256,
                json_mode=False,
            ),
        )
    raise ValueError("unsupported example profile")


def run(profile_name: str = "proxy") -> dict[str, object]:
    # The runnable demonstration supplies an explicit synthetic configuration.
    # select_profile itself has no default or discovered proxy host/allowlist.
    proxy_transport = (
        OpenAICompatibleConfig(
            "https://proxy.fixture.invalid/v1",
            ("proxy.fixture.invalid",),
            auth_header_name="Authorization",
            auth_header_format="Bearer {token}",
        )
        if profile_name == "proxy"
        else None
    )
    profile = select_profile(profile_name, proxy_transport=proxy_transport)
    # Separate stage budgets: embedding keeps its short search-path timeout.
    # Chat may generate 2048 tokens and retry once for schema repair. These
    # illustrative 30s/65s budgets need live latency measurements before deployment.
    chat_transport = replace(profile.transport, timeout_seconds=30.0)
    chat_total_budget = 2 * chat_transport.timeout_seconds + 5.0
    requests: list[dict[str, object]] = []
    dimension = profile.embedding.profile.dimension
    assert dimension is not None

    # Returned below via httpx.MockTransport(handler); the closure is used.
    # nosemgrep: useless-inner-function
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        header_name = profile.transport.auth_header_name
        assert header_name is not None
        assert request.headers[header_name] == profile.transport.auth_header_format.format(
            token=_SYNTHETIC_CREDENTIAL
        )
        body = cast(dict[str, object], json.loads(request.content))
        requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "model": body["model"],
                "fields": sorted(body),
            }
        )
        base_url = profile.transport.base_url.rstrip("/")
        if str(request.url) == base_url + profile.embedding.path:
            expected: dict[str, object] = {
                "model": profile.embedding.profile.model_id,
                "input": list(_EMBEDDING_INPUT),
            }
            if profile.embedding.send_dimensions:
                expected["dimensions"] = dimension
            if profile.embedding.encoding_format is not None:
                expected["encoding_format"] = profile.embedding.encoding_format
            assert body == expected
            return httpx.Response(
                200,
                json={
                    "model": profile.embedding.profile.model_id,
                    "data": [
                        {"index": 1, "embedding": [4, 3] + [0] * (dimension - 2)},
                        {"index": 0, "embedding": [3, 4] + [0] * (dimension - 2)},
                    ],
                    "usage": {"total_tokens": 8, "prompt_tokens_details": {"cached_tokens": 0}},
                },
            )
        assert str(request.url) == base_url + profile.chat.path
        expected_fields = {"model", "messages", "temperature", "max_tokens"} | set(
            profile.chat.extra_body
        )
        if profile.chat.json_mode:
            expected_fields.add("response_format")
            assert body["response_format"] == {"type": "json_object"}
        assert set(body) == expected_fields
        assert body["model"] == profile.chat.profile.model_id
        assert body["max_tokens"] == profile.chat.max_tokens
        assert body["temperature"] == profile.chat.temperature
        for key, value in profile.chat.extra_body.items():
            assert body[key] == value
        messages = cast(list[dict[str, str]], body["messages"])
        assert messages[0]["role"] == "system" and "JSON" in messages[0]["content"]
        assert messages[1] == {
            "role": "user",
            "content": json.dumps({"tool_description": _DESCRIPTION}, ensure_ascii=False),
        }
        return httpx.Response(
            200,
            json={
                "model": profile.chat.profile.model_id,
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"queries": [*_EMBEDDING_INPUT, _EMBEDDING_INPUT[0]]},
                                ensure_ascii=False,
                            )
                        },
                    }
                ],
                "usage": {"total_tokens": 12, "completion_tokens_details": {"reasoning_tokens": 0}},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        embedding = OpenAIEmbeddingProvider(
            client, profile.transport, profile.embedding, credential=_SYNTHETIC_CREDENTIAL
        ).embed(
            _EMBEDDING_INPUT,
            "document",
            RequestContext("profile-embedding", time.monotonic() + 10),
        )
        generated = OpenAIChatProvider(
            client, chat_transport, profile.chat, credential=_SYNTHETIC_CREDENTIAL
        ).generate_queries(
            _DESCRIPTION,
            ("zh", "en"),
            3,
            RequestContext("profile-chat", time.monotonic() + chat_total_budget),
        )
    np.testing.assert_allclose(embedding.vectors[:, :2], [[0.6, 0.8], [0.8, 0.6]])
    return {
        "profile": profile_name,
        "transport": "MockTransport",
        "fixture_only": True,
        "embedding_model": embedding.profile.model_id,
        "embedding_shape": list(embedding.vectors.shape),
        "embedding_dimension_basis": "configured fixture index space",
        "vectors_normalized": bool(np.allclose(np.linalg.norm(embedding.vectors, axis=1), 1.0)),
        "queries": list(generated.queries),
        "auth_header": profile.transport.auth_header_name,
        "rerank_enabled": False,
        "rerank_candidate": (
            {"model": "qwen3-rerank", "path": None, "status": "requires_live_probe"}
            if profile_name == "proxy"
            else None
        ),
        "requests": requests,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=_PROFILE_NAMES, default="proxy")
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.profile), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

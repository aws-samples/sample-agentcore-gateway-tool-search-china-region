"""Chat repair retries report all known usage counters, not just the final reply."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from semantic_core.openai_compatible import (
    ChatConfig,
    OpenAIChatProvider,
    OpenAICompatibleConfig,
)
from semantic_core.providers import (
    ErrorCode,
    GeneratedQueries,
    ModelProfile,
    ProviderError,
    RequestContext,
)

_MISSING = object()


def response(content: str, usage: object = _MISSING) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": "fixture-chat",
        "choices": [{"finish_reason": "stop", "message": {"content": content}}],
    }
    if usage is not _MISSING:
        payload["usage"] = usage
    return payload


def generate(responses: list[dict[str, object]], requests: list[httpx.Request]) -> GeneratedQueries:
    replies = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://usage.fixture.invalid/v1/chat/completions"
        requests.append(request)
        return httpx.Response(200, json=next(replies))

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        provider = OpenAIChatProvider(
            client,
            OpenAICompatibleConfig("https://usage.fixture.invalid", ("usage.fixture.invalid",)),
            ChatConfig(ModelProfile("fixture-chat", "fixture-v1"), "/v1/chat/completions", 256),
        )
        return provider.generate_queries(
            "Find order status", ("en",), 1, RequestContext("usage-retry", time.monotonic() + 10)
        )


def test_schema_repair_sums_all_five_known_usage_counters() -> None:
    requests: list[httpx.Request] = []
    result = generate(
        [
            response(
                "invalid JSON",
                {
                    "total_tokens": 100,
                    "prompt_tokens": 90,
                    "completion_tokens": 10,
                    "input_tokens": 70,
                    "output_tokens": 30,
                },
            ),
            response(
                '{"queries":["Find order status"]}',
                {
                    "total_tokens": 50,
                    "prompt_tokens": 45,
                    "completion_tokens": 5,
                    "input_tokens": 40,
                    "output_tokens": 10,
                },
            ),
        ],
        requests,
    )
    assert result.usage == {
        "total_tokens": 150,
        "prompt_tokens": 135,
        "completion_tokens": 15,
        "input_tokens": 110,
        "output_tokens": 40,
    }
    assert result.queries == ("Find order status",)
    assert len(requests) == 2
    assert requests[0].content != requests[1].content


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (
            {"prompt_tokens": 20, "total_tokens": 22},
            {"completion_tokens": 3, "total_tokens": 7},
            {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 29},
        ),
        (
            {"prompt_tokens": 12, "input_tokens": 0},
            {"completion_tokens": 3, "output_tokens": 3},
            {"prompt_tokens": 12, "input_tokens": 0, "completion_tokens": 3, "output_tokens": 3},
        ),
        ({"total_tokens": 100}, _MISSING, {"total_tokens": 100}),
        (_MISSING, {"total_tokens": 50}, {"total_tokens": 50}),
        (_MISSING, _MISSING, {}),
        ({"input_tokens": 0}, {}, {"input_tokens": 0}),
    ],
)
def test_retry_usage_preserves_mixed_and_missing_known_counters(
    first: object, second: object, expected: dict[str, int]
) -> None:
    requests: list[httpx.Request] = []
    result = generate(
        [
            response('{"queries":["q"],"unknown":"schema failure"}', first),
            response('{"queries":["Find order status"]}', second),
        ],
        requests,
    )
    assert result.usage == expected
    assert len(requests) == 2


def test_retry_usage_ignores_unknown_details_on_both_attempts() -> None:
    requests: list[httpx.Request] = []
    result = generate(
        [
            response(
                "not JSON",
                {
                    "total_tokens": 100,
                    "prompt_tokens_details": {"cached_tokens": 80},
                    "future_extension": {"private_fixture_metadata": "unused"},
                },
            ),
            response(
                '{"queries":["Find order status"]}',
                {
                    "total_tokens": 50,
                    "completion_tokens_details": {"reasoning_tokens": 10},
                    "future_scalar": 999,
                },
            ),
        ],
        requests,
    )
    assert result.usage == {"total_tokens": 150}
    assert "private_fixture_metadata" not in json.dumps(dict(result.usage))
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {
                "total_tokens": 50,
                "prompt_tokens": 45,
                "completion_tokens": 5,
                "input_tokens": 40,
                "output_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 20},
            },
            {
                "total_tokens": 50,
                "prompt_tokens": 45,
                "completion_tokens": 5,
                "input_tokens": 40,
                "output_tokens": 10,
            },
        ),
        (_MISSING, {}),
    ],
)
def test_single_attempt_usage_is_unchanged(usage: object, expected: dict[str, int]) -> None:
    requests: list[httpx.Request] = []
    result = generate([response('{"queries":["Find order status"]}', usage)], requests)
    assert result.usage == expected
    assert len(requests) == 1


@pytest.mark.parametrize("invalid_count", [-1, True, 1.5, "100", None, {"tokens": 100}])
@pytest.mark.parametrize("invalid_attempt", [1, 2])
def test_invalid_known_usage_remains_an_error_and_is_not_retried(
    invalid_count: object, invalid_attempt: int
) -> None:
    requests: list[httpx.Request] = []
    replies = [response("not JSON", {"total_tokens": 100})] if invalid_attempt == 2 else []
    replies.append(response('{"queries":["Find order status"]}', {"total_tokens": invalid_count}))
    with pytest.raises(ProviderError) as error:
        generate(replies, requests)
    assert error.value.code is ErrorCode.INVALID_RESPONSE
    assert len(requests) == invalid_attempt

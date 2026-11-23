"""JSON extension readback and immutable snapshots across both HTTP providers."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Mapping
from typing import Literal, cast

import httpx
import pytest

from semantic_core._http import HTTPConfig
from semantic_core.openai_compatible import (
    ChatConfig,
    EmbeddingConfig,
    OpenAIChatProvider,
    OpenAIEmbeddingProvider,
)
from semantic_core.providers import ModelProfile, RequestContext

Kind = Literal["embedding", "chat"]
Config = EmbeddingConfig | ChatConfig
HOST = "extension.example.invalid"


def extensions() -> dict[str, object]:
    return {
        "stop": ["\n", "###"],
        "extension": {
            "routes": [{"weights": [0.25, 0.75], "labels": [["zh"], ["en"]]}],
            "options": {"bias": [[1, True, None], {"tags": ["fixture"]}]},
        },
    }


def settings(kind: Kind, extra_body: Mapping[str, object]) -> Config:
    if kind == "embedding":
        return EmbeddingConfig(
            ModelProfile("fixture-embedding", "v1", 2),
            "/v1/embeddings",
            max_batch=1,
            max_text_bytes=128,
            extra_body=extra_body,
        )
    return ChatConfig(
        ModelProfile("fixture-chat", "v1"),
        "/v1/chat/completions",
        max_text_bytes=128,
        extra_body=extra_body,
    )


def call(http: httpx.Client, config: Config) -> None:
    transport = HTTPConfig(f"https://{HOST}", (HOST,))
    context = RequestContext("extension-arrays", time.monotonic() + 5)
    if isinstance(config, EmbeddingConfig):
        result = OpenAIEmbeddingProvider(http, transport, config).embed(
            ["synthetic text"], "query", context
        )
        assert result.vectors.shape == (1, 2)
    else:
        generated = OpenAIChatProvider(http, transport, config).generate_queries(
            "Synthetic tool description", ("en",), 1, context
        )
        assert generated.queries == ("synthetic query",)


def response(config: Config) -> httpx.Response:
    if isinstance(config, EmbeddingConfig):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [3.0, 4.0]}]})
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": '{"queries":["synthetic query"]}'},
                }
            ]
        },
    )


@pytest.mark.parametrize("kind", ["embedding", "chat"])
def test_array_extension_readback_equals_input_json(kind: Kind) -> None:
    original = extensions()
    config = settings(kind, original)
    assert config.extra_body == original
    assert original == config.extra_body
    assert dict(config.extra_body) == original
    assert list(config.extra_body) == list(original)
    assert len(config.extra_body) == len(original)
    assert config.extra_body.get("stop") == ["\n", "###"]
    assert config.extra_body.get("missing", "fallback") == "fallback"


@pytest.mark.parametrize("kind", ["embedding", "chat"])
def test_array_extension_wire_values_equal_configuration_readback(kind: Kind) -> None:
    config = settings(kind, extensions())
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"https://{HOST}{config.path}"
        body = cast(dict[str, object], json.loads(request.content))
        requests.append(body)
        for key, value in config.extra_body.items():
            assert body[key] == value
        return response(config)

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http:
        call(http, config)
    assert len(requests) == 1
    assert {key: requests[0][key] for key in config.extra_body} == extensions()


@pytest.mark.parametrize("kind", ["embedding", "chat"])
def test_original_array_mutation_cannot_change_saved_configuration_or_requests(kind: Kind) -> None:
    original = extensions()
    expected = copy.deepcopy(original)
    config = settings(kind, original)
    cast(list[str], original["stop"]).append("source mutation")
    cast(dict[str, object], original["extension"])["routes"] = []
    original["new_field"] = ["source mutation"]
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = cast(dict[str, object], json.loads(request.content))
        requests.append(body)
        assert {key: body[key] for key in expected} == expected
        assert "new_field" not in body
        return response(config)

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http:
        call(http, config)
        call(http, config)
    assert len(requests) == 2
    assert config.extra_body == expected


@pytest.mark.parametrize("kind", ["embedding", "chat"])
def test_nested_readback_mutation_cannot_change_configuration_or_future_requests(
    kind: Kind,
) -> None:
    expected = extensions()
    config = settings(kind, expected)
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = cast(dict[str, object], json.loads(request.content))
        requests.append(body)
        assert {key: body[key] for key in expected} == expected
        return response(config)

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http:
        call(http, config)
        stop = config.extra_body["stop"]
        assert isinstance(stop, list)
        stop.append("readback mutation")
        nested = config.extra_body["extension"]
        assert isinstance(nested, dict)
        routes = nested["routes"]
        assert isinstance(routes, list)
        routes[0]["weights"][0] = 99
        routes[0]["labels"][0].append("readback mutation")
        nested["options"]["bias"][1]["tags"].clear()
        nested["new_field"] = ["readback mutation"]
        items_copy = dict(config.extra_body.items())
        cast(list[str], items_copy["stop"]).clear()
        call(http, config)
    assert len(requests) == 2
    assert config.extra_body == expected
    assert config.extra_body["stop"] is not config.extra_body["stop"]
    assert config.extra_body["extension"] is not config.extra_body["extension"]


@pytest.mark.parametrize("kind", ["embedding", "chat"])
def test_outer_extension_mapping_still_rejects_mutation(kind: Kind) -> None:
    config = settings(kind, extensions())
    with pytest.raises(TypeError):
        cast(dict[str, object], config.extra_body)["stop"] = ["replacement"]
    with pytest.raises(KeyError):
        _ = config.extra_body["missing"]

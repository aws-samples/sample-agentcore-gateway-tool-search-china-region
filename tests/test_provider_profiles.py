"""Run every documented provider profile with a hard no-network guard."""

from __future__ import annotations

import json
import runpy
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import semantic_core._http as provider_http
import semantic_core.providers as provider_contracts
from semantic_core.openai_compatible import OpenAICompatibleConfig

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "provider_profiles.py"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        pytest.fail("provider profile examples must only use MockTransport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)


@pytest.mark.parametrize(
    ("name", "origin", "embedding_path", "chat_path", "dimension", "auth_header"),
    [
        (
            "proxy",
            "https://models.example.com/v1",
            "/embeddings",
            "/chat/completions",
            1024,
            "Authorization",
        ),
        (
            "origin",
            "https://models.example.com",
            "/v1/embeddings",
            "/v1/chat/completions",
            1024,
            "Authorization",
        ),
        (
            "openai-compatible",
            "https://models.example.com/v1",
            "/embeddings",
            "/chat/completions",
            4,
            "api-key",
        ),
        (
            "vllm",
            "https://models.example.com/v1",
            "/embeddings",
            "/chat/completions",
            3,
            "Authorization",
        ),
    ],
)
def test_every_profile_exercises_expected_offline_wire_contract(
    name: str,
    origin: str,
    embedding_path: str,
    chat_path: str,
    dimension: int,
    auth_header: str,
) -> None:
    run = runpy.run_path(str(EXAMPLE))["run"]
    result = run(name)
    assert result["profile"] == name
    assert result["transport"] == "MockTransport"
    assert result["fixture_only"] is True
    assert result["embedding_shape"] == [2, dimension]
    assert result["vectors_normalized"] is True
    assert result["queries"] == ["取消未发货订单", "Find order cancellation"]
    assert result["auth_header"] == auth_header
    assert result["rerank_enabled"] is False
    requests = result["requests"]
    assert [request["url"] for request in requests] == [
        origin + embedding_path,
        origin + chat_path,
    ]
    assert all(request["method"] == "POST" for request in requests)
    embedding_fields, chat_fields = [request["fields"] for request in requests]
    assert ("dimensions" in embedding_fields) is (name != "vllm")
    assert ("encoding_format" in embedding_fields) is (name != "vllm")
    assert ("response_format" in chat_fields) is (name != "vllm")
    expected_chat_fields = {"max_tokens", "messages", "model", "temperature"}
    if name != "vllm":
        expected_chat_fields.add("response_format")
    assert set(chat_fields) == expected_chat_fields
    assert "offline-fixture-only" not in json.dumps(result)


def test_cli_defaults_to_proxy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", [str(EXAMPLE)])
    runpy.run_path(str(EXAMPLE), run_name="__main__")
    result = json.loads(capsys.readouterr().out)
    assert result["profile"] == "proxy"
    assert result["fixture_only"] is True


def test_run_defaults_to_proxy_and_keeps_rerank_unconfigured() -> None:
    run = runpy.run_path(str(EXAMPLE))["run"]
    result = run()
    assert result["profile"] == "proxy"
    assert result["embedding_model"] == "example-embedding-model"
    assert [request["model"] for request in result["requests"]] == [
        "example-embedding-model",
        "example-chat-model",
    ]
    assert result["rerank_enabled"] is False
    assert result["rerank_candidate"] == {
        "model": "example-rerank-model",
        "path": None,
        "status": "requires_live_probe",
    }
    assert result["embedding_dimension_basis"] == "configured fixture index space"
    assert len(result["requests"]) == 2
    assert [request["url"] for request in result["requests"]] == [
        "https://models.example.com/v1/embeddings",
        "https://models.example.com/v1/chat/completions",
    ]
    assert set(result["requests"][1]["fields"]) == {
        "max_tokens",
        "messages",
        "model",
        "response_format",
        "temperature",
    }


def test_proxy_profile_requires_caller_supplied_transport() -> None:
    select_profile = runpy.run_path(str(EXAMPLE))["select_profile"]
    with pytest.raises(ValueError, match="proxy profile requires caller-supplied transport"):
        select_profile("proxy")


def test_proxy_profile_preserves_exact_caller_host_and_allowlist() -> None:
    select_profile = runpy.run_path(str(EXAMPLE))["select_profile"]
    supplied = OpenAICompatibleConfig(
        "https://configured-proxy.fixture.invalid/v1",
        ("configured-proxy.fixture.invalid",),
        auth_header_name="Authorization",
        auth_header_format="Bearer {token}",
    )
    profile = select_profile("proxy", proxy_transport=supplied)
    assert profile.transport is supplied
    assert profile.embedding.path == "/embeddings"
    assert profile.chat.path == "/chat/completions"
    assert profile.embedding.profile.model_id == "example-embedding-model"
    assert profile.embedding.profile.dimension == 1024
    assert profile.chat.profile.model_id == "example-chat-model"
    assert profile.chat.extra_body == {}


def test_unknown_profile_is_rejected() -> None:
    run = runpy.run_path(str(EXAMPLE))["run"]
    with pytest.raises(ValueError, match="unsupported example profile"):
        run("unconfigured")


def test_example_accepts_list_typed_chat_extensions() -> None:
    run = runpy.run_path(str(EXAMPLE))["run"]
    select = run.__globals__["select_profile"]

    def with_array_extensions(*args: object, **kwargs: object) -> object:
        profile = select(*args, **kwargs)
        return replace(
            profile,
            chat=replace(
                profile.chat,
                extra_body={"stop": ["\n", "###"], "options": {"labels": ["a", "b"]}},
            ),
        )

    run.__globals__["select_profile"] = with_array_extensions
    result = run()
    assert {"stop", "options"} <= set(result["requests"][1]["fields"])
    assert result["queries"] == ["取消未发货订单", "Find order cancellation"]


@pytest.mark.parametrize("name", ["proxy", "origin", "openai-compatible", "vllm"])
def test_example_chat_has_separate_timeout_and_can_finish_after_embedding_budget(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = runpy.run_path(str(EXAMPLE))["run"]
    now = [100.0]
    clock = SimpleNamespace(monotonic=lambda: now[0])
    # Replace module bindings, leaving the process-wide stdlib clock untouched.
    monkeypatch.setattr(provider_http, "time", clock)
    monkeypatch.setattr(provider_contracts, "time", clock)
    run.__globals__["time"] = clock
    original = httpx.MockTransport.handle_request
    timeouts: list[object] = []

    def delayed(transport: httpx.MockTransport, request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        response = original(transport, request)
        if request.url.path.endswith("/chat/completions"):
            now[0] += 1.1
        return response

    monkeypatch.setattr(httpx.MockTransport, "handle_request", delayed)
    result = run(name)
    assert result["queries"] == ["取消未发货订单", "Find order cancellation"]
    assert timeouts == [1.0, 30.0]


def test_example_chat_deadline_covers_both_schema_repair_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = runpy.run_path(str(EXAMPLE))["run"]
    now = [100.0]
    clock = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr(provider_http, "time", clock)
    monkeypatch.setattr(provider_contracts, "time", clock)
    run.__globals__["time"] = clock
    original = httpx.MockTransport.handle_request
    chat_attempts = 0
    timeouts: list[object] = []

    def delayed_repair(transport: httpx.MockTransport, request: httpx.Request) -> httpx.Response:
        nonlocal chat_attempts
        response = original(transport, request)
        if request.url.path.endswith("/chat/completions"):
            chat_attempts += 1
            timeouts.append(request.extensions["timeout"]["read"])
            now[0] += 25.0
            if chat_attempts == 1:
                body = response.json()
                body["choices"][0]["message"]["content"] = "invalid JSON"
                return httpx.Response(200, json=body)
        return response

    monkeypatch.setattr(httpx.MockTransport, "handle_request", delayed_repair)
    result = run()
    assert chat_attempts == 2
    assert now[0] == 150.0
    assert timeouts == [30.0, 30.0]
    assert result["queries"] == ["取消未发货订单", "Find order cancellation"]

"""Portable transport checks use explicit synthetic credentials and MockTransport."""

import json
import time
from dataclasses import replace

import httpx
import pytest

from semantic_core._http import HTTPConfig, _HTTPProvider, freeze_body
from semantic_core.openai_compatible import EmbeddingConfig, OpenAIEmbeddingProvider
from semantic_core.providers import ErrorCode, ModelProfile, ProviderError, RequestContext


def context():
    return RequestContext("portable-http-fixture", time.monotonic() + 10)


def test_late_http_result_is_rejected_and_response_closed(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("semantic_core._http.time.monotonic", lambda: clock[0])
    response = httpx.Response(200, json={})

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request):
        clock[0] += 0.46
        return response

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        provider = _HTTPProvider(
            client,
            HTTPConfig("https://model.example", ("model.example",), timeout_seconds=0.45),
            ModelProfile("fixture", "v1"),
        )
        with pytest.raises(ProviderError) as error:
            provider._post("/embeddings", {}, RequestContext("late", 101.0))
    assert error.value.code is ErrorCode.TIMEOUT
    # is_closed is a boolean property; calling it would be incorrect.
    # nosemgrep: is-function-without-parentheses
    assert response.is_closed


def test_configured_https_prefix_and_port_are_usable():
    config = HTTPConfig("https://model.example:8443/tenant/v1", ("model.example",))
    urls = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        provider = _HTTPProvider(client, config, ModelProfile("local", "one"))
        provider._post("/embeddings", {}, context())
    assert urls == ["https://model.example:8443/tenant/v1/embeddings"]


def test_unknown_version_metadata_does_not_break_standard_embedding_response():
    response = {
        "model": "local",
        "revision": {"vendor": "extension"},
        "config_version": False,
        "data": [{"index": 0, "embedding": [3, 4], "extension": {"ignored": True}}],
        "usage": {"total_tokens": 2, "completion_tokens_details": {"reasoning_tokens": 0}},
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)),
        trust_env=False,
    ) as client:
        provider = OpenAIEmbeddingProvider(
            client,
            HTTPConfig("https://model.example", ("model.example",)),
            EmbeddingConfig(ModelProfile("local", "one", 2), "/v1/embeddings", 16, 2048),
        )
        result = provider.embed(["text"], "query", context())
    assert result.vectors.shape == (1, 2)
    assert result.profile == ModelProfile("local", "one", 2)
    assert result.usage == {"total_tokens": 2}


def test_generic_resource_limits_are_not_bound_to_legacy_bailian_ceilings():
    config = HTTPConfig(
        "https://model.example",
        ("model.example",),
        timeout_seconds=61,
        max_response_bytes=12 * 1024 * 1024,
    )
    assert config.timeout_seconds == 61
    assert config.max_response_bytes == 12 * 1024 * 1024


@pytest.mark.parametrize(
    "name,template,expected",
    [
        ("Authorization", "Bearer {token}", "Bearer fixture-value"),
        ("api-key", "{token}", "fixture-value"),
        ("X-Model-Auth", "Token {token}", "Token fixture-value"),
    ],
)
def test_explicit_auth_name_and_format_override_client_auth(name, template, expected):
    requests = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    config = HTTPConfig(
        "https://model.example",
        ("model.example",),
        auth_header_name=name,
        auth_header_format=template,
        timeout_seconds=1.25,
    )
    with httpx.Client(
        transport=httpx.MockTransport(handler),
        auth=httpx.BasicAuth("fixture-user", "fixture-password"),
        trust_env=False,
    ) as client:
        provider = _HTTPProvider(
            client, config, ModelProfile("local", "one"), credential="fixture-value"
        )
        provider._post("/v1/embeddings", {}, context())
        assert "fixture-value" not in repr(config)
        assert "fixture-value" not in repr(provider)
    assert requests[0].headers[name] == expected
    assert requests[0].headers["X-Request-ID"] == "portable-http-fixture"
    assert requests[0].extensions["timeout"]["read"] == 1.25
    if name.lower() != "authorization":
        assert "authorization" not in requests[0].headers


def test_no_explicit_auth_preserves_injected_client_headers():
    requests = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"X-Inherited-Auth": "fixture-value"},
        trust_env=False,
    ) as client:
        provider = _HTTPProvider(
            client, HTTPConfig("https://model.example", ("model.example",)), ModelProfile("m", "r")
        )
        provider._post("/v1/embeddings", {}, context())
    assert requests[0].headers["X-Inherited-Auth"] == "fixture-value"


def test_redirect_cannot_forward_explicit_credentials():
    requests = []

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://other.example/stolen"})

    config = HTTPConfig(
        "https://model.example", ("model.example",), auth_header_name="Authorization"
    )
    with httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True, trust_env=False
    ) as client:
        provider = _HTTPProvider(
            client, config, ModelProfile("local", "one"), credential="fixture-value"
        )
        with pytest.raises(ProviderError) as error:
            provider._post("/v1/embeddings", {}, context())
    assert error.value.code is ErrorCode.INVALID_RESPONSE
    assert len(requests) == 1
    assert "fixture-value" not in str(error.value)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://model.example",
        "https://other.example",
        "https://model.example.evil.test",
        "https://user:pass@model.example",
        "https://model.example?token=fixture",
        "https://model.example/#fragment",
        "https://model.example:0",
        "https://model.example:65536",
        "https://model.example/a/../b",
        "https://model.example/%2e%2e/other",
        "https://model.example\\@other.example",
        "https://model.example/\n",
    ],
)
def test_unsafe_base_urls_are_rejected(base_url):
    with pytest.raises(ValueError):
        HTTPConfig(base_url, ("model.example",))


@pytest.mark.parametrize(
    "path",
    [
        "https://other.example/v1",
        "//other.example/v1",
        "/../v1",
        "/%2e%2e/v1",
        "/v1?token=fixture",
        "/v1#fragment",
        "/v1\\other",
        "/v1/%0a",
    ],
)
def test_path_cannot_replace_origin_or_escape_prefix(path):
    with pytest.raises(ValueError):
        EmbeddingConfig(ModelProfile("model", "one", 2), path, 16, 2048)


@pytest.mark.parametrize(
    "overrides",
    [
        {"allowed_hosts": ["model.example"]},
        {"allowed_hosts": ("*.example",)},
        {"auth_header_name": "Host"},
        {"auth_header_name": "X-Request-ID"},
        {"auth_header_name": "Content-Length"},
        {"auth_header_name": "name\r\ninjected"},
        {"auth_header_format": "{token}\n"},
        {"auth_header_format": "Bearer {secret}"},
        {"auth_header_format": "{token.__class__}"},
        {"auth_header_format": "{token}{token}"},
        {"auth_header_format": "{token!r}"},
        {"validate_model_echo": 1},
        {"validate_version_echo": "yes"},
        {"timeout_seconds": float("nan")},
        {"max_response_bytes": True},
    ],
)
def test_invalid_transport_configuration_fails_closed(overrides):
    kwargs = {"base_url": "https://model.example", "allowed_hosts": ("model.example",)}
    with pytest.raises(ValueError):
        HTTPConfig(**(kwargs | overrides))


@pytest.mark.parametrize("credential", [None, "", "\nfixture", "fixture\r\nX-Injected: value"])
def test_missing_or_malformed_injected_credentials_fail_before_request(credential):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("sent")), trust_env=False
    ) as client:
        with pytest.raises(ValueError, match="injected credential"):
            _HTTPProvider(
                client,
                HTTPConfig(
                    "https://model.example", ("model.example",), auth_header_name="Authorization"
                ),
                ModelProfile("local", "one"),
                credential=credential,
            )


def test_credential_without_auth_configuration_is_rejected():
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("sent")), trust_env=False
    ) as client:
        with pytest.raises(ValueError, match="explicit authentication header"):
            _HTTPProvider(
                client,
                HTTPConfig("https://model.example", ("model.example",)),
                ModelProfile("local", "one"),
                credential="fixture-value",
            )


def test_explicit_model_echo_policy_does_not_change_configured_profile():
    config = HTTPConfig("https://model.example", ("model.example",))
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("sent")), trust_env=False
    ) as client:
        strict = _HTTPProvider(client, config, ModelProfile("expected", "one"))
        with pytest.raises(ProviderError):
            strict._validate_identity({"model": "reported"})
        aliases = _HTTPProvider(
            client,
            replace(config, response_model_aliases=("reported",)),
            ModelProfile("expected", "one"),
        )
        aliases._validate_identity({"model": "reported"})
        configured = _HTTPProvider(
            client, replace(config, validate_model_echo=False), ModelProfile("expected", "one")
        )
        configured._validate_identity({"model": "reported"})
        assert configured.describe() == aliases.describe() == strict.describe()


def test_frozen_nested_request_extensions_are_encoded_as_json():
    requests = []
    source = {"vendor": {"flags": [1, {"enabled": True}]}}
    frozen = freeze_body(source, set())
    source["vendor"]["flags"].append(2)

    # Callback is passed to the local mock transport below; it is not unused.
    # nosemgrep: useless-inner-function
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        provider = _HTTPProvider(
            client, HTTPConfig("https://model.example", ("model.example",)), ModelProfile("m", "r")
        )
        provider._post("/v1/chat/completions", frozen, context())
    assert json.loads(requests[0].content) == {"vendor": {"flags": [1, {"enabled": True}]}}

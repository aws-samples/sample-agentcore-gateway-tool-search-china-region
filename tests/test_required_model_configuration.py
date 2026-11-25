"""Missing model settings fail locally without selecting a model service."""

import pytest

from semantic_core._http import HTTPConfig
from semantic_core.openai_compatible import openai_chat_config, openai_embedding_config
from semantic_service.models import ModelSettings


@pytest.mark.parametrize("key", ["MODEL_EMBEDDING_MODEL", "MODEL_CHAT_MODEL", "MODEL_RERANK_MODEL"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_runtime_requires_explicit_model_names(key, value):
    env = {
        "MODEL_ALLOWED_HOSTS": "models.example.com",
        "MODEL_BASE_URL": "https://models.example.com/v1",
        "MODEL_EMBEDDING_MODEL": "example-embedding-model",
        "MODEL_CHAT_MODEL": "example-chat-model",
        "MODEL_RERANK_MODEL": "example-rerank-model",
        "MODEL_RERANK_ENABLED": "true",
    }
    if value is None:
        del env[key]
    else:
        env[key] = value
    with pytest.raises(ValueError, match=key):
        ModelSettings.from_env(env)


def test_embedding_factory_requires_model_name():
    with pytest.raises(TypeError, match="model_id"):
        openai_embedding_config("configured-v1")


def test_chat_factory_requires_model_name():
    with pytest.raises(TypeError, match="model_id"):
        openai_chat_config()


@pytest.mark.parametrize("endpoint", [None, "", " "])
def test_transport_requires_explicit_endpoint(endpoint):
    with pytest.raises(ValueError, match="endpoint"):
        HTTPConfig(endpoint, ("models.example.com",))


@pytest.mark.parametrize("endpoint", [None, "", "   "])
def test_runtime_rejects_missing_secret_endpoint_before_network(endpoint):
    import httpx

    from semantic_service.models import RotatingModelProviders
    from semantic_service.secrets import SecretError, SecretValue

    settings = ModelSettings.from_env(
        {
            "MODEL_ALLOWED_HOSTS": "models.example.com",
            "MODEL_EMBEDDING_MODEL": "example-embedding-model",
            "MODEL_CHAT_MODEL": "example-chat-model",
        }
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("no network"))
    ) as client:
        providers = RotatingModelProviders(None, settings, client)
        with pytest.raises(SecretError, match="configured secret unavailable or invalid"):
            providers._transport(SecretValue("v1", {"api_key": "fixture", "base_url": endpoint}), 1)


def test_runtime_accepts_explicit_endpoint_without_secret_endpoint():
    import httpx

    from semantic_service.models import RotatingModelProviders
    from semantic_service.secrets import SecretValue

    settings = ModelSettings.from_env(
        {
            "MODEL_ALLOWED_HOSTS": "models.example.com",
            "MODEL_BASE_URL": "https://models.example.com/v1",
            "MODEL_EMBEDDING_MODEL": "example-embedding-model",
            "MODEL_CHAT_MODEL": "example-chat-model",
        }
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("no network"))
    ) as client:
        providers = RotatingModelProviders(None, settings, client)
        transport, key = providers._transport(SecretValue("v1", {"api_key": "fixture"}), 1)
    assert transport.base_url == "https://models.example.com/v1"
    assert key == "fixture"

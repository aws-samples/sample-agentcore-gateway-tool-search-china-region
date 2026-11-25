"""Factories for explicitly configured HTTP model providers.

OriginHTTPConfig restricts endpoints to HTTPS origins on port 443. Factories
require model identities; callers supply clients, endpoints and credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from semantic_core._http import HTTPConfig
from semantic_core.openai_compatible import (
    OpenAIChatProvider,
    OpenAIEmbeddingProvider,
    openai_chat_config,
    openai_embedding_config,
)
from semantic_core.rerank import HTTPRerankProvider, RerankCodec, configured_rerank_config


@dataclass(frozen=True)
class OriginHTTPConfig(HTTPConfig):
    """Origin-only HTTPS transport with optional version echo checks."""

    validate_version_echo: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        parsed = urlsplit(self.base_url)
        if parsed.port not in (None, 443) or parsed.path not in ("", "/"):
            raise ValueError("origin HTTP config requires an HTTPS origin on port 443")
        if self.timeout_seconds > 30 or self.max_response_bytes > 10 * 1024 * 1024:
            raise ValueError("invalid origin adapter resource limit")


def create_embedding_provider(
    client: httpx.Client,
    config: OriginHTTPConfig,
    *,
    model_id: str,
    revision: str,
    config_version: str = "v1",
    credential: str | None = None,
) -> OpenAIEmbeddingProvider:
    return OpenAIEmbeddingProvider(
        client,
        config,
        openai_embedding_config(revision, config_version, model_id=model_id),
        credential=credential,
    )


def create_chat_provider(
    client: httpx.Client,
    config: OriginHTTPConfig,
    *,
    model_id: str,
    revision: str,
    config_version: str = "v1",
    credential: str | None = None,
) -> OpenAIChatProvider:
    return OpenAIChatProvider(
        client,
        config,
        openai_chat_config(config_version, model_id=model_id, revision=revision),
        credential=credential,
    )


def create_rerank_provider(
    client: httpx.Client,
    config: OriginHTTPConfig,
    *,
    model_id: str,
    revision: str,
    codec: RerankCodec,
    config_version: str = "v1",
    credential: str | None = None,
) -> HTTPRerankProvider:
    return HTTPRerankProvider(
        client,
        config,
        configured_rerank_config(model_id, revision, config_version, codec=codec),
        credential=credential,
    )

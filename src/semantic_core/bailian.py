"""Compatibility factories for Bailian presets of the generic HTTP providers.

The old constructor spellings remain callable, but no supplier-specific provider
class or parser is retained. New integrations use openai_compatible/rerank
configuration directly. No factory creates a client or discovers credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from semantic_core._http import HTTPConfig
from semantic_core.openai_compatible import (
    OpenAIChatProvider,
    OpenAIEmbeddingProvider,
    bailian_chat_config,
    bailian_embedding_config,
)
from semantic_core.rerank import HTTPRerankProvider, bailian_rerank_config


@dataclass(frozen=True)
class BailianConfig(HTTPConfig):
    """Legacy config retains origin-only URLs and optional version echo checks."""

    validate_version_echo: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        parsed = urlsplit(self.base_url)
        if parsed.port not in (None, 443) or parsed.path not in ("", "/"):
            raise ValueError("legacy Bailian config requires an HTTPS origin on port 443")
        if self.timeout_seconds > 30 or self.max_response_bytes > 10 * 1024 * 1024:
            raise ValueError("invalid legacy adapter resource limit")


def BailianEmbeddingProvider(
    client: httpx.Client,
    config: BailianConfig,
    *,
    revision: str,
    config_version: str = "v1",
    credential: str | None = None,
) -> OpenAIEmbeddingProvider:
    return OpenAIEmbeddingProvider(
        client,
        config,
        bailian_embedding_config(revision, config_version),
        credential=credential,
    )


def BailianLLMProvider(
    client: httpx.Client,
    config: BailianConfig,
    *,
    config_version: str = "v1",
    credential: str | None = None,
) -> OpenAIChatProvider:
    return OpenAIChatProvider(
        client, config, bailian_chat_config(config_version), credential=credential
    )


def BailianRerankProvider(
    client: httpx.Client,
    config: BailianConfig,
    *,
    model_id: str,
    revision: str,
    config_version: str = "v1",
    credential: str | None = None,
) -> HTTPRerankProvider:
    return HTTPRerankProvider(
        client,
        config,
        bailian_rerank_config(model_id, revision, config_version),
        credential=credential,
    )

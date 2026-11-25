"""Runtime model configuration and Secrets Manager credential rotation."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar, cast

import httpx

from semantic_core._http import HTTPConfig
from semantic_core._timing import timed
from semantic_core.openai_compatible import (
    ChatConfig,
    EmbeddingConfig,
    OpenAIChatProvider,
    OpenAIEmbeddingProvider,
)
from semantic_core.providers import (
    EmbeddingProvider,
    EmbeddingResult,
    ErrorCode,
    GeneratedQueries,
    LLMProvider,
    ModelProfile,
    ProviderError,
    ProviderFailure,
    Purpose,
    RequestContext,
    RerankDocument,
    RerankProvider,
    RerankResult,
)
from semantic_core.rerank import HTTPRerankProvider, RerankCodec, RerankConfig
from semantic_service.config import (
    parse_embedding_timeout_seconds,
    validate_embedding_timeout_seconds,
)
from semantic_service.secrets import SecretError, SecretsCache, SecretValue

T = TypeVar("T")


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be explicitly configured")
    return value


def _integer(env: Mapping[str, str], key: str, default: int | None = None) -> int:
    raw = env.get(key)
    if raw is None and default is None:
        raise ValueError(f"{key} must be explicitly configured")
    try:
        value = int(raw) if raw is not None else cast(int, default)
    except ValueError:
        raise ValueError(f"{key} must be a positive integer") from None
    if value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _number(env: Mapping[str, str], key: str, default: float) -> float:
    try:
        value = float(env.get(key, str(default)))
    except ValueError:
        raise ValueError(f"{key} must be positive and finite") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key} must be positive and finite")
    return value


def _boolean(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = env.get(key, str(default)).lower()
    if raw not in ("true", "false"):
        raise ValueError(f"{key} must be true or false")
    return raw == "true"


def _body(env: Mapping[str, str], key: str) -> Mapping[str, object]:
    try:
        value: object = json.loads(env.get(key, "{}"))
    except ValueError:
        raise ValueError(f"{key} must be a JSON object") from None
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a JSON object")
    return cast(dict[str, object], value)


@dataclass(frozen=True)
class ModelSettings:
    allowed_hosts: tuple[str, ...]
    embedding: EmbeddingConfig
    chat: ChatConfig
    rerank: RerankConfig | None = None
    secret_id: str = "semsearch/model-api"
    base_url: str | None = None
    embedding_timeout_seconds: float = 1.0
    document_timeout_seconds: float = 30
    chat_timeout_seconds: float = 30
    rerank_timeout_seconds: float = 0.45
    auth_header_name: str = "Authorization"
    auth_header_format: str = "Bearer {token}"
    response_model_aliases: tuple[str, ...] = ()
    validate_model_echo: bool = True

    def __post_init__(self) -> None:
        if not self.secret_id or not self.allowed_hosts:
            raise ValueError("model secret ID and exact allowed hosts are required")
        object.__setattr__(
            self,
            "embedding_timeout_seconds",
            validate_embedding_timeout_seconds(self.embedding_timeout_seconds),
        )
        if isinstance(self.base_url, str) and not self.base_url.strip():
            object.__setattr__(self, "base_url", None)
        # Validate all transport options before credentials or network access.
        base = self.base_url if self.base_url is not None else "https://" + self.allowed_hosts[0]
        for timeout in (
            self.embedding_timeout_seconds,
            self.document_timeout_seconds,
            self.chat_timeout_seconds,
            self.rerank_timeout_seconds,
        ):
            HTTPConfig(
                base,
                self.allowed_hosts,
                timeout_seconds=timeout,
                auth_header_name=self.auth_header_name,
                auth_header_format=self.auth_header_format,
                response_model_aliases=self.response_model_aliases,
                validate_model_echo=self.validate_model_echo,
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ModelSettings:
        """Read controlled model profiles, never any API-key environment variable."""
        source = os.environ if env is None else env
        hosts = tuple(
            host.strip()
            for host in source.get("MODEL_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        )
        if not hosts:
            raise ValueError("MODEL_ALLOWED_HOSTS must list exact HTTPS hosts")
        version = source.get("MODEL_CONFIG_VERSION", "v1")
        embedding = EmbeddingConfig(
            ModelProfile(
                _required(source, "MODEL_EMBEDDING_MODEL"),
                source.get("MODEL_EMBEDDING_REVISION", "configured-v1"),
                _integer(source, "MODEL_EMBEDDING_DIMENSIONS", 1024),
                config_version=version,
            ),
            source.get("MODEL_EMBEDDING_PATH", "/embeddings"),
            _integer(source, "MODEL_EMBEDDING_MAX_BATCH", 10),
            _integer(source, "MODEL_EMBEDDING_MAX_TEXT_BYTES", 8192),
            send_dimensions=_boolean(source, "MODEL_EMBEDDING_SEND_DIMENSIONS"),
            encoding_format="float"
            if _boolean(source, "MODEL_EMBEDDING_SEND_FLOAT_FORMAT")
            else None,
            extra_body=_body(source, "MODEL_EMBEDDING_EXTRA_BODY"),
        )
        chat = ChatConfig(
            ModelProfile(
                _required(source, "MODEL_CHAT_MODEL"),
                source.get("MODEL_CHAT_REVISION", "configured-v1"),
                config_version=version,
            ),
            source.get("MODEL_CHAT_PATH", "/chat/completions"),
            _integer(source, "MODEL_CHAT_MAX_TEXT_BYTES", 8192),
            max_tokens=_integer(source, "MODEL_CHAT_MAX_TOKENS", 2048),
            json_mode=True,
            extra_body=_body(source, "MODEL_CHAT_EXTRA_BODY"),
        )
        rerank = None
        if _boolean(source, "MODEL_RERANK_ENABLED"):
            # Paths are relative to the configured base, including any URL prefix.
            path = source.get("MODEL_RERANK_PATH", "/rerank")
            envelope = source.get("MODEL_RERANK_ENVELOPE", "flat")
            score_field = source.get("MODEL_RERANK_SCORE_FIELD", "relevance_score")
            if (
                not path
                or envelope not in ("flat", "nested-output")
                or score_field not in ("score", "relevance_score")
            ):
                raise ValueError("enabled rerank requires a valid path, envelope and score field")
            rerank = RerankConfig(
                ModelProfile(
                    _required(source, "MODEL_RERANK_MODEL"),
                    source.get("MODEL_RERANK_REVISION", "configured-v1"),
                    config_version=version,
                ),
                RerankCodec(
                    path,
                    cast(Literal["flat", "nested-output"], envelope),
                    cast(Literal["score", "relevance_score"], score_field),
                ),
                max_documents=_integer(source, "MODEL_RERANK_MAX_DOCUMENTS", 30),
                max_query_bytes=_integer(source, "MODEL_RERANK_MAX_QUERY_BYTES", 256),
                max_document_bytes=_integer(source, "MODEL_RERANK_MAX_DOCUMENT_BYTES", 512),
                max_total_bytes=_integer(source, "MODEL_RERANK_MAX_TOTAL_BYTES", 30000),
            )
        return cls(
            hosts,
            embedding,
            chat,
            rerank,
            secret_id=source.get("MODEL_SECRET_ID", "semsearch/model-api"),
            base_url=source.get("MODEL_BASE_URL"),
            embedding_timeout_seconds=parse_embedding_timeout_seconds(source),
            document_timeout_seconds=_number(source, "MODEL_DOCUMENT_TIMEOUT_SECONDS", 30),
            chat_timeout_seconds=_number(source, "MODEL_CHAT_TIMEOUT_SECONDS", 30),
            rerank_timeout_seconds=_number(source, "MODEL_RERANK_TIMEOUT_SECONDS", 0.45),
            auth_header_name=source.get("MODEL_AUTH_HEADER_NAME", "Authorization"),
            auth_header_format=source.get("MODEL_AUTH_HEADER_FORMAT", "Bearer {token}"),
            response_model_aliases=tuple(
                alias.strip()
                for alias in source.get("MODEL_RESPONSE_ALIASES", "").split(",")
                if alias.strip()
            ),
            validate_model_echo=_boolean(source, "MODEL_VALIDATE_MODEL_ECHO", True),
        )


class RotatingModelProviders:
    """Reuse HTTP connections and refresh a failed credential once per request.

    A 30-second refresh cooldown also bounds Secrets calls during persistent
    provider authentication failures. Retry retains the original request deadline.
    """

    def __init__(self, cache: SecretsCache, settings: ModelSettings, client: httpx.Client) -> None:
        self._cache = cache
        self._settings = settings
        self._client = client
        self._endpoint: str | None = None
        self._refresh_after = 0.0
        self._lock = threading.RLock()
        self.embedding: EmbeddingProvider = _RotatingEmbedding(self)
        self.llm: LLMProvider = _RotatingLLM(self)
        self.rerank: RerankProvider | None = (
            _RotatingRerank(self) if settings.rerank is not None else None
        )

    @timed("model.setup")
    def _transport(self, secret: SecretValue, timeout: float) -> tuple[HTTPConfig, str]:
        key = secret.value.get("api_key")
        if not isinstance(key, str):
            raise SecretError()
        base = (
            self._settings.base_url
            if self._settings.base_url is not None
            else secret.value.get("base_url")
        )
        if not isinstance(base, str) or not base.strip():
            raise SecretError()
        transport = HTTPConfig(
            base,
            self._settings.allowed_hosts,
            timeout_seconds=timeout,
            auth_header_name=self._settings.auth_header_name,
            auth_header_format=self._settings.auth_header_format,
            response_model_aliases=self._settings.response_model_aliases,
            validate_model_echo=self._settings.validate_model_echo,
        )
        with self._lock:
            if self._endpoint is not None and base != self._endpoint:
                raise ValueError("endpoint changes require a new model configuration and index")
            self._endpoint = base
        return transport, key

    def _refresh(self, previous: SecretValue) -> SecretValue | None:
        with self._lock:
            current = self._cache.get(self._settings.secret_id)
            if current.version_id != previous.version_id:
                return current
            now = time.monotonic()
            if now < self._refresh_after:
                return None
            self._refresh_after = now + 30
            return self._cache.get(self._settings.secret_id, force_refresh=True)

    def _invoke(
        self,
        context: RequestContext,
        timeout: float,
        operation: Callable[[HTTPConfig, str], T],
    ) -> T:
        context.remaining()
        authentication_failure: ProviderFailure | None = None
        try:
            secret = self._cache.get(self._settings.secret_id)
            for attempt in range(2):
                context.remaining()
                transport, key = self._transport(secret, timeout)
                try:
                    return operation(transport, key)
                except ProviderError as error:
                    if error.code is not ErrorCode.AUTHENTICATION or attempt:
                        raise
                    # Retain diagnostics within this rotation only, never across requests.
                    authentication_failure = error.failure
                    context.remaining()
                    refreshed = self._refresh(secret)
                    if refreshed is None:
                        raise
                    secret = refreshed
            raise AssertionError("bounded authentication retry exhausted")
        except SecretError:
            raise ProviderError(ErrorCode.UNAVAILABLE, failure=authentication_failure) from None
        except ValueError:
            raise ProviderError(ErrorCode.INVALID_REQUEST, failure=authentication_failure) from None


class _RotatingEmbedding:
    def __init__(self, owner: RotatingModelProviders) -> None:
        self._owner = owner

    def describe(self) -> ModelProfile:
        return self._owner._settings.embedding.profile

    def embed(
        self, texts: Sequence[str], purpose: Purpose, context: RequestContext
    ) -> EmbeddingResult:
        owner = self._owner
        timeout = (
            owner._settings.embedding_timeout_seconds
            if purpose == "query"
            else owner._settings.document_timeout_seconds
        )
        return owner._invoke(
            context,
            timeout,
            lambda transport, key: OpenAIEmbeddingProvider(
                owner._client, transport, owner._settings.embedding, credential=key
            ).embed(texts, purpose, context),
        )


class _RotatingLLM:
    def __init__(self, owner: RotatingModelProviders) -> None:
        self._owner = owner

    def describe(self) -> ModelProfile:
        return self._owner._settings.chat.profile

    def generate_queries(
        self, tool_description: str, languages: Sequence[str], count: int, context: RequestContext
    ) -> GeneratedQueries:
        owner = self._owner
        return owner._invoke(
            context,
            owner._settings.chat_timeout_seconds,
            lambda transport, key: OpenAIChatProvider(
                owner._client, transport, owner._settings.chat, credential=key
            ).generate_queries(tool_description, languages, count, context),
        )


class _RotatingRerank:
    def __init__(self, owner: RotatingModelProviders) -> None:
        self._owner = owner

    def describe(self) -> ModelProfile:
        config = self._owner._settings.rerank
        assert config is not None
        return config.profile

    def rerank(
        self, query: str, documents: Sequence[RerankDocument], top_n: int, context: RequestContext
    ) -> RerankResult:
        owner = self._owner
        config = owner._settings.rerank
        assert config is not None
        return owner._invoke(
            context,
            owner._settings.rerank_timeout_seconds,
            lambda transport, key: HTTPRerankProvider(
                owner._client, transport, config, credential=key
            ).rerank(query, documents, top_n, context),
        )

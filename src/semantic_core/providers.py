"""Versioned, synchronous model contracts and deterministic offline providers.

All credentials and transports belong to the caller. A revision is an explicit
configuration identity; it does not claim a supplier alias is immutable.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from semantic_core import _normalization

FloatMatrix = NDArray[np.float32]
Purpose = Literal["query", "document"]


class ErrorCode(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"


FailureKind = Literal[
    "http_rate_limited",
    "http_client_error",
    "http_server_error",
    "http_status_error",
    "connect_timeout",
    "read_timeout",
    "write_timeout",
    "pool_timeout",
    "timeout",
    "deadline_exceeded",
    "network_error",
    "response_parse",
    "authentication",
    "invalid_request",
    "unavailable",
    "rate_limited",
    "unknown",
]
_FAILURE_KINDS = frozenset(
    (
        "http_rate_limited",
        "http_client_error",
        "http_server_error",
        "http_status_error",
        "connect_timeout",
        "read_timeout",
        "write_timeout",
        "pool_timeout",
        "timeout",
        "deadline_exceeded",
        "network_error",
        "response_parse",
        "authentication",
        "invalid_request",
        "unavailable",
        "rate_limited",
        "unknown",
    )
)
_RATE_LIMIT_HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
)
_MAX_DIAGNOSTIC_COUNTER = 2_147_483_647


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    """只保留受控类别和有界数值；不接受异常文本、URL 或响应正文。"""

    kind: FailureKind
    http_status: int | None = None
    retry_after_seconds: int | None = None
    rate_limit_headers: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in _FAILURE_KINDS:
            raise ValueError("invalid provider failure kind")
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise ValueError("invalid diagnostic HTTP status")
        if self.retry_after_seconds is not None and (
            type(self.retry_after_seconds) is not int
            or not 0 <= self.retry_after_seconds <= _MAX_DIAGNOSTIC_COUNTER
        ):
            raise ValueError("invalid diagnostic retry delay")
        if (
            type(self.rate_limit_headers) is not tuple
            or len(self.rate_limit_headers) > len(_RATE_LIMIT_HEADERS)
            or any(
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or item[0] not in _RATE_LIMIT_HEADERS
                or type(item[1]) is not int
                or not 0 <= item[1] <= _MAX_DIAGNOSTIC_COUNTER
                for item in self.rate_limit_headers
            )
            or len(dict(self.rate_limit_headers)) != len(self.rate_limit_headers)
        ):
            raise ValueError("invalid diagnostic rate limits")

    def as_log_fields(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "http_status": self.http_status,
            "retry_after_seconds": self.retry_after_seconds,
            "rate_limit_headers": dict(self.rate_limit_headers),
        }


_DEFAULT_FAILURE_KINDS: dict[ErrorCode, FailureKind] = {
    ErrorCode.TIMEOUT: "timeout",
    ErrorCode.RATE_LIMITED: "rate_limited",
    ErrorCode.AUTHENTICATION: "authentication",
    ErrorCode.INVALID_REQUEST: "invalid_request",
    ErrorCode.UNAVAILABLE: "unavailable",
    ErrorCode.INVALID_RESPONSE: "response_parse",
}


class ProviderError(Exception):
    """Safe error plus validated usage from responses received before failure."""

    def __init__(
        self,
        code: ErrorCode,
        *,
        usage: Mapping[str, int] | None = None,
        failure: ProviderFailure | None = None,
    ) -> None:
        if failure is not None and not isinstance(failure, ProviderFailure):
            raise TypeError("failure must be ProviderFailure or None")
        self.code = code
        self.failure = (
            failure if failure is not None else ProviderFailure(_DEFAULT_FAILURE_KINDS[code])
        )
        # Unknown fields can contain provider metadata. Retain only the same
        # numeric counters exposed by successful results, never response bodies.
        self.usage: Mapping[str, int] = MappingProxyType(
            {
                key: value
                for key, value in (usage or {}).items()
                if key
                in {
                    "total_tokens",
                    "input_tokens",
                    "output_tokens",
                    "prompt_tokens",
                    "completion_tokens",
                }
                and type(value) is int
                and value >= 0
            }
        )
        super().__init__(f"provider {code.value}")


@dataclass(frozen=True)
class ModelProfile:
    model_id: str
    revision: str
    dimension: int | None = None
    normalization: str = "l2"
    config_version: str = "v1"

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.model_id, self.revision, self.config_version)
        ):
            raise ValueError("model profile requires explicit version identifiers")
        if self.dimension is not None and (
            isinstance(self.dimension, bool)
            or not isinstance(self.dimension, int)
            or self.dimension < 1
        ):
            raise ValueError("invalid model dimension")
        if self.normalization != "l2":
            raise ValueError("unsupported normalization")


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    deadline: float

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request_id, str)
            or not self.request_id.strip()
            or len(self.request_id) > 128
            or any(ord(character) < 32 or ord(character) > 126 for character in self.request_id)
        ):
            raise ValueError("invalid request ID")
        if isinstance(self.deadline, bool) or not math.isfinite(self.deadline):
            raise ValueError("invalid request deadline")

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError(ErrorCode.TIMEOUT, failure=ProviderFailure("deadline_exceeded"))
        return remaining


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: FloatMatrix
    profile: ModelProfile
    usage: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RerankDocument:
    id: str
    text: str


@dataclass(frozen=True)
class RerankItem:
    id: str
    score: float


@dataclass(frozen=True)
class RerankResult:
    items: tuple[RerankItem, ...]
    profile: ModelProfile
    usage: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class GeneratedQueries:
    queries: tuple[str, ...]
    profile: ModelProfile
    prompt_version: str
    usage: Mapping[str, int] = field(default_factory=dict)


@runtime_checkable
class EmbeddingProvider(Protocol):
    def describe(self) -> ModelProfile: ...

    def embed(
        self, texts: Sequence[str], purpose: Purpose, context: RequestContext
    ) -> EmbeddingResult: ...


@runtime_checkable
class RerankProvider(Protocol):
    def describe(self) -> ModelProfile: ...

    def rerank(
        self,
        query: str,
        documents: Sequence[RerankDocument],
        top_n: int,
        context: RequestContext,
    ) -> RerankResult: ...


@runtime_checkable
class LLMProvider(Protocol):
    def describe(self) -> ModelProfile: ...

    def generate_queries(
        self,
        tool_description: str,
        languages: Sequence[str],
        count: int,
        context: RequestContext,
    ) -> GeneratedQueries: ...


def normalize_vectors(vectors: object, *, dimension: int, rows: int | None = None) -> FloatMatrix:
    """Provider wrapper: nonempty float32-representable rows, read-only output.

    Invalid input always becomes INVALID_RESPONSE. Index construction keeps its
    distinct ValueError/empty-catalog/finite-float64 wrapper around the same core.
    """
    if (
        isinstance(dimension, bool)
        or not isinstance(dimension, int)
        or dimension < 1
        or (rows is not None and (isinstance(rows, bool) or not isinstance(rows, int) or rows < 1))
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE)
    try:
        normalized = _normalization.normalize_rows(
            vectors,
            dimension,
            allow_empty=False,
            float32_input=True,
            expected_rows=rows,
        )
    except (ValueError, TypeError, OverflowError):
        raise ProviderError(ErrorCode.INVALID_RESPONSE) from None
    normalized.setflags(write=False)
    return normalized


def validate_embedding_result(
    result: EmbeddingResult, expected_profile: ModelProfile, rows: int
) -> FloatMatrix:
    if result.profile != expected_profile or expected_profile.dimension is None:
        raise ProviderError(ErrorCode.INVALID_RESPONSE)
    return normalize_vectors(result.vectors, dimension=expected_profile.dimension, rows=rows)


def validate_texts(texts: Sequence[str], purpose: Purpose) -> None:
    if (
        purpose not in ("query", "document")
        or isinstance(texts, str | bytes)
        or not texts
        or any(not isinstance(text, str) or not text.strip() for text in texts)
    ):
        raise ProviderError(ErrorCode.INVALID_REQUEST)


def validate_rerank_input(query: str, documents: Sequence[RerankDocument], top_n: int) -> None:
    if (
        not isinstance(query, str)
        or not query.strip()
        or not documents
        or isinstance(top_n, bool)
        or not isinstance(top_n, int)
        or not 1 <= top_n <= len(documents)
        or any(
            not isinstance(document, RerankDocument)
            or not isinstance(document.id, str)
            or not document.id
            or not isinstance(document.text, str)
            or not document.text.strip()
            for document in documents
        )
        or len({document.id for document in documents}) != len(documents)
    ):
        raise ProviderError(ErrorCode.INVALID_REQUEST)


def validate_generation_input(tool_description: str, languages: Sequence[str], count: int) -> None:
    if (
        not isinstance(tool_description, str)
        or not tool_description.strip()
        or isinstance(languages, str | bytes)
        or not languages
        or any(language not in ("zh", "en") for language in languages)
        or len(set(languages)) != len(languages)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or not 1 <= count <= 6
    ):
        raise ProviderError(ErrorCode.INVALID_REQUEST)


class MockEmbeddingProvider:
    """Content-hash vectors are stable across processes and Python hash seeds."""

    def __init__(self, dimension: int = 16) -> None:
        self._profile = ModelProfile("mock-embedding", "fixture-v1", dimension)

    def describe(self) -> ModelProfile:
        return self._profile

    def embed(
        self, texts: Sequence[str], purpose: Purpose, context: RequestContext
    ) -> EmbeddingResult:
        context.remaining()
        validate_texts(texts, purpose)
        dimension = self._profile.dimension
        assert dimension is not None
        raw = [
            [
                int.from_bytes(hashlib.sha256(f"{text}\0{column}".encode()).digest()[:4], "big")
                / 2**31
                - 1
                for column in range(dimension)
            ]
            for text in texts
        ]
        vectors = normalize_vectors(raw, dimension=dimension)
        return EmbeddingResult(vectors, self._profile, {"input_items": len(texts)})


class MockRerankProvider:
    def __init__(self) -> None:
        self._profile = ModelProfile("mock-rerank", "fixture-v1")

    def describe(self) -> ModelProfile:
        return self._profile

    def rerank(
        self,
        query: str,
        documents: Sequence[RerankDocument],
        top_n: int,
        context: RequestContext,
    ) -> RerankResult:
        context.remaining()
        validate_rerank_input(query, documents, top_n)
        terms = set(re.findall(r"\w", query.casefold()))
        scored = [
            RerankItem(
                document.id,
                len(terms & set(re.findall(r"\w", document.text.casefold()))) / max(len(terms), 1),
            )
            for document in documents
        ]
        items = tuple(sorted(scored, key=lambda item: -item.score)[:top_n])
        return RerankResult(items, self._profile, {"input_items": len(documents)})


class MockLLMProvider:
    def __init__(self) -> None:
        self._profile = ModelProfile("mock-llm", "fixture-v1")

    def describe(self) -> ModelProfile:
        return self._profile

    def generate_queries(
        self,
        tool_description: str,
        languages: Sequence[str],
        count: int,
        context: RequestContext,
    ) -> GeneratedQueries:
        context.remaining()
        validate_generation_input(tool_description, languages, count)
        summary = " ".join(tool_description.split())[:180]
        queries = tuple(
            (
                f"如何使用此工具完成任务 {index + 1}：{summary}"
                if languages[index % len(languages)] == "zh"
                else f"How can this tool help with task {index + 1}: {summary}"
            )
            for index in range(count)
        )
        return GeneratedQueries(queries, self._profile, "mock-queries-v1")

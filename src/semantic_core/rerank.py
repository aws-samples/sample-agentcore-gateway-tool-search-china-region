"""Explicit HTTP rerank codecs; reranking is not an OpenAI standard endpoint.

The caller selects a path, wire envelope, model identity, and byte budgets.
Transport and credentials are injected; this module never discovers either.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import httpx

from semantic_core._http import (
    HTTPConfig,
    _HTTPProvider,
    _index,
    _list,
    _object,
    _response_failure,
    _score,
    _usage,
    _utf8_size,
    positive_int,
    validate_path,
    validate_preset_origin,
)
from semantic_core.providers import (
    ErrorCode,
    ModelProfile,
    ProviderError,
    RequestContext,
    RerankDocument,
    RerankItem,
    RerankResult,
    validate_rerank_input,
)


@dataclass(frozen=True, slots=True)
class RerankCodec:
    """An immutable choice of request envelope, response envelope, and score key.

    ``flat`` uses query/documents/top_n and top-level results; ``nested-output``
    uses input/parameters and output.results. Both return zero-based indices.
    The default path is a common convention, not a standards compatibility claim.
    """

    path: str = "/v1/rerank"
    envelope: Literal["flat", "nested-output"] = "flat"
    score_field: Literal["relevance_score", "score"] = "relevance_score"

    def __post_init__(self) -> None:
        validate_path(self.path)
        if self.envelope not in ("flat", "nested-output"):
            raise ValueError("unsupported rerank envelope")
        if self.score_field not in ("relevance_score", "score"):
            raise ValueError("unsupported rerank score field")

    def encode(
        self, model_id: str, query: str, texts: Sequence[str], top_n: int
    ) -> dict[str, object]:
        if self.envelope == "nested-output":
            return {
                "model": model_id,
                "input": {"query": query, "documents": list(texts)},
                "parameters": {"top_n": top_n, "return_documents": False},
            }
        return {
            "model": model_id,
            "query": query,
            "documents": list(texts),
            "top_n": top_n,
            "return_documents": False,
        }

    def results(self, response: Mapping[str, object]) -> list[object]:
        output = _object(response.get("output")) if self.envelope == "nested-output" else response
        return _list(output.get("results"))


@dataclass(frozen=True, slots=True)
class RerankConfig:
    """Configured model and conservative UTF-8 byte budgets, not token counts."""

    profile: ModelProfile
    codec: RerankCodec
    max_documents: int = 30
    max_query_bytes: int = 256
    max_document_bytes: int = 512
    max_total_bytes: int = 30_000
    origin_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.profile, ModelProfile) or not isinstance(self.codec, RerankCodec):
            raise ValueError("rerank configuration requires a model profile and codec")
        for name in (
            "max_documents",
            "max_query_bytes",
            "max_document_bytes",
            "max_total_bytes",
        ):
            positive_int(getattr(self, name), name)


class HTTPRerankProvider(_HTTPProvider):
    """Order every supplied candidate while preserving local candidate identity."""

    def __init__(
        self,
        client: httpx.Client,
        transport: HTTPConfig,
        config: RerankConfig,
        *,
        credential: str | None = None,
    ) -> None:
        if not isinstance(config, RerankConfig):
            raise ValueError("invalid rerank configuration")
        validate_preset_origin(transport, config.origin_only)
        super().__init__(client, transport, config.profile, credential=credential)
        self._rerank_config = config

    def rerank(
        self,
        query: str,
        documents: Sequence[RerankDocument],
        top_n: int,
        context: RequestContext,
    ) -> RerankResult:
        """Require top_n == candidate count; malformed or partial output fails."""
        context.remaining()
        validate_rerank_input(query, documents, top_n)
        config = self._rerank_config
        query_bytes = _utf8_size(query)
        document_bytes = [_utf8_size(document.text) for document in documents]
        estimated_total = query_bytes * len(documents) + sum(document_bytes)
        if (
            len(documents) > config.max_documents
            or query_bytes > config.max_query_bytes
            or any(size > config.max_document_bytes for size in document_bytes)
            or estimated_total > config.max_total_bytes
            or top_n != len(documents)
        ):
            raise ProviderError(ErrorCode.INVALID_REQUEST)
        response = self._post(
            config.codec.path,
            config.codec.encode(
                self._profile.model_id, query, [document.text for document in documents], top_n
            ),
            context,
        )
        usage: Mapping[str, int] = {}
        try:
            usage = _usage(response)
            self._validate_identity(response)
            results = config.codec.results(response)
            if len(results) != top_n:
                raise ProviderError(ErrorCode.INVALID_RESPONSE)
            seen: set[int] = set()
            scored: list[tuple[int, float]] = []
            previous = math.inf
            for result in results:
                item = _object(result)
                index = _index(item.get("index"), len(documents))
                score = _score(item.get(config.codec.score_field))
                if index in seen or score > previous:
                    raise ProviderError(ErrorCode.INVALID_RESPONSE)
                seen.add(index)
                previous = score
                scored.append((index, score))
        except ProviderError as error:
            # A rejected identity, envelope, or ranking still consumed provider tokens.
            raise ProviderError(
                error.code,
                usage=usage or error.usage,
                failure=_response_failure(error, response),
            ) from None
        # Equal supplier scores retain the incoming candidate (RRF) order.
        scored.sort(key=lambda item: (-item[1], item[0]))
        items = tuple(RerankItem(documents[index].id, score) for index, score in scored)
        return RerankResult(items, self._profile, usage)


def configured_rerank_config(
    model_id: str,
    revision: str,
    config_version: str = "v1",
    *,
    codec: RerankCodec,
    max_total_bytes: int = 30_000,
) -> RerankConfig:
    """Bind an explicit model to a codec without inferring routes from its name."""
    return RerankConfig(
        profile=ModelProfile(model_id, revision, config_version=config_version),
        codec=codec,
        max_documents=30,
        max_query_bytes=256,
        max_document_bytes=512,
        max_total_bytes=max_total_bytes,
        origin_only=True,
    )

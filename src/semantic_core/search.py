"""Authorized hybrid retrieval; public protocol handlers live outside this package."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from semantic_core._embedding_telemetry import (
    EmbeddingTiming,
    collect_embedding,
    snapshot_embedding,
)
from semantic_core._timing import stage as timing_stage
from semantic_core._timing import timed
from semantic_core.catalog import ToolRecord
from semantic_core.providers import (
    EmbeddingProvider,
    ProviderError,
    ProviderFailure,
    RequestContext,
    RerankDocument,
    RerankProvider,
    validate_embedding_result,
)
from semantic_core.snapshot import IndexSnapshot


class AuthorizationError(RuntimeError):
    """Authorization is unavailable or invalid; fail closed."""


FusionStrategy = Literal["rrf", "vector", "vector_bm25_fallback"]
SearchMode = Literal["bm25", "rrf", "vector", "vector_bm25_fallback", "rerank"]
VectorAvailability = Literal["available", "disabled", "unavailable", "failed", "not_attempted"]
RerankState = Literal["applied", "disabled", "skipped", "failed"]


@dataclass(frozen=True, slots=True)
class SearchDiagnostics:
    """Bounded request-local outcomes without query, candidate, score or raw error data.

    ``ranking_mode`` is absent until a ranking completes. Rerank state describes
    the stage outcome; final authorization can replace a successfully reranked
    list with a new base ranking. Refusal, empty output and degradation are
    independent outcomes.
    """

    configured_fusion: FusionStrategy
    ranking_mode: SearchMode | None = None
    empty_result: bool = False
    below_threshold: bool = False
    degraded: bool = False
    vector_availability: VectorAvailability = "not_attempted"
    rerank_state: RerankState = "disabled"
    embedding_error: ProviderFailure | None = None
    embedding_timing: EmbeddingTiming | None = None
    lambda_deadline_ignored: bool = False


@dataclass(slots=True)
class _SearchTrace:
    value: SearchDiagnostics


@dataclass(frozen=True, slots=True)
class SearchConfig:
    """Opt-in retrieval experiments; serving defaults retain their original contract.

    Confidence gates use authorized parent cosine scores before candidate truncation.
    ``min_vector_margin`` measures top1 minus the request's median score; a single
    finite score has zero margin. Both enabled gates must pass, including after
    revocation. Neither gate interprets rerank scores, which are request-local.

    The fallback strategy keeps vector order when confident, fills vectorless
    candidates with positive lexical matches, and uses positive BM25 matches when
    vector top1 is below ``bm25_fallback_similarity``. A rejection gate takes
    precedence over fallback. Provider degradation still uses authorized BM25.
    """

    top_k: int = 5
    recall_k: int = 50
    rerank_limit: int = 30
    rerank_enabled: bool = False
    rrf_constant: float = 60.0
    lexical_weight: float = 1.0
    vector_weight: float = 1.0
    embedding_timeout: float = 1.0
    rerank_timeout: float = 0.45
    fusion_strategy: FusionStrategy = "rrf"
    bm25_fallback_similarity: float = 0.5
    min_vector_similarity: float | None = None
    min_vector_margin: float | None = None

    def __post_init__(self) -> None:
        for limit, upper in ((self.top_k, 20), (self.recall_k, 50), (self.rerank_limit, 30)):
            if type(limit) is not int or not 1 <= limit <= upper:
                raise ValueError("invalid retrieval limit")
        if type(self.rerank_enabled) is not bool:
            raise ValueError("rerank switch must be boolean")
        if self.fusion_strategy not in ("rrf", "vector", "vector_bm25_fallback"):
            raise ValueError("invalid fusion strategy")
        if self.bm25_fallback_similarity is None:
            raise ValueError("fallback confidence threshold is required")
        for value, threshold_lower, threshold_upper in (
            (self.bm25_fallback_similarity, -1.0, 1.0),
            (self.min_vector_similarity, -1.0, 1.0),
            (self.min_vector_margin, 0.0, 2.0),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not threshold_lower <= value <= threshold_upper
            ):
                raise ValueError("invalid vector confidence threshold")
        for value in (
            self.rrf_constant,
            self.lexical_weight,
            self.vector_weight,
            self.embedding_timeout,
            self.rerank_timeout,
        ):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("retrieval weights and budgets must be finite and positive")


@dataclass(frozen=True, slots=True)
class SearchHit:
    tool: ToolRecord


@dataclass(frozen=True, slots=True, init=False)
class SearchResult:
    hits: tuple[SearchHit, ...]
    mode: SearchMode
    # Kept for evaluation consumers; an explicit value must agree with mode.
    ranking_mode: SearchMode
    abstained: bool = False

    def __init__(
        self,
        hits: tuple[SearchHit, ...],
        mode: SearchMode,
        ranking_mode: SearchMode | None = None,
        abstained: bool = False,
    ) -> None:
        object.__setattr__(self, "hits", hits)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "ranking_mode", mode if ranking_mode is None else ranking_mode)
        object.__setattr__(self, "abstained", abstained)
        self.__post_init__()

    def __post_init__(self) -> None:
        if self.mode not in ("bm25", "rrf", "vector", "vector_bm25_fallback", "rerank"):
            raise ValueError("invalid retrieval mode")
        if self.ranking_mode != self.mode:
            raise ValueError("inconsistent retrieval modes")
        if type(self.abstained) is not bool or (
            self.abstained and (self.hits or self.mode in ("bm25", "rerank"))
        ):
            raise ValueError("invalid abstained state")


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]],
    names: Mapping[str, str],
    *,
    weights: Sequence[float] | None = None,
    constant: float = 60.0,
) -> list[tuple[str, float]]:
    if isinstance(constant, bool) or not math.isfinite(constant) or constant <= 0:
        raise ValueError("invalid RRF constant")
    channel_weights = tuple(weights) if weights is not None else (1.0,) * len(rankings)
    if len(channel_weights) != len(rankings) or any(
        isinstance(weight, bool) or not math.isfinite(weight) or weight <= 0
        for weight in channel_weights
    ):
        raise ValueError("invalid RRF weights")
    scores: dict[str, float] = {}
    for ranking, weight in zip(rankings, channel_weights, strict=True):
        if len(set(ranking)) != len(ranking) or any(key not in names for key in ranking):
            raise ValueError("RRF requires unique known parents per channel")
        for rank, tool_id in enumerate(ranking, 1):
            scores[tool_id] = scores.get(tool_id, 0.0) + weight / (constant + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], names[item[0]]))


def _allowlist(value: frozenset[str]) -> frozenset[str]:
    if not isinstance(value, frozenset) or any(not isinstance(key, str) for key in value):
        raise AuthorizationError("invalid authorization view")
    return value


class SearchEngine:
    def __init__(
        self,
        snapshot: IndexSnapshot,
        embedding: EmbeddingProvider | None = None,
        reranker: RerankProvider | None = None,
        config: SearchConfig | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.embedding = embedding
        self.reranker = reranker
        self.config = config or SearchConfig()

    @timed("search.core")
    def search(
        self,
        query: str,
        allowed_ids: frozenset[str],
        context: RequestContext,
        *,
        reauthorize: Callable[[], frozenset[str]] | None = None,
        diagnostics: Callable[[SearchDiagnostics], None] | None = None,
    ) -> SearchResult:
        trace = _SearchTrace(
            SearchDiagnostics(
                configured_fusion=self.config.fusion_strategy,
                rerank_state="skipped" if self.config.rerank_enabled else "disabled",
            )
        )
        try:
            result = self._search(query, allowed_ids, context, reauthorize=reauthorize, trace=trace)
            trace.value = replace(
                trace.value,
                empty_result=not result.hits,
                below_threshold=result.abstained,
            )
            return result
        finally:
            if diagnostics is not None:
                try:
                    diagnostics(trace.value)
                # Optional diagnostics must preserve the retrieval result or fail-closed exception.
                except Exception:  # nosec B110
                    # Observability must not change results or replace a fail-closed error.
                    pass

    def _search(
        self,
        query: str,
        allowed_ids: frozenset[str],
        context: RequestContext,
        *,
        reauthorize: Callable[[], frozenset[str]] | None,
        trace: _SearchTrace,
    ) -> SearchResult:
        if not isinstance(query, str) or not query.strip() or len(query) > 2048:
            raise ValueError("query must contain 1 to 2048 characters")
        allowed = _allowlist(allowed_ids)
        snapshot = self.snapshot
        tools = snapshot.tools
        by_id = {tool.tool_id: tool for tool in tools}
        tool_ids = tuple(by_id)
        names = {key: tool.name for key, tool in by_id.items()}
        mask = np.fromiter((key in allowed for key in tool_ids), dtype=np.bool_, count=len(tools))

        if not mask.any():
            return SearchResult((), "bm25")
        allowed_rows = mask.tolist()
        vector_scores: NDArray[np.float64] | None = None
        profile = snapshot.model_profile
        vector_index = snapshot.vectors
        trace.value = replace(
            trace.value,
            vector_availability="disabled" if self.embedding is None else "unavailable",
            degraded=self.embedding is not None,
        )
        if (
            self.embedding is not None
            and profile is not None
            and vector_index is not None
            and vector_index.vectors.shape[0] > 0
        ):
            query_vector = None
            checking_embedding_budget = False
            try:
                if self.embedding.describe() == profile:
                    stage = RequestContext(
                        context.request_id,
                        min(context.deadline, time.monotonic() + self.config.embedding_timeout),
                    )
                    with collect_embedding(
                        context.deadline, stage.deadline, self.config.embedding_timeout
                    ) as measurements:
                        try:
                            checking_embedding_budget = True
                            stage.remaining()
                            checking_embedding_budget = False
                            with timing_stage("model.embedding"):
                                embedded = self.embedding.embed([query], "query", stage)
                            stage.remaining()
                            query_vector = validate_embedding_result(embedded, profile, 1)[0]
                        finally:
                            trace.value = replace(
                                trace.value, embedding_timing=snapshot_embedding(measurements)
                            )
            except Exception as error:
                # Only the provider boundary is degraded. No raw errors are logged.
                query_vector = None
                trace.value = replace(
                    trace.value,
                    vector_availability="unavailable" if checking_embedding_budget else "failed",
                    degraded=True,
                    embedding_error=(
                        error.failure
                        if isinstance(error, ProviderError)
                        else ProviderFailure("unknown")
                    ),
                )
            if query_vector is not None:
                # Index failures are outside the provider catch and fail closed.
                trace.value = replace(trace.value, vector_availability="available", degraded=False)
                with timing_stage("search.vector"):
                    vector_scores = vector_index.scores(query_vector, allowed_rows)

        # 查询与文档使用快照内相同的版本化分词配置，不额外改写查询。
        with timing_stage("search.bm25"):
            lexical_scores = snapshot.bm25.masked_scores(query, allowed_rows)

        def masked_scores(scores: NDArray[np.float64]) -> NDArray[np.float64]:
            if scores.shape != (len(tools),):
                raise ValueError("invalid index scores")
            masked = np.array(scores, copy=True)
            masked[~mask] = -np.inf
            if np.isnan(masked).any() or np.isposinf(masked).any():
                raise ValueError("invalid index scores")
            return masked

        def ranked(scores: NDArray[np.float64]) -> list[tuple[str, float]]:
            masked = masked_scores(scores)
            indices = sorted(
                (i for i in range(len(tools)) if np.isfinite(masked[i])),
                key=lambda i: (-float(masked[i]), tools[i].name),
            )[: self.config.recall_k]
            return [(tool_ids[i], float(masked[i])) for i in indices]

        @timed("search.ranking")
        def base_ranking() -> tuple[list[tuple[str, float]], SearchMode, bool]:
            if not mask.any():
                return [], "bm25", False
            lexical = ranked(lexical_scores)
            if vector_scores is None:
                return lexical, "bm25", False
            vectors = ranked(vector_scores)
            mode = self.config.fusion_strategy
            if (
                self.config.min_vector_similarity is not None
                or self.config.min_vector_margin is not None
            ):
                authorized_scores = masked_scores(vector_scores)
                finite_scores = authorized_scores[np.isfinite(authorized_scores)]
                if finite_scores.size == 0:
                    return [], mode, True
                top_similarity = float(np.max(finite_scores))
                if (
                    self.config.min_vector_similarity is not None
                    and top_similarity < self.config.min_vector_similarity
                ):
                    return [], mode, True
                if (
                    self.config.min_vector_margin is not None
                    and top_similarity - float(np.median(finite_scores))
                    < self.config.min_vector_margin
                ):
                    return [], mode, True
            if self.config.fusion_strategy == "vector":
                return vectors, mode, False
            if self.config.fusion_strategy == "vector_bm25_fallback":
                positive_lexical = [(key, score) for key, score in lexical if score > 0]
                if (
                    not vectors or vectors[0][1] < self.config.bm25_fallback_similarity
                ) and positive_lexical:
                    return positive_lexical, "bm25", False
                vector_ids = {key for key, _ in vectors}
                # Mixed cosine/BM25 values are not comparable scores. Only this
                # vector-first list order is consumed; never sort or gate it by score.
                return (
                    (
                        vectors
                        + [(key, score) for key, score in positive_lexical if key not in vector_ids]
                    )[: self.config.recall_k],
                    mode,
                    False,
                )
            return (
                reciprocal_rank_fusion(
                    [[key for key, _ in lexical], [key for key, _ in vectors]],
                    names,
                    weights=(self.config.lexical_weight, self.config.vector_weight),
                    constant=self.config.rrf_constant,
                ),
                mode,
                False,
            )

        def refresh_authorization() -> bool:
            nonlocal allowed, mask
            if reauthorize is None:
                return False
            try:
                latest = _allowlist(reauthorize())
            except Exception:
                raise AuthorizationError("authorization unavailable") from None
            updated = allowed.intersection(latest)
            changed = updated != allowed
            allowed = updated
            if changed:
                mask &= np.fromiter(
                    (key in allowed for key in tool_ids), dtype=np.bool_, count=len(tools)
                )
            return changed

        # Recheck before any candidate text can leave the process.
        refresh_authorization()
        ranking, mode, abstained = base_ranking()
        if mask.any():
            trace.value = replace(trace.value, ranking_mode=mode, below_threshold=abstained)
        if (
            vector_scores is not None
            and self.config.rerank_enabled
            and self.reranker is not None
            and ranking
        ):
            candidates = ranking[: self.config.rerank_limit]
            documents = tuple(RerankDocument(key, by_id[key].summary) for key, _ in candidates)
            if len(query.encode("utf-8")) <= 256 and all(
                len(document.text.encode("utf-8")) <= 512 for document in documents
            ):
                rerank_attempted = False
                try:
                    stage = RequestContext(
                        context.request_id,
                        min(context.deadline, time.monotonic() + self.config.rerank_timeout),
                    )
                    stage.remaining()
                    rerank_attempted = True
                    expected_profile = self.reranker.describe()
                    with timing_stage("model.rerank"):
                        result = self.reranker.rerank(query, documents, len(documents), stage)
                    stage.remaining()
                    candidate_ids = [key for key, _ in candidates]
                    if result.profile != expected_profile or len(result.items) != len(
                        candidate_ids
                    ):
                        raise ValueError("invalid rerank response")
                    scores = {item.id: item.score for item in result.items}
                    if set(scores) != set(candidate_ids) or len(scores) != len(result.items):
                        raise ValueError("invalid rerank candidate IDs")
                    if any(
                        isinstance(score, bool) or not math.isfinite(score)
                        for score in scores.values()
                    ):
                        raise ValueError("invalid rerank scores")
                    if any(
                        left.score < right.score
                        for left, right in zip(result.items, result.items[1:], strict=False)
                    ):
                        raise ValueError("rerank results must be sorted")
                    # Stable sort retains the base order on all ties, even negative scores.
                    ranking = sorted(
                        ((key, scores[key]) for key in candidate_ids), key=lambda item: -item[1]
                    )
                    mode = "rerank"
                    trace.value = replace(trace.value, ranking_mode=mode, rerank_state="applied")
                except Exception:
                    # Keep exact base order on failure, including mixed-score fallback.
                    if rerank_attempted:
                        trace.value = replace(trace.value, rerank_state="failed", degraded=True)
        if refresh_authorization():
            # Refill from already-scored authorized channels after revocation.
            ranking, mode, abstained = base_ranking()
            if mask.any():
                trace.value = replace(trace.value, ranking_mode=mode, below_threshold=abstained)
        hits = tuple(SearchHit(by_id[key]) for key, _ in ranking if key in allowed)[
            : self.config.top_k
        ]
        return SearchResult(hits, mode, abstained=abstained)

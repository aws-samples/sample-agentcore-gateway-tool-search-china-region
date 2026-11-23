"""Explicit deployment identity and bounded, stateless scheduler configuration."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from semantic_core.catalog import GatewayIdentity

if TYPE_CHECKING:
    from semantic_core.search import SearchConfig


SEARCH_FINALIZATION_SECONDS = 0.3
_EMBEDDING_TIMEOUT_ERROR = (
    "MODEL_EMBEDDING_TIMEOUT_SECONDS must be in (0, 6] seconds "
    "(numeric string in configuration; built-in int or float when passed directly)"
)


def _positive_seconds(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, str(default))
    message = f"{name} must be positive and finite"
    if not isinstance(raw, str):
        raise ValueError(message)
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(message) from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(message)
    return value


def parse_search_request_seconds(env: Mapping[str, str]) -> float:
    """Configured Search provider budget; the Lambda tail is bounded separately."""
    name = "SEMSEARCH_SEARCH_TIMEOUT_SECONDS"
    value = _positive_seconds(env, name, 3.0)
    if not SEARCH_FINALIZATION_SECONDS < value <= 6:
        raise ValueError(f"{name} must be in ({SEARCH_FINALIZATION_SECONDS:g}, 6]")
    return value


def validate_embedding_timeout_seconds(value: float) -> float:
    """Share the query-only limit with direct ModelSettings construction."""
    if type(value) not in (int, float) or not 0 < value <= 6:
        raise ValueError(_EMBEDDING_TIMEOUT_ERROR)
    return float(value)


def parse_embedding_timeout_seconds(env: Mapping[str, str]) -> float:
    raw = env.get("MODEL_EMBEDDING_TIMEOUT_SECONDS", "1.0")
    if not isinstance(raw, str):
        raise ValueError(_EMBEDDING_TIMEOUT_ERROR)
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(_EMBEDDING_TIMEOUT_ERROR) from None
    return validate_embedding_timeout_seconds(value)


def _optional_threshold(
    env: Mapping[str, str], name: str, lower: float, upper: float
) -> float | None:
    if name not in env:
        return None
    raw = env[name]
    message = f"{name} must be a finite number in [{lower:g}, {upper:g}]"
    if not isinstance(raw, str):
        raise ValueError(message)
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(message) from None
    if not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(message)
    return value


def parse_search_config(env: Mapping[str, str]) -> SearchConfig:
    """Validate service retrieval settings without changing the core defaults.

    Parse the effective environment after pinned S3 settings have overridden it.
    Missing thresholds remain unset. The query embedding setting controls both
    core admission and the provider HTTP budget; rerank keeps its core default.
    Validate the total budget here too, including when models are disabled.
    """
    from semantic_core.search import FusionStrategy, SearchConfig

    parse_search_request_seconds(env)
    fusion = env.get("SEMSEARCH_FUSION_STRATEGY", "vector_bm25_fallback")
    if fusion not in ("rrf", "vector", "vector_bm25_fallback"):
        raise ValueError("SEMSEARCH_FUSION_STRATEGY must be rrf, vector or vector_bm25_fallback")
    rerank = env.get("MODEL_RERANK_ENABLED", "false")
    if not isinstance(rerank, str) or rerank.lower() not in ("true", "false"):
        raise ValueError("MODEL_RERANK_ENABLED must be true or false")
    raw_top_k = env.get("SEMSEARCH_TOP_K", "5")
    message = "SEMSEARCH_TOP_K must be an integer in [1, 20]"
    if not isinstance(raw_top_k, str):
        raise ValueError(message)
    try:
        top_k = int(raw_top_k)
    except ValueError:
        raise ValueError(message) from None
    if not 1 <= top_k <= 20:
        raise ValueError(message)
    return SearchConfig(
        top_k=top_k,
        fusion_strategy=cast(FusionStrategy, fusion),
        min_vector_similarity=_optional_threshold(env, "SEMSEARCH_MIN_VECTOR_SIMILARITY", -1, 1),
        min_vector_margin=_optional_threshold(env, "SEMSEARCH_MIN_VECTOR_MARGIN", 0, 2),
        rerank_enabled=rerank.lower() == "true",
        embedding_timeout=parse_embedding_timeout_seconds(env),
    )


def required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value or value != value.strip():
        raise ValueError(f"missing or invalid configuration: {name}")
    return value


@dataclass(frozen=True)
class ServiceSettings:
    gateway: GatewayIdentity
    search_target_id: str
    search_tool_name: str
    bucket: str
    table: str
    protected_table: str
    poll_seconds: int = 60
    indexer_budget_seconds: int = 45
    enhancer_budget_seconds: int = 240

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9]{12}", self.gateway.account):
            raise ValueError("invalid account")
        if self.gateway.region not in ("cn-north-1", "cn-northwest-1"):
            raise ValueError("unsupported deployment region")
        for value in (self.gateway.gateway_id, self.search_target_id):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
                raise ValueError("invalid configured resource identity")
        if not re.fullmatch(r"[A-Za-z0-9_-]+___search_tools", self.search_tool_name):
            raise ValueError("configure the exact prefixed Search tool name")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket):
            raise ValueError("invalid bucket")
        if any(
            not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", value)
            for value in (self.table, self.protected_table)
        ):
            raise ValueError("invalid table")
        if self.table == self.protected_table:
            raise ValueError("state and protected tables must be distinct")
        if (
            type(self.poll_seconds) is not int
            or self.poll_seconds < 60
            or self.indexer_budget_seconds != 45
            or self.enhancer_budget_seconds != 240
        ):
            raise ValueError("invalid scheduler budget")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServiceSettings:
        values = os.environ if env is None else env
        region = values.get("SEMSEARCH_REGION") or required(values, "AWS_REGION")
        return cls(
            GatewayIdentity(
                required(values, "SEMSEARCH_ACCOUNT"),
                region,
                required(values, "SEMSEARCH_GATEWAY_ID"),
            ),
            required(values, "SEMSEARCH_SEARCH_TARGET_ID"),
            required(values, "SEMSEARCH_SEARCH_TOOL_NAME"),
            required(values, "SEMSEARCH_BUCKET"),
            required(values, "SEMSEARCH_TABLE"),
            required(values, "SEMSEARCH_PROTECTED_TABLE"),
            int(values.get("SEMSEARCH_POLL_SECONDS", "60")),
        )


def scheduled_tick_is_current(event: object, *, now: datetime | None = None) -> bool:
    """Discard stale/future scheduled work; never accept a request's namespace.

    An empty object supports controlled direct invocation. Scheduler events use
    their RFC3339 `time`; missed or rejected ticks recover at the next schedule.
    """
    if not isinstance(event, dict) or any(
        key in event for key in ("gatewayId", "gateway_id", "account", "region")
    ):
        raise ValueError("invalid scheduled event")
    timestamp = event.get("time")
    if timestamp is None and not event:
        return True
    if not isinstance(timestamp, str):
        raise ValueError("missing scheduled timestamp")
    try:
        scheduled = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if scheduled.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise ValueError("invalid scheduled timestamp") from None
    age = ((now or datetime.now(UTC)) - scheduled).total_seconds()
    return 0 <= age <= 60

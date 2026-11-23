"""Indexer 调用级整数计数；不接收认证材料或异常文本。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# 日志与 CDK 共用字段表，避免单侧改名后指标静默无数据。
COUNTER_FIELDS = (
    "gateway_401_count",
    "gateway_403_count",
    "token_proactive_refresh_count",
    "gateway_replay_count",
    "gateway_replay_success_count",
    "gateway_replay_failure_count",
    "gateway_replay_skipped_count",
    "gateway_auth_refresh_failure_count",
)
_CURRENT: ContextVar[dict[str, int] | None] = ContextVar("indexer_counters", default=None)


@contextmanager
def collect() -> Iterator[dict[str, int]]:
    fields = dict.fromkeys(COUNTER_FIELDS, 0)
    token = _CURRENT.set(fields)
    try:
        yield fields
    finally:
        _CURRENT.reset(token)


def increment(field: str) -> None:
    fields = _CURRENT.get()
    if fields is not None and field in fields:
        fields[field] += 1


@contextmanager
def replay_attempt(enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return
    increment("gateway_replay_count")
    succeeded = False
    try:
        yield
        succeeded = True
    finally:
        increment("gateway_replay_success_count" if succeeded else "gateway_replay_failure_count")

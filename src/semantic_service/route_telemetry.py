"""Bounded facade stage timings; no arguments, URLs, credentials or exception text."""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import httpx

_STAGES = frozenset(
    {
        "handler.factory",
        "runtime.import",
        "runtime.aws_client",
        "runtime.aws_session",
        "runtime.aws_client.secretsmanager",
        "runtime.aws_client.lambda",
        "runtime.http_client",
        "runtime.oauth_source",
        "aws.secrets",
        "oauth.token_exchange",
    }
)


class Timings:
    """Same count/duration/max/errors shape as core timings; nested times are inclusive."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, float | int]] = {}
        self.oauth_rejected = False
        self.oauth_server_failed = False

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        return {
            name: {
                key: round(value, 3) if isinstance(value, float) else value
                for key, value in counts.items()
            }
            for name, counts in sorted(self.values.items())
        }


_CURRENT: ContextVar[Timings | None] = ContextVar("facade_timings", default=None)


@contextmanager
def collect() -> Iterator[Timings]:
    captured = Timings()
    token = _CURRENT.set(captured)
    try:
        yield captured
    finally:
        _CURRENT.reset(token)


@contextmanager
def stage(name: str) -> Iterator[None]:
    captured = _CURRENT.get()
    if captured is None or name not in _STAGES:
        yield
        return
    started, failed = time.perf_counter(), True
    try:
        yield
        failed = False
    finally:
        elapsed = max(0.0, time.perf_counter() - started) * 1000
        value = captured.values.setdefault(
            name, {"count": 0, "duration_ms": 0.0, "max_ms": 0.0, "errors": 0}
        )
        value["count"] += 1
        value["duration_ms"] += elapsed
        value["max_ms"] = max(value["max_ms"], elapsed)
        value["errors"] += int(failed)


class MeasuredSecrets:
    def __init__(self, client: object) -> None:
        self.client = client

    @property
    def meta(self) -> object:
        # Retain SecretsCache's strict refresh-budget inspection.
        return getattr(self.client, "meta", None)

    def get_secret_value(self, **kwargs: object) -> Mapping[str, object]:
        from semantic_service.secrets import SecretsClient

        with stage("aws.secrets"):
            return cast(SecretsClient, self.client).get_secret_value(**kwargs)


class MeasuredTokenHTTP:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    @contextmanager
    def stream(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str],
        auth: httpx.BasicAuth,
        timeout: float,
        follow_redirects: bool,
    ) -> Iterator[httpx.Response]:
        # This wrapper is used only for OAuth. Search keeps its full HTTP trace.
        # Includes body iteration by the consumer and response cleanup.
        with (
            stage("oauth.token_exchange"),
            self.client.stream(
                method,
                url,
                data=data,
                auth=auth,
                timeout=timeout,
                follow_redirects=follow_redirects,
            ) as response,
        ):
            captured = _CURRENT.get()
            if captured is not None:
                # Keep an explicit denial across the source's one credential
                # refresh. A later network failure cannot bypass that denial.
                captured.oauth_rejected |= response.status_code in {400, 401, 403}
                captured.oauth_server_failed |= response.status_code >= 500
            yield response

"""Bounded in-memory Secrets Manager cache; plaintext never enters diagnostics."""

from __future__ import annotations

import base64
import binascii
import json
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol, cast

from semantic_core._timing import stage as timing_stage

SecretStage = Literal["AWSCURRENT", "AWSPREVIOUS"]


class SecretsClient(Protocol):
    def get_secret_value(self, **kwargs: object) -> Mapping[str, object]: ...


class SecretError(Exception):
    def __init__(self) -> None:
        super().__init__("configured secret unavailable or invalid")


@dataclass(frozen=True)
class SecretValue:
    version_id: str
    value: Mapping[str, object] = field(repr=False)


def _immutable(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _immutable(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_immutable(item) for item in value)
    return value


class SecretsCache:
    """Fetch only explicit secret IDs and stages, retaining values at most five minutes."""

    def __init__(
        self,
        client: SecretsClient,
        ttl_seconds: float = 300,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not math.isfinite(ttl_seconds)
            or not 0 < ttl_seconds <= 300
        ):
            raise ValueError("secret cache TTL must be in (0, 300]")
        self.ttl_seconds = ttl_seconds
        self._client = client
        self._clock = clock
        self._entries: dict[tuple[str, SecretStage], tuple[float, SecretValue]] = {}
        self._lock = threading.RLock()

    def get(
        self,
        secret_id: str,
        *,
        stage: SecretStage = "AWSCURRENT",
        force_refresh: bool = False,
    ) -> SecretValue:
        if (
            not isinstance(secret_id, str)
            or not secret_id
            or stage
            not in (
                "AWSCURRENT",
                "AWSPREVIOUS",
            )
        ):
            raise ValueError("explicit secret ID and supported version stage required")
        key = (secret_id, stage)
        with self._lock:
            now = self._clock()
            entry = self._entries.get(key)
            if not force_refresh and entry is not None and now < entry[0]:
                return entry[1]
            # Never serve an expired value or the pre-refresh value after a failed fetch.
            self._entries.pop(key, None)
            try:
                with timing_stage("aws.secrets"):
                    response = self._client.get_secret_value(SecretId=secret_id, VersionStage=stage)
                version = response.get("VersionId")
                raw = response.get("SecretString")
                if (
                    not isinstance(version, str)
                    or not version
                    or len(version) > 128
                    or any(ord(c) < 33 or ord(c) > 126 for c in version)
                    or not isinstance(raw, str)
                    or len(raw.encode("utf-8")) > 65536
                ):
                    raise SecretError()
                value: object = json.loads(raw)
                if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
                    raise SecretError()
                result = SecretValue(version, cast(Mapping[str, object], _immutable(value)))
            except Exception:
                raise SecretError() from None
            self._entries[key] = (now + self.ttl_seconds, result)
            return result

    def invalidate(self, secret_id: str, *, stage: SecretStage = "AWSCURRENT") -> None:
        with self._lock:
            self._entries.pop((secret_id, stage), None)

    def _get_for_refresh(
        self, secret_id: str, deadline: float, exchange_timeout: float
    ) -> SecretValue | None:
        """只在既有 SDK 超时及后续交换预算充足时启动同步读取。"""
        config = getattr(getattr(self._client, "meta", None), "config", None)
        connect = getattr(config, "connect_timeout", None)
        read = getattr(config, "read_timeout", None)
        retries = getattr(config, "retries", None)
        if (
            isinstance(connect, bool)
            or not isinstance(connect, int | float)
            or isinstance(read, bool)
            or not isinstance(read, int | float)
            or not math.isfinite(connect)
            or not math.isfinite(read)
            or connect <= 0
            or read <= 0
            or not isinstance(retries, dict)
            or type(retries.get("total_max_attempts")) is not int
            or retries.get("total_max_attempts") != 1
            or retries.get("mode") != "standard"
        ):
            # 未知超时、SDK 退避或 adaptive 等待不能视为已知同步预算。
            return None
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if deadline - time.monotonic() <= connect + read + exchange_timeout:
                return None
            return self.get(secret_id, force_refresh=True)
        finally:
            self._lock.release()


class SigningSecretsKeys:
    """VersionId-keyed HMAC keys with an explicit activation time and bounded overlap.

    Each stage stores ``key_b64`` and ``activated_at`` (UTC epoch seconds). Key
    promotion must set activation time deliberately; AWS key creation time is
    not assumed to equal the time its AWSCURRENT stage was promoted.
    """

    def __init__(
        self,
        cache: SecretsCache,
        secret_id: str,
        *,
        previous_window_seconds: float = 390,
        envelope_ttl: float = 30,
        skew: float = 30,
        safety: float = 30,
        refresh_cooldown_seconds: float = 30,
        revoked_kids: frozenset[str] = frozenset(),
        clock: Callable[[], float] = time.time,
    ) -> None:
        durations = (
            previous_window_seconds,
            envelope_ttl,
            skew,
            safety,
            refresh_cooldown_seconds,
        )
        if any(isinstance(v, bool) or not math.isfinite(v) or v < 0 for v in durations):
            raise ValueError("invalid signing key rotation windows")
        if (
            previous_window_seconds < cache.ttl_seconds + envelope_ttl + skew + safety
            or refresh_cooldown_seconds <= 0
        ):
            raise ValueError("signing overlap must cover cache, envelope, skew and safety")
        self._cache = cache
        self._secret_id = secret_id
        self._previous_window = previous_window_seconds
        self._skew = skew
        self._refresh_cooldown = refresh_cooldown_seconds
        self._revoked = revoked_kids
        self._clock = clock
        self._refresh_after = 0.0
        self._previous_retry_after = 0.0
        self._lock = threading.RLock()

    def _decode(self, value: SecretValue) -> tuple[bytes, float]:
        encoded = value.value.get("key_b64")
        activated = value.value.get("activated_at")
        if (
            not isinstance(encoded, str)
            or isinstance(activated, bool)
            or not isinstance(activated, int | float)
            or not math.isfinite(activated)
            or activated < 0
            or activated > self._clock() + self._skew
            or value.version_id in self._revoked
        ):
            raise SecretError()
        try:
            key = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise SecretError() from None
        if len(key) < 32:
            raise SecretError()
        return key, float(activated)

    def current(self) -> tuple[str, bytes]:
        value = self._cache.get(self._secret_id)
        key, _ = self._decode(value)
        return value.version_id, key

    def _resolve(self, kid: str, *, refresh: bool) -> bytes | None:
        current = self._cache.get(self._secret_id, force_refresh=refresh)
        current_key, activated = self._decode(current)
        if kid == current.version_id:
            return current_key
        if self._clock() > activated + self._previous_window:
            return None
        if not refresh and self._clock() < self._previous_retry_after:
            return None
        try:
            previous = self._cache.get(self._secret_id, stage="AWSPREVIOUS", force_refresh=refresh)
        except SecretError:
            self._previous_retry_after = self._clock() + self._refresh_cooldown
            return None
        self._previous_retry_after = 0.0
        if previous.version_id != kid or previous.version_id == current.version_id:
            return None
        previous_key, previous_activated = self._decode(previous)
        if previous_activated > activated:
            raise SecretError()
        return previous_key

    def resolve(self, kid: str) -> bytes:
        if not isinstance(kid, str) or not kid or kid in self._revoked:
            raise SecretError()
        with self._lock:
            key = self._resolve(kid, refresh=False)
            if key is not None:
                return key
            now = self._clock()
            if now >= self._refresh_after:
                self._refresh_after = now + self._refresh_cooldown
                key = self._resolve(kid, refresh=True)
                if key is not None:
                    return key
            raise SecretError()

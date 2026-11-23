"""Shared bounded HTTP transport with explicit configuration and injected credentials."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import cast
from urllib.parse import unquote, urlsplit

import httpx

from semantic_core._embedding_telemetry import (
    AttemptTrace,
    http_extensions,
    record_http,
    start_http_attempt,
)
from semantic_core.providers import (
    _MAX_DIAGNOSTIC_COUNTER,
    _RATE_LIMIT_HEADERS,
    ErrorCode,
    FailureKind,
    ModelProfile,
    ProviderError,
    ProviderFailure,
    RequestContext,
)

_USAGE_FIELDS = {
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "prompt_tokens",
    "completion_tokens",
}
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_RESERVED_HEADERS = {
    "host",
    "content-length",
    "content-type",
    "connection",
    "transfer-encoding",
    "proxy-authorization",
    "x-request-id",
}


def positive_int(value: object, name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def validate_path(path: str) -> None:
    """Allow named dot segments, but reject decoded dot-directory traversal.

    Semicolon path parameters do not change whether a segment is ``.`` or
    ``..``. Decoding and parameter removal apply only to validation; the
    configured route is never rewritten or allowed to replace its origin.
    """
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or any(ord(character) < 33 or ord(character) > 126 for character in path)
        or any(character in path for character in "\\?#")
    ):
        raise ValueError("endpoint path must be an absolute URL path")
    decoded = path
    while True:
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    if (
        any(segment.split(";", 1)[0] in (".", "..") for segment in decoded.split("/"))
        or decoded.startswith("//")
        or "\\" in decoded
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
    ):
        raise ValueError("endpoint path cannot contain traversal or control characters")


def _frozen_json(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("request extension keys must be strings")
        return MappingProxyType({key: _frozen_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_frozen_json(item) for item in value)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("request extensions must contain finite JSON values")


class _ExtensionBody(Mapping[str, object]):
    """Read-only mapping with an immutable snapshot and detached JSON readback.

    Arrays and objects read back as ordinary lists and dictionaries, matching
    their request-body representation. Mutating a returned value cannot change
    the stored snapshot or a subsequent request.
    """

    __slots__ = ("_snapshot",)

    def __init__(self, value: Mapping[str, object]) -> None:
        self._snapshot = cast(Mapping[str, object], _frozen_json(value))

    def __getitem__(self, key: str) -> object:
        return _plain_json(self._snapshot[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._snapshot)

    def __len__(self) -> int:
        return len(self._snapshot)

    def __repr__(self) -> str:
        return repr(dict(self))

    def __hash__(self) -> int:
        return _json_hash(self._snapshot)


def _json_hash(value: object) -> int:
    if isinstance(value, Mapping):
        return hash(frozenset((key, _json_hash(item)) for key, item in value.items()))
    if isinstance(value, tuple):
        return hash(tuple(_json_hash(item) for item in value))
    return hash(value)


def freeze_body(value: Mapping[str, object], reserved: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or key.lower() in reserved for key in value
    ):
        raise ValueError("request extensions cannot override protected fields")
    return _ExtensionBody(value)


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain_json(item) for item in value]
    return value


@dataclass(frozen=True)
class HTTPConfig:
    """A trusted origin, route prefix and auth format, never a credential store.

    Paths append to the base URL prefix. Only this exact HTTPS host is used;
    redirects are disabled. Explicit credentials are constructor arguments, while
    auth_header_name=None retains authentication on the caller's injected client.
    """

    base_url: str
    allowed_hosts: tuple[str, ...]
    timeout_seconds: float = 1.0
    max_response_bytes: int = 1024 * 1024
    response_model_aliases: tuple[str, ...] = ()
    auth_header_name: str | None = None
    auth_header_format: str = "Bearer {token}"
    validate_model_echo: bool = True
    validate_version_echo: bool = False

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.base_url)
            port = parsed.port
        except (TypeError, ValueError):
            raise ValueError("invalid configured HTTPS endpoint") from None
        if (
            not isinstance(self.base_url, str)
            or any(ord(character) < 33 or ord(character) > 126 for character in self.base_url)
            or parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
            or not isinstance(self.allowed_hosts, tuple)
            or not self.allowed_hosts
            or any(
                not isinstance(host, str)
                or not host
                or host != host.lower()
                or any(character in host for character in "/*\\?#@:")
                or any(ord(character) < 33 or ord(character) > 126 for character in host)
                for host in self.allowed_hosts
            )
            or parsed.hostname not in self.allowed_hosts
        ):
            raise ValueError("endpoint must match an explicit HTTPS host allowlist")
        validate_path(parsed.path or "/")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("invalid adapter timeout")
        positive_int(self.max_response_bytes, "max_response_bytes")
        if (
            not isinstance(self.response_model_aliases, tuple)
            or any(
                not isinstance(alias, str)
                or not alias
                or len(alias) > 512
                or any(character.isspace() or ord(character) < 32 for character in alias)
                or "\x7f" in alias
                or "*" in alias
                or "?" in alias
                for alias in self.response_model_aliases
            )
            or len(set(self.response_model_aliases)) != len(self.response_model_aliases)
        ):
            raise ValueError("response model aliases must be unique exact model identifiers")
        if (
            type(self.validate_model_echo) is not bool
            or type(self.validate_version_echo) is not bool
        ):
            raise ValueError("response identity switches must be boolean")
        if self.auth_header_name is not None and (
            not isinstance(self.auth_header_name, str)
            or _HEADER_NAME.fullmatch(self.auth_header_name) is None
            or self.auth_header_name.lower() in _RESERVED_HEADERS
        ):
            raise ValueError("invalid authentication header name")
        if (
            not isinstance(self.auth_header_format, str)
            or self.auth_header_format.count("{token}") != 1
            or any(
                character in self.auth_header_format.replace("{token}", "") for character in "{}"
            )
            or any(
                ord(character) < 32 or ord(character) > 126 for character in self.auth_header_format
            )
        ):
            raise ValueError("authentication format requires exactly one literal {token} field")


def validate_preset_origin(transport: HTTPConfig, origin_only: bool) -> None:
    """Keep full-path direct-provider presets separate from proxy prefixes."""
    if type(origin_only) is not bool:
        raise ValueError("origin-only preset switch must be boolean")
    if origin_only and urlsplit(transport.base_url).path not in ("", "/"):
        raise ValueError("direct-provider preset requires an origin-only base URL")


class _HTTPResponse(dict[str, object]):
    """正文键保持原样；成功状态仅随当前响应在进程内传递。"""

    __slots__ = ("status_code",)

    def __init__(self, body: Mapping[str, object], status_code: int) -> None:
        super().__init__(body)
        self.status_code = status_code


def _response_failure(
    error: ProviderError, response: Mapping[str, object] | None
) -> ProviderFailure:
    if error.failure.http_status is not None or not isinstance(response, _HTTPResponse):
        return error.failure
    return replace(error.failure, http_status=response.status_code)


class _HTTPProvider:
    def __init__(
        self,
        client: httpx.Client,
        config: HTTPConfig,
        profile: ModelProfile,
        *,
        credential: str | None = None,
    ) -> None:
        if config.auth_header_name is None:
            if credential is not None:
                raise ValueError("credential requires an explicit authentication header")
        elif (
            not isinstance(credential, str)
            or not credential.strip()
            or any(ord(character) < 32 or ord(character) > 126 for character in credential)
        ):
            raise ValueError("explicit authentication requires a valid injected credential")
        self._client = client
        self._config = config
        self._profile = profile
        self._auth_headers = (
            {}
            if config.auth_header_name is None
            else {config.auth_header_name: config.auth_header_format.format(token=credential)}
        )

    def describe(self) -> ModelProfile:
        return self._profile

    def _post(
        self, path: str, payload: Mapping[str, object], context: RequestContext
    ) -> dict[str, object]:
        validate_path(path)
        remaining = min(context.remaining(), self._config.timeout_seconds)
        deadline = time.monotonic() + remaining
        attempt = start_http_attempt(remaining)
        try:
            return self._post_response(path, payload, context, remaining, deadline, attempt)
        finally:
            record_http(attempt, "finish")

    def _post_response(
        self,
        path: str,
        payload: Mapping[str, object],
        context: RequestContext,
        remaining: float,
        deadline: float,
        attempt: AttemptTrace | None,
    ) -> dict[str, object]:
        raw = bytearray()
        status: int | None = None
        try:
            with self._client.stream(
                "POST",
                self._config.base_url.rstrip("/") + path,
                json=_plain_json(payload),
                headers={**self._auth_headers, "X-Request-ID": context.request_id},
                auth=None if self._auth_headers else httpx.USE_CLIENT_DEFAULT,
                timeout=httpx.Timeout(remaining),
                follow_redirects=False,
                extensions=http_extensions(attempt),
            ) as response:
                status = response.status_code
                record_http(attempt, "headers", status=status)
                _check_status(status, response.headers)
                body_complete = False
                record_http(attempt, "body_started")
                try:
                    for chunk in response.iter_bytes():
                        if chunk:
                            record_http(attempt, "body_chunk")
                        if time.monotonic() >= deadline:
                            raise ProviderError(
                                ErrorCode.TIMEOUT,
                                failure=ProviderFailure("deadline_exceeded", http_status=status),
                            )
                        if len(raw) + len(chunk) > self._config.max_response_bytes:
                            raise ProviderError(
                                ErrorCode.INVALID_RESPONSE,
                                failure=ProviderFailure("response_parse", http_status=status),
                            )
                        raw.extend(chunk)
                    body_complete = True
                finally:
                    record_http(attempt, "body_finished", complete=body_complete)
        except httpx.TimeoutException as error:
            kind: FailureKind = (
                "connect_timeout"
                if isinstance(error, httpx.ConnectTimeout)
                else "read_timeout"
                if isinstance(error, httpx.ReadTimeout)
                else "write_timeout"
                if isinstance(error, httpx.WriteTimeout)
                else "pool_timeout"
                if isinstance(error, httpx.PoolTimeout)
                else "timeout"
            )
            raise ProviderError(
                ErrorCode.TIMEOUT, failure=ProviderFailure(kind, http_status=status)
            ) from None
        except httpx.DecodingError:
            # 保持旧 ErrorCode；诊断区分响应解码失败与网络故障。
            raise ProviderError(
                ErrorCode.UNAVAILABLE,
                failure=ProviderFailure("response_parse", http_status=status),
            ) from None
        except httpx.RequestError:
            raise ProviderError(
                ErrorCode.UNAVAILABLE,
                failure=ProviderFailure("network_error", http_status=status),
            ) from None
        if time.monotonic() >= deadline:
            raise ProviderError(
                ErrorCode.TIMEOUT,
                failure=ProviderFailure("deadline_exceeded", http_status=status),
            )
        try:
            decoded: object = json.loads(raw)
            assert status is not None
            return _HTTPResponse(_object(decoded), status)
        except (ValueError, UnicodeError, ProviderError):
            raise ProviderError(
                ErrorCode.INVALID_RESPONSE,
                failure=ProviderFailure("response_parse", http_status=status),
            ) from None

    def _validate_identity(self, response: Mapping[str, object]) -> None:
        if self._config.validate_model_echo and "model" in response:
            reported = response["model"]
            if not isinstance(reported, str) or (
                reported != self._profile.model_id
                and reported not in self._config.response_model_aliases
            ):
                raise ProviderError(ErrorCode.INVALID_RESPONSE)
        # Compatibility-only opt-in; generic OpenAI responses ignore these
        # nonstandard fields and always return the configured local profile.
        if self._config.validate_version_echo:
            if "revision" in response and response["revision"] != self._profile.revision:
                raise ProviderError(ErrorCode.INVALID_RESPONSE)
            if (
                "config_version" in response
                and response["config_version"] != self._profile.config_version
            ):
                raise ProviderError(ErrorCode.INVALID_RESPONSE)


def _numeric_header(headers: httpx.Headers, name: str) -> int | None:
    value = headers.get(name)
    # 严格拒绝日期、单位、重复头拼接及任意文本，不截取或记录原值。
    if value is None or re.fullmatch(r"[0-9]{1,10}", value) is None:
        return None
    number = int(value)
    return number if number <= _MAX_DIAGNOSTIC_COUNTER else None


def _check_status(status: int, headers: httpx.Headers) -> None:
    if 200 <= status < 300:
        return
    if status in (401, 403):
        code = ErrorCode.AUTHENTICATION
    elif status == 429:
        code = ErrorCode.RATE_LIMITED
    elif status in (408, 504):
        code = ErrorCode.TIMEOUT
    elif 400 <= status < 500:
        code = ErrorCode.INVALID_REQUEST
    elif status >= 500:
        code = ErrorCode.UNAVAILABLE
    else:
        code = ErrorCode.INVALID_RESPONSE
    kind: FailureKind = (
        "http_rate_limited"
        if status == 429
        else "http_client_error"
        if 400 <= status < 500
        else "http_server_error"
        if 500 <= status < 600
        else "http_status_error"
    )
    retry_after = _numeric_header(headers, "retry-after") if status in (429, 503) else None
    rate_limits: tuple[tuple[str, int], ...] = ()
    if status == 429:
        rate_limits = tuple(
            (name, value)
            for name in _RATE_LIMIT_HEADERS
            if (value := _numeric_header(headers, name)) is not None
        )
    raise ProviderError(
        code,
        failure=ProviderFailure(
            kind,
            http_status=status if 100 <= status <= 599 else None,
            retry_after_seconds=retry_after,
            rate_limit_headers=rate_limits,
        ),
    )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ProviderError(ErrorCode.INVALID_RESPONSE)
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ProviderError(ErrorCode.INVALID_RESPONSE)
    return cast(list[object], value)


def _index(value: object, count: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < count:
        raise ProviderError(ErrorCode.INVALID_RESPONSE)
    return value


def _score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProviderError(ErrorCode.INVALID_RESPONSE)
    try:
        result = float(value)
    except OverflowError:
        raise ProviderError(ErrorCode.INVALID_RESPONSE) from None
    if not math.isfinite(result):
        raise ProviderError(ErrorCode.INVALID_RESPONSE)
    return result


def _usage(response: Mapping[str, object]) -> Mapping[str, int]:
    """Preserve known counters; tolerate unknown fields and nested token details."""
    if "usage" not in response:
        return {}
    raw = _object(response["usage"])
    usage: dict[str, int] = {}
    invalid = False
    for key, value in raw.items():
        if key not in _USAGE_FIELDS:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            invalid = True
        else:
            usage[key] = value
    if invalid:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, usage=usage)
    return usage


def _utf8_size(text: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeError:
        raise ProviderError(ErrorCode.INVALID_REQUEST) from None

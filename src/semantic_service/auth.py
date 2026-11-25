"""Trusted identity, signed request binding and fail-closed authorization."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import math
import random
import secrets
import socket
import ssl
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast
from urllib.parse import urlsplit

import httpx
import jwt
from botocore.exceptions import ClientError

from semantic_core._timing import stage, timed
from semantic_core.catalog import GatewayIdentity, ToolIdentity, canonical_json
from semantic_service._mcp_id import require_mcp_id

if TYPE_CHECKING:
    from semantic_service.storage import ControlRecord


_OIDC_DIAGNOSTICS: ContextVar[dict[str, object] | None] = ContextVar(
    "oidc_diagnostics", default=None
)
_JWK_VALIDATION_COUNTS: ContextVar[tuple[int, dict[str, int]] | None] = ContextVar(
    "jwk_validation_counts", default=None
)
_OIDC_ATTEMPTS: ContextVar[Callable[[dict[str, object]], None] | None] = ContextVar(
    "oidc_attempts", default=None
)
_BINDING_DIAGNOSTICS: ContextVar[dict[str, object] | None] = ContextVar(
    "binding_diagnostics", default=None
)


@contextmanager
def collect_binding_diagnostics() -> Iterator[dict[str, object]]:
    """Collect fixed verification facts in this invocation only."""
    fields: dict[str, object] = {}
    token = _BINDING_DIAGNOSTICS.set(fields)
    try:
        yield fields
    finally:
        _BINDING_DIAGNOSTICS.reset(token)


@contextmanager
def collect_oidc_attempts(
    emit: Callable[[dict[str, object]], None] | None,
) -> Iterator[None]:
    """Bind a runtime-owned sink without retaining request state on a verifier."""
    token = _OIDC_ATTEMPTS.set(emit)
    try:
        yield
    finally:
        _OIDC_ATTEMPTS.reset(token)


@contextmanager
def collect_oidc_diagnostics() -> Iterator[dict[str, object]]:
    """Collect bounded recovery facts for one REQUEST invocation, never on the verifier."""
    fields: dict[str, object] = {}
    token = _OIDC_DIAGNOSTICS.set(fields)
    validation_token = _JWK_VALIDATION_COUNTS.set((0, {}))
    try:
        yield fields
    finally:
        _JWK_VALIDATION_COUNTS.reset(validation_token)
        _OIDC_DIAGNOSTICS.reset(token)


class ServiceError(RuntimeError):
    """A fixed local semantic error; live Gateway wire mapping is not asserted."""

    message = "internal failure"

    def __init__(self) -> None:
        self.oidc_failure: _OIDCFailure | None = None
        super().__init__(self.message)


class InputError(ServiceError):
    message = "invalid input"


class AuthenticationError(ServiceError):
    message = "unauthenticated"


class AuthorizationError(ServiceError):
    message = "unauthorized"


class IndexNotReadyError(ServiceError):
    message = "index not ready"


class RateLimitError(ServiceError):
    message = "rate limited"

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__()


def _text(value: object, maximum: int = 2048) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("invalid text")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid integer")
    return value


def _strings(value: object, maximum: int = 256) -> frozenset[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("invalid string collection")
    return frozenset(_text(item) for item in value)


def _public_url(value: str, hosts: frozenset[str]) -> None:
    if (
        not isinstance(hosts, frozenset)
        or not hosts
        or any(not isinstance(host, str) or not host or host != host.lower() for host in hosts)
    ):
        raise ValueError("identity host allowlist must be explicit")
    parsed = urlsplit(value)
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or host is None
        or host not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or host == "localhost"
        or host.endswith(".localhost")
    ):
        raise ValueError("invalid identity endpoint")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("identity endpoint must be public") from None
    else:
        # ipaddress.IPv4Address/IPv6Address.is_global is a boolean property.
        # nosemgrep: is-function-without-parentheses
        if not address.is_global:
            raise ValueError("identity endpoint must be public")
    # Evaluate now so malformed ports fail configuration, not authentication.
    _ = parsed.port


@dataclass(frozen=True, slots=True)
class Principal:
    issuer: str
    subject: str
    scopes: frozenset[str]
    groups: frozenset[str]
    issued_at: int
    expires_at: int

    def __post_init__(self) -> None:
        _text(self.issuer)
        _text(self.subject)
        for collection in (self.scopes, self.groups):
            if not isinstance(collection, frozenset) or len(collection) > 256:
                raise ValueError("invalid principal claims")
            for item in collection:
                _text(item)
        if not 0 < _integer(self.expires_at) - _integer(self.issued_at) <= 300:
            raise ValueError("invalid principal lifetime")


@dataclass(frozen=True, slots=True)
class OIDCConfig:
    issuer: str
    jwks_uri: str
    audience: str
    allowed_hosts: frozenset[str]
    algorithms: tuple[str, ...] = ("RS256",)
    scope_claim: str = "scope"
    groups_claim: str = "groups"
    token_use_claim: str = "token_use"
    token_use: str = "access"
    discovery_uri: str | None = None
    clock_skew: int = 30
    max_token_lifetime: int = 300
    cache_seconds: int = 300
    refresh_cooldown: int = 30

    def __post_init__(self) -> None:
        _public_url(self.issuer, self.allowed_hosts)
        _public_url(self.jwks_uri, self.allowed_hosts)
        if self.discovery_uri is not None:
            _public_url(self.discovery_uri, self.allowed_hosts)
        _text(self.audience)
        if not self.algorithms or any(
            alg not in {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA"}
            for alg in self.algorithms
        ):
            raise ValueError("only configured asymmetric JWT algorithms are permitted")
        if (
            type(self.clock_skew) is not int
            or type(self.max_token_lifetime) is not int
            or not 0 <= self.clock_skew <= 30
            or not 1 <= self.max_token_lifetime <= 300
        ):
            raise ValueError("invalid token lifetime")
        if (
            type(self.cache_seconds) is not int
            or type(self.refresh_cooldown) is not int
            or not 1 <= self.cache_seconds <= 300
            or self.refresh_cooldown < 1
        ):
            raise ValueError("invalid JWKS cache policy")


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal: ...


@dataclass(frozen=True, slots=True)
class _OIDCFailure:
    """Code-owned diagnostic values; never retain remote text or credentials."""

    endpoint: Literal["discovery", "jwks", "jwk"]
    category: Literal["http", "timeout", "network", "document", "unexpected"]
    reason: str
    http_status: int | None = None
    cause: str | None = None


class _OIDCMetadataError(Exception):
    def __init__(self, failure: _OIDCFailure) -> None:
        self.failure = failure
        super().__init__("identity dependency failure")


def _transport_cause(error: BaseException) -> str | None:
    # Only explicit, typed causes are evidence. Messages and implicit contexts
    # may contain URLs, tokens or misleading text, and must never be inspected.
    seen: set[int] = set()
    current: BaseException | None = error
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        for kind, name in (
            (socket.gaierror, "dns"),
            (ssl.SSLCertVerificationError, "tls_certificate"),
            (ssl.SSLError, "tls"),
            (ConnectionRefusedError, "connection_refused"),
            (ConnectionResetError, "connection_reset"),
        ):
            if isinstance(current, kind):
                return name
        current = current.__cause__
    return None


def _fetch_failure(
    endpoint: Literal["discovery", "jwks"], error: Exception, status: int | None
) -> _OIDCFailure:
    if isinstance(error, httpx.TimeoutException):
        reason = "unknown"
        for kind, name in (
            (httpx.ConnectTimeout, "connect"),
            (httpx.ReadTimeout, "read"),
            (httpx.WriteTimeout, "write"),
            (httpx.PoolTimeout, "pool"),
        ):
            if isinstance(error, kind):
                reason = name
                break
        return _OIDCFailure(endpoint, "timeout", reason, status, _transport_cause(error))
    if isinstance(error, httpx.DecodingError):
        return _OIDCFailure(endpoint, "document", "content_encoding", status)
    if isinstance(error, UnicodeError):
        return _OIDCFailure(endpoint, "document", "json_encoding", status)
    if isinstance(error, json.JSONDecodeError):
        return _OIDCFailure(endpoint, "document", "json_invalid", status)
    if isinstance(error, httpx.TransportError):
        reason = "unknown"
        for network_kind, name in (
            (httpx.ConnectError, "connect"),
            (httpx.ReadError, "read"),
            (httpx.WriteError, "write"),
            (httpx.CloseError, "close"),
            (httpx.ProtocolError, "protocol"),
        ):
            if isinstance(error, network_kind):
                reason = name
                break
        return _OIDCFailure(endpoint, "network", reason, status, _transport_cause(error))
    return _OIDCFailure(endpoint, "unexpected", "unknown", status)


@dataclass(slots=True)
class _OIDCRequest:
    """Recovery and discovery state never survive a single verification."""

    retry_available: bool = True
    discovery_validated: bool = False
    jwks_attempts: int = 0
    first_timeout: float = 2.0
    retry_timeout: float = 0.5
    deadline: float | None = None


def _attempt_started() -> float | None:
    try:
        return time.perf_counter()
    except Exception:
        # Optional diagnostics must not prevent the original fetch.
        return None


def _emit_jwks_attempt(
    emit: Callable[[dict[str, object]], None],
    *,
    started: float | None,
    attempt: int,
    compensation: bool,
    outcome: Literal["ok", "failed", "interrupted"],
    failure: _OIDCFailure | None,
) -> None:
    try:
        if started is None or not 1 <= attempt <= 3:
            return
        duration_ms = (time.perf_counter() - started) * 1000
        if not math.isfinite(duration_ms):
            return
        emit(
            {
                "operation": "oidc_fetch",
                "event": "attempt_finished",
                "endpoint": "jwks",
                "attempt": attempt,
                "compensation": compensation,
                "duration_ms": round(max(0.0, duration_ms), 3),
                "outcome": outcome,
                "failure": None if failure is None else asdict(failure),
            }
        )
    # Telemetry cannot replace the fetch outcome or fail-closed authentication.
    except Exception:  # nosec B110
        # Includes serialization/output failures; never replace the fetch outcome.
        pass


def _retryable_metadata(failure: _OIDCFailure) -> bool:
    if failure.cause in {"tls", "tls_certificate"}:
        return False
    return (failure.category == "http" and failure.http_status in {500, 502, 503, 504}) or (
        failure.category == "timeout" and failure.reason in {"connect", "read"}
    )


_JWK_REJECTION_REASONS = frozenset(
    {
        "usage",
        "key_ops",
        "private_material",
        "algorithm",
        "missing_material",
        "encoding",
        "material",
    }
)


def _jwk_supports_algorithm(key: dict[str, object], algorithm: str) -> bool:
    """Missing alg allows compatible hashes, never a different key family or curve."""
    if key.get("alg", algorithm) != algorithm:
        return False
    if algorithm in {"RS256", "RS384", "RS512"}:
        return key.get("kty") == "RSA"
    curves = {"ES256": "P-256", "ES384": "P-384", "ES512": "P-521"}
    if algorithm in curves:
        return key.get("kty") == "EC" and key.get("crv") == curves[algorithm]
    return (
        algorithm == "EdDSA" and key.get("kty") == "OKP" and key.get("crv") in ("Ed25519", "Ed448")
    )


def _record_jwks_validation(accepted: int, rejection_reasons: dict[str, int]) -> None:
    """Publish only finite rejection categories, with no key identity or material."""
    fields, counts = _OIDC_DIAGNOSTICS.get(), _JWK_VALIDATION_COUNTS.get()
    if fields is None or counts is None:
        return
    previous_accepted, previous_reasons = counts
    reasons = dict(previous_reasons)
    for reason, count in rejection_reasons.items():
        reasons[reason] = reasons.get(reason, 0) + count
    total_accepted = previous_accepted + accepted
    _JWK_VALIDATION_COUNTS.set((total_accepted, reasons))
    if reasons:
        fields["jwks_validation"] = {
            "accepted": total_accepted,
            "rejected": sum(reasons.values()),
            "rejection_reasons": reasons,
        }


def _normalized_signing_key(
    key: dict[str, object],
    algorithms: tuple[str, ...],
    *,
    rejection_reasons: dict[str, int] | None = None,
) -> dict[str, object] | None:
    """Select configured public signing material, never cache remote annotations."""

    def reject(reason: str) -> None:
        if rejection_reasons is not None:
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

    if key.get("use", "sig") != "sig":
        reject("usage")
        return None
    if "key_ops" in key and key["key_ops"] != ["verify"]:
        reject("key_ops")
        return None
    if any(name in key for name in ("d", "p", "q", "dp", "dq", "qi", "oth", "k")):
        reject("private_material")
        return None
    if not key.get("kty"):
        reject("missing_material")
        return None
    for algorithm in algorithms:
        if not _jwk_supports_algorithm(key, algorithm):
            continue
        material_fields = (
            ("n", "e")
            if key.get("kty") == "RSA"
            else ("x", "y")
            if key.get("kty") == "EC"
            else ("x",)
        )
        if any(key.get(name) in (None, "") for name in material_fields):
            reject("missing_material")
            return None
        try:
            for name in material_fields:
                encoded = _text(key.get(name), 256 * 1024)
                material = base64.b64decode(
                    encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
                )
                if (
                    not material
                    or base64.urlsafe_b64encode(material).decode().rstrip("=") != encoded
                ):
                    raise ValueError("invalid JWK material encoding")
        except Exception:
            reject("encoding")
            return None
        try:
            public = jwt.PyJWK.from_dict(key, algorithm=algorithm)
            normalized = cast(dict[str, object], public.Algorithm.to_jwk(public.key, as_dict=True))
        except Exception:
            reject("material")
            return None
        # Algorithms above only accept asymmetric material, and private fields
        # were excluded before parsing. Export again to remove x5c/x5u and any
        # other issuer-controlled metadata from both request and Init caches.
        normalized["kid"] = key["kid"]
        if "alg" in key:
            normalized["alg"] = key["alg"]
        if "use" in key:
            normalized["use"] = "sig"
        if "key_ops" in key:
            normalized["key_ops"] = ["verify"]
        return normalized
    reject("algorithm")
    return None


def _validated_jwks(raw: object, algorithms: tuple[str, ...]) -> dict[str, dict[str, object]]:
    """Validate an entire key set and retain only usable configured public keys."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        raise _OIDCMetadataError(_OIDCFailure("jwks", "document", "jwks_keys", 200))
    candidates: dict[str, dict[str, object]] = {}
    for value in raw:
        if not isinstance(value, dict):
            raise _OIDCMetadataError(_OIDCFailure("jwks", "document", "jwk_entry", 200))
        key = cast(dict[str, object], value)
        try:
            kid = _text(key.get("kid"), 256)
        except ValueError:
            raise _OIDCMetadataError(_OIDCFailure("jwks", "document", "jwk_kid", 200)) from None
        if kid in candidates:
            raise _OIDCMetadataError(_OIDCFailure("jwks", "document", "duplicate_kid", 200))
        candidates[kid] = key
    keys: dict[str, dict[str, object]] = {}
    rejection_reasons: dict[str, int] = {}
    for kid, key in candidates.items():
        normalized = _normalized_signing_key(key, algorithms, rejection_reasons=rejection_reasons)
        if normalized is not None:
            keys[kid] = normalized
    _record_jwks_validation(len(keys), rejection_reasons)
    if not keys:
        raise _OIDCMetadataError(_OIDCFailure("jwk", "document", "jwk_material"))
    return keys


class OIDCVerifier:
    """Exact configured issuer/algorithm/host validation; no token-provided URLs."""

    def __init__(
        self,
        config: OIDCConfig,
        client: httpx.Client,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.client = client
        self.clock = clock
        self._keys: dict[str, dict[str, object]] = {}
        self._expires = -math.inf
        self._last_unknown = -math.inf

    def _prefetch(
        self,
        *,
        first_timeout: float = 2.0,
        retry_timeout: float = 0.5,
        deadline: float | None = None,
    ) -> None:
        """Warm metadata with an isolated retry budget; cache only a valid result."""
        if self.clock() >= self._expires:
            self._refresh(
                _OIDCRequest(
                    first_timeout=first_timeout, retry_timeout=retry_timeout, deadline=deadline
                )
            )

    def _get_json(
        self, url: str, endpoint: Literal["discovery", "jwks"], *, timeout: float
    ) -> dict[str, object]:
        status: int | None = None
        try:
            try:
                _public_url(url, self.config.allowed_hosts)
            except ValueError:
                raise _OIDCMetadataError(
                    _OIDCFailure(endpoint, "document", "endpoint_invalid")
                ) from None
            with self.client.stream(
                "GET", url, follow_redirects=False, timeout=timeout
            ) as response:
                status = response.status_code
                if status != 200:
                    raise _OIDCMetadataError(_OIDCFailure(endpoint, "http", "status", status))
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 256 * 1024:
                        raise _OIDCMetadataError(
                            _OIDCFailure(endpoint, "document", "body_too_large", status)
                        )
            result: object = json.loads(data)
            if not isinstance(result, dict):
                raise _OIDCMetadataError(_OIDCFailure(endpoint, "document", "not_object", status))
            return cast(dict[str, object], result)
        except _OIDCMetadataError:
            raise
        except Exception as error:
            raise _OIDCMetadataError(_fetch_failure(endpoint, error, status)) from None

    def _fetch_json(
        self, url: str, endpoint: Literal["discovery", "jwks"], request: _OIDCRequest
    ) -> dict[str, object]:
        def attempt(timeout: float, *, compensation: bool = False) -> dict[str, object]:
            if request.deadline is not None:
                remaining = request.deadline - time.monotonic()
                if remaining <= 0:
                    raise _OIDCMetadataError(_OIDCFailure(endpoint, "timeout", "read"))
                timeout = min(timeout, remaining)
            if endpoint == "jwks":
                request.jwks_attempts += 1
            emit = _OIDC_ATTEMPTS.get() if endpoint == "jwks" else None
            started = _attempt_started() if emit is not None else None
            outcome: Literal["ok", "failed", "interrupted"] = "interrupted"
            failure = None
            try:
                with stage(f"auth.{endpoint}"):
                    result = self._get_json(url, endpoint, timeout=timeout)
                outcome = "ok"
                return result
            except _OIDCMetadataError as error:
                outcome, failure = "failed", error.failure
                raise
            except Exception:
                outcome = "failed"
                failure = _OIDCFailure(endpoint, "unexpected", "unknown")
                raise
            finally:
                if emit is not None:
                    _emit_jwks_attempt(
                        emit,
                        started=started,
                        attempt=request.jwks_attempts,
                        compensation=compensation,
                        outcome=outcome,
                        failure=failure,
                    )

        try:
            return attempt(request.first_timeout)
        except _OIDCMetadataError as error:
            if not request.retry_available or not _retryable_metadata(error.failure):
                raise
        request.retry_available = False
        # Retry scheduling jitter never generates authentication material.
        delay = random.uniform(0.025, 0.075)  # nosec B311
        if request.deadline is not None:
            delay = min(delay, max(0.0, request.deadline - time.monotonic()))
        time.sleep(delay)
        # These are per-operation idle timeouts, not an end-to-end deadline.
        result = attempt(request.retry_timeout, compensation=True)
        fields = _OIDC_DIAGNOSTICS.get()
        if fields is not None:
            fields["auth_recovered"] = True
        return result

    def _read_keys(self, request: _OIDCRequest) -> None:
        if self.config.discovery_uri is not None and not request.discovery_validated:
            document = self._fetch_json(self.config.discovery_uri, "discovery", request)
            if (
                document.get("issuer") != self.config.issuer
                or document.get("jwks_uri") != self.config.jwks_uri
            ):
                raise _OIDCMetadataError(
                    _OIDCFailure("discovery", "document", "discovery_mismatch", 200)
                )
            request.discovery_validated = True
        document = self._fetch_json(self.config.jwks_uri, "jwks", request)
        self._keys = _validated_jwks(document.get("keys"), self.config.algorithms)
        self._expires = self.clock() + self.config.cache_seconds

    def _refresh(self, request: _OIDCRequest) -> None:
        try:
            self._read_keys(request)
        except _OIDCMetadataError as metadata:
            error = ServiceError()
            error.oidc_failure = metadata.failure
            raise error from None
        except Exception:
            # The configured issuer's transport/document is an internal
            # dependency, not evidence that the caller's JWT is invalid.
            raise ServiceError() from None

    @timed("auth.oidc")
    def verify(self, token: str) -> Principal:
        try:
            _text(token, 16384)
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            kid = _text(header.get("kid"), 256)
            if algorithm not in self.config.algorithms or any(
                name in header for name in ("jku", "x5u", "jwk", "crit")
            ):
                raise AuthenticationError()
            now = self.clock()
            request = _OIDCRequest()
            if now >= self._expires:
                self._refresh(request)
            if kid not in self._keys and now - self._last_unknown >= self.config.refresh_cooldown:
                self._last_unknown = now
                self._refresh(request)
            key = self._keys.get(kid)
            if key is None or key.get("use", "sig") != "sig":
                raise AuthenticationError()
            if not _jwk_supports_algorithm(key, cast(str, algorithm)):
                raise AuthenticationError()
            if "key_ops" in key and key["key_ops"] != ["verify"]:
                raise AuthenticationError()
            try:
                pykey = jwt.PyJWK.from_dict(key, algorithm=cast(str, algorithm))
            except Exception:
                error = ServiceError()
                error.oidc_failure = _OIDCFailure("jwk", "document", "jwk_material")
                raise error from None
            claims = jwt.decode(
                token,
                pykey.key,
                algorithms=list(self.config.algorithms),
                issuer=self.config.issuer,
                audience=self.config.audience,
                options={
                    "require": ["iss", "sub", "aud", "iat", "exp"],
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
            iat, exp = _integer(claims["iat"]), _integer(claims["exp"])
            now = self.clock()
            skew = self.config.clock_skew
            if (
                not 0 < exp - iat <= self.config.max_token_lifetime
                or iat > now + skew
                or exp <= now - skew
                or claims.get(self.config.token_use_claim) != self.config.token_use
            ):
                raise AuthenticationError()
            if "nbf" in claims and (
                _integer(claims["nbf"]) > now + skew or _integer(claims["nbf"]) >= exp
            ):
                raise AuthenticationError()
            scopes: object = claims.get(self.config.scope_claim, "")
            if isinstance(scopes, str):
                scopes = scopes.split()
            return Principal(
                self.config.issuer,
                _text(claims["sub"]),
                _strings(scopes),
                _strings(claims.get(self.config.groups_claim, [])),
                iat,
                exp,
            )
        except ServiceError:
            raise
        except Exception:
            raise AuthenticationError() from None


class SigningKeyProvider(Protocol):
    def current(self) -> tuple[str, bytes]: ...

    def resolve(self, kid: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class EnvelopeBinding:
    gateway_id: str
    target_id: str
    tool_name: str
    mcp_id: str | int
    query: str

    def __post_init__(self) -> None:
        for value in (self.gateway_id, self.target_id, self.tool_name):
            _text(value)
        require_mcp_id(self.mcp_id)
        if not isinstance(self.query, str) or not self.query.strip() or len(self.query) > 2048:
            raise ValueError("invalid query")

    def claims(self) -> dict[str, object]:
        return {
            "gateway": self.gateway_id,
            "target": self.target_id,
            "tool": self.tool_name,
            # Canonical JSON keeps signed integers distinct from signed strings.
            # Verification permits only canonical int-to-string context loss.
            "mcp_id": self.mcp_id,
            "query_hash": hashlib.sha256(self.query.encode("utf-8")).hexdigest(),
        }


@dataclass(frozen=True, slots=True)
class VerifiedEnvelope:
    principal: Principal
    policy_epoch: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


class EnvelopeCodec:
    def __init__(
        self,
        keys: SigningKeyProvider,
        *,
        clock: Callable[[], float] = time.time,
        ttl: int = 30,
        clock_skew: int = 30,
    ) -> None:
        if (
            type(ttl) is not int
            or type(clock_skew) is not int
            or not 1 <= ttl <= 30
            or not 0 <= clock_skew <= 30
        ):
            raise ValueError("invalid envelope lifetime")
        self.keys, self.clock, self.ttl, self.clock_skew = keys, clock, ttl, clock_skew

    @timed("auth.envelope")
    def issue(self, principal: Principal, binding: EnvelopeBinding, *, policy_epoch: int) -> str:
        try:
            now = int(self.clock())
            if principal.expires_at <= now or principal.issued_at > now + self.clock_skew:
                raise AuthenticationError()
            kid, key = self.keys.current()
            if len(key) < 32:
                raise AuthenticationError()
            header = {"alg": "HS256", "typ": "SEMSEARCH", "kid": _text(kid, 256)}
            body: dict[str, object] = {
                "v": 1,
                "iss": principal.issuer,
                "sub": principal.subject,
                "scopes": sorted(principal.scopes),
                "groups": sorted(principal.groups),
                "principal_iat": principal.issued_at,
                "principal_exp": principal.expires_at,
                "iat": now,
                "exp": min(now + self.ttl, principal.expires_at),
                "epoch": _integer(policy_epoch),
                "nonce": secrets.token_urlsafe(16),
                **binding.claims(),
            }
            signed = _b64(canonical_json(header)) + "." + _b64(canonical_json(body))
            result = signed + "." + _b64(hmac.digest(key, signed.encode("ascii"), "sha256"))
            return _text(result, 8192)
        except Exception:
            raise AuthenticationError() from None

    @timed("auth.envelope")
    def verify(self, token: object, binding: EnvelopeBinding) -> VerifiedEnvelope:
        try:
            encoded = _text(token, 8192)
            header_text, body_text, signature = encoded.split(".")
            header = json.loads(_unb64(header_text))
            if not isinstance(header, dict) or set(header) != {"alg", "typ", "kid"}:
                raise AuthenticationError()
            if header["alg"] != "HS256" or header["typ"] != "SEMSEARCH":
                raise AuthenticationError()
            key = self.keys.resolve(_text(header["kid"], 256))
            if len(key) < 32:
                raise AuthenticationError()
            expected = hmac.digest(key, f"{header_text}.{body_text}".encode("ascii"), "sha256")
            if not hmac.compare_digest(expected, _unb64(signature)):
                raise AuthenticationError()
            body = json.loads(_unb64(body_text))
            if not isinstance(body, dict) or body.get("v") != 1:
                raise AuthenticationError()
            now = self.clock()
            iat, exp = _integer(body["iat"]), _integer(body["exp"])
            if (
                not 0 < exp - iat <= self.ttl
                or iat > now + self.clock_skew
                or exp <= now - self.clock_skew
            ):
                raise AuthenticationError()
            signed_id = require_mcp_id(body.get("mcp_id"))
            coerced = (
                type(signed_id) is int
                and type(binding.mcp_id) is str
                and str(signed_id) == binding.mcp_id
            )
            for name, value in binding.claims().items():
                if name == "mcp_id" and coerced:
                    continue
                if type(body.get(name)) is not type(value) or body[name] != value:
                    raise AuthenticationError()
            _text(body["nonce"], 256)
            principal = Principal(
                _text(body["iss"]),
                _text(body["sub"]),
                _strings(body["scopes"]),
                _strings(body["groups"]),
                _integer(body["principal_iat"]),
                _integer(body["principal_exp"]),
            )
            if principal.expires_at < exp or principal.issued_at > iat + self.clock_skew:
                raise AuthenticationError()
            verified = VerifiedEnvelope(principal, _integer(body["epoch"]))
            # Emit only after every claim passes; never log the token or IDs.
            diagnostics = _BINDING_DIAGNOSTICS.get()
            if coerced and diagnostics is not None:
                diagnostics["binding_match"] = "mcp_id_str_coerced"
            return verified
        except AuthenticationError:
            raise
        except Exception:
            raise AuthenticationError() from None


@dataclass(frozen=True, slots=True)
class AuthorizationView:
    control: ControlRecord
    allowed_ids: frozenset[str]
    tools_by_name: Mapping[str, ToolIdentity]
    schema_digests: Mapping[str, str]
    query_egress_allowed: bool


class Authorizer(Protocol):
    def read(self, principal: Principal, gateway: GatewayIdentity) -> AuthorizationView: ...


class AuthorizationStore(Protocol):
    def read_control(self) -> ControlRecord: ...

    def load_policy(self, control: ControlRecord) -> dict[str, object]: ...

    def load_safety(self, control: ControlRecord) -> dict[str, str]: ...


def _identity(key: object, gateway: GatewayIdentity) -> ToolIdentity:
    parts: object = json.loads(_text(key, 8192))
    if not isinstance(parts, list) or len(parts) != 6:
        raise ValueError("invalid policy identity")
    values = [_text(value) for value in parts]
    identity = ToolIdentity(GatewayIdentity(*values[:3]), *values[3:])
    if identity.gateway != gateway or identity.key != key:
        raise ValueError("policy identity outside gateway")
    return identity


class DDBAuthorizer:
    """Read an immutable policy only through its strongly consistent control pointer."""

    def __init__(self, store: AuthorizationStore) -> None:
        self.store = store

    @timed("auth.authorize")
    def read(self, principal: Principal, gateway: GatewayIdentity) -> AuthorizationView:
        try:
            control = self.store.read_control()
            if control.gateway != gateway:
                raise ServiceError()
            if control.tombstoned:
                raise AuthorizationError()
            policy = self.store.load_policy(control)
            # Before the first catalog publication, a valid machine principal
            # needs to list the catalog that will create the safety snapshot.
            # An empty safety view grants no tool calls or ordinary bootstrap.
            safety = {} if control.safety is None else self.store.load_safety(control)
            if set(policy) != {"version", "epoch", "principals", "tools"}:
                raise ServiceError()
            if policy["version"] != 1 or _integer(policy["epoch"]) != control.policy_epoch:
                raise ServiceError()
            raw_tools, entries = policy["tools"], policy["principals"]
            if not isinstance(raw_tools, dict) or not isinstance(entries, list):
                raise ServiceError()
            tools: dict[str, ToolIdentity] = {}
            approved_schemas: dict[str, str] = {}
            for name, registration in raw_tools.items():
                if not isinstance(registration, dict) or set(registration) != {
                    "id",
                    "schema_digest",
                }:
                    raise ServiceError()
                identity = _identity(registration["id"], gateway)
                if name != identity.full_name:
                    raise ServiceError()
                approved_digest = _text(registration["schema_digest"])
                if len(approved_digest) != 64 or any(
                    char not in "0123456789abcdef" for char in approved_digest
                ):
                    raise ServiceError()
                tools[identity.full_name] = identity
                approved_schemas[identity.key] = approved_digest
            allowed: frozenset[str] = frozenset()
            egress = False
            matched = False
            known_keys = {tool.key for tool in tools.values()}
            seen: set[tuple[str, str]] = set()
            for raw in entries:
                if not isinstance(raw, dict) or set(raw) != {
                    "issuer",
                    "subject",
                    "scopes",
                    "groups",
                    "allowed_tools",
                    "allow_query_egress",
                    "not_before",
                    "disabled",
                }:
                    raise ServiceError()
                identifier = (_text(raw["issuer"]), _text(raw["subject"]))
                if identifier in seen:
                    raise ServiceError()
                seen.add(identifier)
                scopes, groups = _strings(raw["scopes"]), _strings(raw["groups"])
                candidate = _strings(raw["allowed_tools"], maximum=20000)
                if not candidate.issubset(known_keys):
                    raise ServiceError()
                not_before = _integer(raw["not_before"])
                if type(raw["disabled"]) is not bool or type(raw["allow_query_egress"]) is not bool:
                    raise ServiceError()
                if identifier != (principal.issuer, principal.subject):
                    continue
                matched = True
                if (
                    raw["disabled"]
                    or principal.issued_at < not_before
                    or not scopes.issubset(principal.scopes)
                    or not groups.issubset(principal.groups)
                ):
                    raise AuthorizationError()
                allowed = frozenset(
                    key for key in candidate if safety.get(key) == approved_schemas[key]
                )
                egress = raw["allow_query_egress"]
            if not matched:
                raise AuthorizationError()
            return AuthorizationView(control, allowed, tools, safety, egress)
        except ServiceError:
            raise
        except Exception:
            raise ServiceError() from None


class CounterClient(Protocol):
    def update_item(self, **kwargs: object) -> Mapping[str, object]: ...


class Limiter(Protocol):
    def admit(
        self, principal: Principal, gateway: GatewayIdentity, *, machine: bool = False
    ) -> None: ...


class FixedWindowLimiter:
    def __init__(
        self,
        client: CounterClient,
        table_name: str,
        *,
        ordinary_limit: int = 600,
        machine_limit: int = 600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        _text(table_name)
        if (
            type(ordinary_limit) is not int
            or type(machine_limit) is not int
            or min(ordinary_limit, machine_limit) < 1
        ):
            raise ValueError("invalid rate limit")
        self.client, self.table_name = client, table_name
        self.ordinary_limit, self.machine_limit, self.clock = ordinary_limit, machine_limit, clock

    @timed("auth.rate_limit")
    def admit(
        self, principal: Principal, gateway: GatewayIdentity, *, machine: bool = False
    ) -> None:
        now = int(self.clock())
        minute = now // 60
        namespace = hashlib.sha256(
            canonical_json(
                [
                    gateway.as_dict(),
                    principal.issuer,
                    principal.subject,
                ]
            )
        ).hexdigest()
        try:
            self.client.update_item(
                TableName=self.table_name,
                Key={"pk": {"S": f"rate/{namespace}/{minute}"}},
                UpdateExpression="SET #ttl = if_not_exists(#ttl, :ttl) ADD #count :one",
                ConditionExpression="attribute_not_exists(#count) OR #count < :limit",
                ExpressionAttributeNames={"#count": "count", "#ttl": "expires_at"},
                ExpressionAttributeValues={
                    ":ttl": {"N": str((minute + 2) * 60)},
                    ":one": {"N": "1"},
                    ":limit": {"N": str(self.machine_limit if machine else self.ordinary_limit)},
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise RateLimitError(60 - now % 60) from None
            raise ServiceError() from None
        except Exception:
            raise ServiceError() from None

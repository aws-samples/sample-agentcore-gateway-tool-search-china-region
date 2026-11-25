"""Single Gateway target with bounded ECS-first routing and one Lambda fallback."""

from __future__ import annotations

import json
import math
import os
import signal
import socket
import ssl
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from functools import lru_cache
from typing import Protocol, cast
from urllib.parse import urlsplit

from semantic_service._mcp_id import correlation_id
from semantic_service.route_budget import (
    ECS_SECONDS,
    FALLBACK_RESERVE_SECONDS,
    FALLBACK_SECONDS,
    MIN_ATTEMPT_SECONDS,
    PLATFORM_RESERVE_SECONDS,
    ROUTE_SECONDS,
)
from semantic_service.transport import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    InvalidEnvelope,
    RouteIdentity,
    arguments,
    encode_json,
    strict_json,
)

_RETRY_REASONS = frozenset(
    {
        "transport_unavailable",
        "connect_error",
        "read_error",
        "write_error",
        "protocol_error",
        "close_error",
        "connect_timeout",
        "read_timeout",
        "write_timeout",
        "pool_timeout",
        "transport_timeout",
        "deadline_exceeded",
        "capacity",
        "replay_unavailable",
        "clock_skew",
        "worker_unavailable",
    }
)


class TransportUnavailable(Exception):
    """A code-owned infrastructure reason; never include remote exception text."""

    def __init__(self, reason: str = "transport_unavailable") -> None:
        if reason not in _RETRY_REASONS:
            raise ValueError("unknown transport reason")
        self.reason = reason
        super().__init__(reason)


class DeadlineExpired(BaseException):
    """Escape broad dependency handlers; the facade maps this to a fixed error."""


class ServiceError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("internal failure")


def initialization_error_type(error: BaseException) -> str:
    name = type(error).__name__
    return name if len(name) <= 128 and name.isascii() and name.isidentifier() else "unknown"


class _InitializationError(ServiceError):
    def __init__(self, timings: dict[str, dict[str, float | int]], cause: BaseException) -> None:
        super().__init__()
        self.initialization_timings = timings
        self.exception_type = initialization_error_type(cause)


class AuthenticationError(ServiceError):
    def __init__(self) -> None:
        RuntimeError.__init__(self, "unauthenticated")


class AuthorizationError(ServiceError):
    def __init__(self) -> None:
        RuntimeError.__init__(self, "unauthorized")


class InputError(ServiceError):
    def __init__(self) -> None:
        RuntimeError.__init__(self, "invalid input")


class RateLimitError(ServiceError):
    def __init__(self) -> None:
        RuntimeError.__init__(self, "rate limited")


class IndexNotReadyError(ServiceError):
    def __init__(self) -> None:
        RuntimeError.__init__(self, "index not ready")


def _public_error(kind: str) -> ServiceError:
    error_type = {
        "unauthenticated": AuthenticationError,
        "replay rejected": AuthenticationError,
        "unauthorized": AuthorizationError,
        "invalid input": InputError,
        "rate limited": RateLimitError,
        "index not ready": IndexNotReadyError,
    }.get(kind, ServiceError)
    return error_type()


@contextmanager
def enforce_deadline(end: float) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        raise ServiceError()
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ServiceError()
    remaining = end - time.monotonic()
    if remaining <= 0:
        raise DeadlineExpired()
    previous = signal.getsignal(signal.SIGALRM)
    active = True

    def expire(signum: int, frame: object) -> None:
        if active:
            raise DeadlineExpired()

    signal.signal(signal.SIGALRM, expire)
    try:
        try:
            signal.setitimer(signal.ITIMER_REAL, remaining)
            yield
        finally:
            # A delivery at this first cleanup instruction can raise, but the
            # enclosing finally still restores the process-wide signal state.
            active = False
    finally:
        # A completed attempt must not be discarded by a signal delivered while
        # disarming. Block and consume a pending alarm before restoring SIG_DFL
        # (or the caller's handler), and always restore both handler and mask.
        active = False
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        try:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
                if signal.SIGALRM in signal.sigpending():
                    signal.sigwait({signal.SIGALRM})
            finally:
                signal.signal(signal.SIGALRM, previous)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def check_response(value: object) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] != 1
        or type(value.get("ok")) is not bool
    ):
        raise ServiceError()
    if value["ok"]:
        result = value.get("result")
        if (
            set(value) != {"version", "ok", "result"}
            or not isinstance(result, dict)
            or set(result) != {"tools"}
            or not isinstance(result["tools"], list)
            or any(not isinstance(tool, dict) for tool in result["tools"])
        ):
            raise ServiceError()
        return cast(dict[str, object], result)
    if (
        set(value) != {"version", "ok", "error", "retryable"}
        or type(value.get("retryable")) is not bool
        or not isinstance(value.get("error"), str)
    ):
        raise ServiceError()
    if value["retryable"]:
        retryable = {
            "capacity": "capacity",
            "deadline exceeded": "deadline_exceeded",
            "replay unavailable": "replay_unavailable",
            "clock skew": "clock_skew",
            "worker unavailable": "worker_unavailable",
        }
        reason = retryable.get(value["error"])
        if reason is None:
            raise ServiceError()
        raise TransportUnavailable(reason)
    raise _public_error(value["error"])


class BackendCall(Protocol):
    def __call__(self, body: dict[str, object], deadline: float) -> dict[str, object]: ...


class LambdaContext(Protocol):
    def get_remaining_time_in_millis(self) -> float: ...


class InvokeClient(Protocol):
    def invoke(self, **kwargs: object) -> dict[str, object]: ...


def emit_record(record: dict[str, object]) -> None:
    print(json.dumps(record, separators=(",", ":")), flush=True)


def platform_request_id(context: object) -> str | None:
    value = getattr(context, "aws_request_id", None)
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


class Router:
    def __init__(
        self,
        identity: RouteIdentity,
        primary: BackendCall,
        fallback: BackendCall,
        *,
        emit: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.identity, self.primary, self.fallback = identity, primary, fallback
        self.emit = emit or emit_record
        self.initialization_timings: dict[str, dict[str, float | int]] = {}

    def _attempt(
        self,
        call: BackendCall,
        body: dict[str, object],
        deadline: float,
        side: str,
        platform_id: str | None,
        correlation: str | None,
    ) -> dict[str, object]:
        started = time.monotonic()
        status = "internal failure"

        def emit(phase: str) -> None:
            self.emit(
                {
                    "operation": "search_attempt",
                    "phase": phase,
                    "backend": side,
                    "attempt_id": body["attempt_id"],
                    "aws_request_id": platform_id,
                    "correlation_id": correlation,
                    "timestamp_ms": int(time.time() * 1000),
                    "remaining_ms": round(max(0.0, deadline - time.monotonic()) * 1000, 3),
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    "status": status,
                }
            )

        status = "started"
        emit("start")
        result: dict[str, object] | None = None
        rejection: ServiceError | TransportUnavailable | None = None
        try:
            with enforce_deadline(deadline):
                try:
                    result = check_response(call(body, deadline))
                except (ServiceError, TransportUnavailable) as error:
                    rejection = error
                    raise
            status = "ok"
            return result
        except TransportUnavailable as error:
            status = error.reason
            raise
        except DeadlineExpired:
            if rejection is not None:
                status = str(rejection)
                raise rejection from None
            # Only an already-validated response can survive a cleanup-time
            # signal. A timeout while executing/parsing has no result to keep.
            if result is not None:
                status = "ok"
                return result
            status = "deadline_exceeded"
            raise
        except ServiceError as error:
            status = str(error)
            raise
        except Exception:
            status = "internal failure"
            raise
        finally:
            emit("end")

    def __call__(
        self,
        event: object,
        context: object,
        *,
        cold_start: bool = False,
        factory_ms: float = 0.0,
        invocation_started: float | None = None,
    ) -> dict[str, object]:
        started = invocation_started if invocation_started is not None else time.monotonic()
        side, reason, outcome, status = "none", "none", "error", "internal failure"
        stage = "admission"
        correlation: str | None = None
        ecs_attempt: object = None
        attempt: object = None
        end = started
        fallback_started = False
        fallback_not_started = "none"
        primary_not_started = "none"
        fallback_reserve_satisfied = False
        fallback_budget_ms = 0.0
        primary_budget_ms = 0.0
        platform_id = platform_request_id(context)
        try:
            try:
                args = arguments(event)
            except InvalidEnvelope:
                raise AuthenticationError() from None
            except ValueError:
                raise InputError() from None
            try:
                mcp_id = self.identity.validate_custom(
                    getattr(getattr(context, "client_context", None), "custom", None)
                )
            except ValueError:
                raise AuthenticationError() from None
            correlation = correlation_id(mcp_id)
            try:
                remaining = cast(LambdaContext, context).get_remaining_time_in_millis()
                if type(remaining) not in (int, float) or not math.isfinite(remaining):
                    raise ValueError()
                end = min(
                    started + ROUTE_SECONDS,
                    time.monotonic() + remaining / 1000 - PLATFORM_RESERVE_SECONDS,
                )
                if end - time.monotonic() < MIN_ATTEMPT_SECONDS:
                    raise ValueError()
            except Exception:
                raise ServiceError() from None
            epoch_end_ms = int(time.time() * 1000 + max(0, end - time.monotonic()) * 1000)
            ecs_end = min(end - FALLBACK_RESERVE_SECONDS, time.monotonic() + ECS_SECONDS)
            body: dict[str, object] = {
                "version": 1,
                "arguments": args,
                "custom": self.identity.context(mcp_id).client_context.custom,
                "deadline_ms": min(
                    epoch_end_ms,
                    int(time.time() * 1000 + max(0.0, ecs_end - time.monotonic()) * 1000),
                ),
                "attempt_id": uuid.uuid4().hex,
            }
            if len(encode_json(body)) > MAX_REQUEST_BYTES:
                raise InputError()
            response: dict[str, object] | None = None
            primary_budget_ms = max(0.0, ecs_end - time.monotonic()) * 1000
            if primary_budget_ms >= MIN_ATTEMPT_SECONDS * 1000:
                ecs_attempt = attempt = body["attempt_id"]
                side, stage = "ecs", "attempt"
                try:
                    response = self._attempt(
                        self.primary, body, ecs_end, "ecs", platform_id, correlation
                    )
                except (TransportUnavailable, DeadlineExpired) as error:
                    reason = (
                        error.reason
                        if isinstance(error, TransportUnavailable)
                        else "deadline_exceeded"
                    )
            else:
                primary_not_started = reason = "fallback_reserve"
            if response is None:
                if end - time.monotonic() < MIN_ATTEMPT_SECONDS:
                    fallback_not_started = "insufficient_budget"
                    raise ServiceError() from None
                fallback_started_at = time.monotonic()
                fallback_end = min(end, fallback_started_at + FALLBACK_SECONDS)
                fallback_budget_ms = max(0.0, fallback_end - fallback_started_at) * 1000
                fallback_reserve_satisfied = fallback_budget_ms >= FALLBACK_RESERVE_SECONDS * 1000
                body = {
                    **body,
                    "deadline_ms": min(
                        epoch_end_ms,
                        int(time.time() * 1000 + fallback_budget_ms),
                    ),
                    "attempt_id": uuid.uuid4().hex,
                }
                side, stage, fallback_started, attempt = (
                    "fallback",
                    "attempt",
                    True,
                    body["attempt_id"],
                )
                response = self._attempt(
                    self.fallback, body, fallback_end, "fallback", platform_id, correlation
                )
            outcome, status = "ok", "ok"
            return response
        except (DeadlineExpired, TransportUnavailable):
            raise ServiceError() from None
        except ServiceError as error:
            status = str(error)
            raise
        except Exception:
            raise ServiceError() from None
        finally:
            self.emit(
                {
                    "operation": "search_route",
                    "phase": "end",
                    "backend": side,
                    "outcome": outcome,
                    "status": status,
                    "failure_stage": stage if outcome != "ok" else "none",
                    "fallback_reason": reason,
                    "fallback_started": fallback_started,
                    "fallback_not_started": fallback_not_started,
                    "primary_not_started": primary_not_started,
                    "primary_budget_ms": round(primary_budget_ms, 3),
                    "fallback_budget_ms": round(fallback_budget_ms, 3),
                    "fallback_reserve_ms": FALLBACK_RESERVE_SECONDS * 1000,
                    "fallback_reserve_satisfied": fallback_reserve_satisfied,
                    "correlation_id": correlation,
                    "aws_request_id": platform_id,
                    "ecs_attempt_id": ecs_attempt,
                    "attempt_id": attempt,
                    "timestamp_ms": int(time.time() * 1000),
                    "remaining_ms": round(max(0.0, end - time.monotonic()) * 1000, 3),
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    "factory_ms": round(factory_ms, 3),
                    "factory_ms_scope": "invocation",
                    "cold_start": cold_start,
                }
            )


def _has_tls_cause(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _exchange_failure(
    error: BaseException, *, rejected: bool, server_failed: bool, deadline: bool = False
) -> tuple[str, str | None]:
    """Classify typed causes, preserving explicit rejection over compensating I/O."""
    import httpx
    from botocore import exceptions as sdk_errors

    from semantic_service.catalog import _RefreshUnavailable

    pending: list[BaseException] = [error]
    causes: list[BaseException] = []
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if len(seen) > 32:
            return "oauth_failed", None
        causes.append(current)
        pending.extend(
            item for item in (current.__cause__, current.__context__) if item is not None
        )
    if any(isinstance(cause, (ssl.SSLError, sdk_errors.SSLError)) for cause in causes):
        return "oauth_tls_failed", None
    if any(isinstance(cause, _RefreshUnavailable) for cause in causes):
        # The real token source raises this when the local refresh budget prevents
        # a second exchange. The first credential may be cached or freshly read;
        # this signal does not prove that a second exchange was rejected.
        return "oauth_refresh_unavailable", "transport_unavailable"
    if rejected:
        return "oauth_rejected", None
    if server_failed:
        # An HTTP server error is a received response, not proof of transport
        # failure. Keep this policy separate from NAT/DNS/connect timeouts.
        return "oauth_server_failed", None
    service_errors = [cause for cause in causes if isinstance(cause, sdk_errors.ClientError)]
    if service_errors:
        # Only transient infrastructure responses from the configured secret
        # read authorize the IAM fallback. Permission/configuration rejections
        # stay closed even if a prior transport error is also in the chain.
        if all(_transient_secret_error(cause) for cause in service_errors):
            return "oauth_secret_unavailable", "transport_unavailable"
        return "oauth_failed", None
    if deadline:
        return "deadline_exceeded", "deadline_exceeded"
    categories = (
        ((httpx.ConnectTimeout, sdk_errors.ConnectTimeoutError), "connect_timeout"),
        ((httpx.ReadTimeout, sdk_errors.ReadTimeoutError), "read_timeout"),
        ((httpx.WriteTimeout,), "write_timeout"),
        ((httpx.PoolTimeout,), "pool_timeout"),
        ((httpx.TimeoutException, TimeoutError), "transport_timeout"),
        (
            (httpx.ConnectError, sdk_errors.EndpointConnectionError, socket.gaierror),
            "connect_error",
        ),
        ((httpx.ReadError, sdk_errors.ConnectionClosedError, ConnectionResetError), "read_error"),
        ((httpx.WriteError,), "write_error"),
        ((httpx.CloseError,), "close_error"),
        ((httpx.ProtocolError,), "protocol_error"),
        (
            (httpx.NetworkError, sdk_errors.HTTPClientError, ConnectionError),
            "transport_unavailable",
        ),
    )
    for types, reason in categories:
        if any(isinstance(cause, types) for cause in causes):
            return "oauth_transport_failed", reason
    return "oauth_failed", None


def _transient_secret_error(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    if getattr(error, "operation_name", None) != "GetSecretValue" or not isinstance(response, dict):
        return False
    detail, metadata = response.get("Error"), response.get("ResponseMetadata", {})
    if not isinstance(detail, dict) or not isinstance(metadata, dict):
        return False
    code, status = detail.get("Code"), metadata.get("HTTPStatusCode")
    if code in {
        "AccessDenied",
        "AccessDeniedException",
        "DecryptionFailure",
        "InvalidClientTokenId",
        "InvalidParameterException",
        "InvalidRequestException",
        "KMSAccessDeniedException",
        "ResourceNotFoundException",
        "UnrecognizedClientException",
    }:
        return False
    return code in {
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
        "InternalServiceError",
        "InternalServiceErrorException",
        "InternalFailure",
        "ServiceUnavailable",
        "ServiceUnavailableException",
    } or (type(status) is int and (status == 429 or 500 <= status <= 599))


class HTTPBackend:
    def __init__(
        self,
        url: str,
        client: object,
        tokens: object,
        *,
        emit: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port not in (None, 443)
            or parsed.path != "/search"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("fixed HTTPS search endpoint required")
        self.url, self.client, self.tokens = url, client, tokens
        self.emit = emit or emit_record

    def __call__(self, body: dict[str, object], deadline: float) -> dict[str, object]:
        import httpx

        from semantic_core._embedding_telemetry import AttemptTrace
        from semantic_core.providers import RequestContext
        from semantic_service.catalog import OAuthTokenSource, _CatalogDeadlineExceeded
        from semantic_service.route_telemetry import Timings, collect

        client = cast(httpx.Client, self.client)
        tokens = cast(OAuthTokenSource, self.tokens)
        oauth_started = time.monotonic()
        # These diagnostics contain only code-owned labels and numeric counters.
        exchange_status = "started"
        exchange_timings: dict[str, dict[str, float | int]] = {}
        oauth_rejected = False
        oauth_server_failed = False
        transport_reason: str | None = None
        measured: Timings | None = None

        def oauth(phase: str) -> None:
            self.emit(
                {
                    "operation": "search_oauth",
                    "phase": phase,
                    "attempt_id": body["attempt_id"],
                    "backend": "ecs",
                    "timestamp_ms": int(time.time() * 1000),
                    "duration_ms": round((time.monotonic() - oauth_started) * 1000, 3),
                    "remaining_ms": round(max(0.0, deadline - time.monotonic()) * 1000, 3),
                    "status": exchange_status,
                    "stage_timings": exchange_timings,
                    "transport_reason": transport_reason,
                }
            )

        # Secret/cache and exchange work stays within this ECS attempt. An actual
        # deadline is a budget failure; authentication/TLS rejection stays closed.
        oauth("start")
        try:
            try:
                with collect() as measured:
                    try:
                        token = tokens.token(
                            RequestContext(cast(str, body["attempt_id"]), deadline)
                        )
                    finally:
                        exchange_timings = measured.snapshot()
                        oauth_rejected = measured.oauth_rejected
                        oauth_server_failed = measured.oauth_server_failed
                if time.monotonic() >= deadline:
                    raise DeadlineExpired()
                exchange_status = "ok"
            except (DeadlineExpired, _CatalogDeadlineExceeded) as error:
                # A diagnostic/cleanup deadline may mask an earlier rejection or
                # TLS error in its context; it must not authorize fallback.
                exchange_status, transport_reason = _exchange_failure(
                    error,
                    rejected=oauth_rejected or (measured is not None and measured.oauth_rejected),
                    server_failed=(
                        oauth_server_failed
                        or (measured is not None and measured.oauth_server_failed)
                    ),
                    deadline=True,
                )
                if transport_reason is None:
                    raise ServiceError() from None
                raise TransportUnavailable(transport_reason) from None
            except Exception as error:
                exchange_status, transport_reason = _exchange_failure(
                    error,
                    rejected=oauth_rejected or (measured is not None and measured.oauth_rejected),
                    server_failed=(
                        oauth_server_failed
                        or (measured is not None and measured.oauth_server_failed)
                    ),
                )
                if transport_reason is not None:
                    raise TransportUnavailable(transport_reason) from None
                raise ServiceError() from None
            finally:
                oauth("end")
        except DeadlineExpired:
            # The terminal telemetry callback still runs inside the attempt
            # timer. Its late signal cannot replace a classified rejection or
            # overwrite the cause of an already classified transport failure.
            if exchange_status in {
                "oauth_rejected",
                "oauth_tls_failed",
                "oauth_server_failed",
                "oauth_failed",
            }:
                raise ServiceError() from None
            if transport_reason is not None:
                raise TransportUnavailable(transport_reason) from None
            raise
        effective_timeout = max(0.001, min(ECS_SECONDS, deadline - time.monotonic()))
        trace = AttemptTrace(1, effective_timeout)
        transport_status = "internal failure"
        completed: dict[str, object] | None = None
        rejection: ServiceError | None = None
        try:
            try:
                with client.stream(
                    "POST",
                    self.url,
                    content=encode_json(body),
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    timeout=effective_timeout,
                    follow_redirects=False,
                    extensions={"trace": trace.trace},
                ) as response:
                    try:
                        if response.status_code in {400, 401, 403, 409, 429}:
                            # Preserve an explicit non-retryable HTTP rejection
                            # even if its body is interrupted before validation.
                            rejection = ServiceError()
                        trace.headers_received(response.status_code)
                        trace.body_started()
                        data = bytearray()
                        for chunk in response.iter_bytes():
                            if chunk:
                                trace.body_chunk()
                            if time.monotonic() >= deadline:
                                raise TransportUnavailable("deadline_exceeded")
                            if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise ServiceError()
                            data.extend(chunk)
                        trace.body_finished(True)
                        value = strict_json(bytes(data))
                        if response.status_code not in (200, 400, 401, 403, 409, 429, 503):
                            raise ServiceError()
                        if (response.status_code == 200) != (value.get("ok") is True):
                            raise ServiceError()
                        check_response(value)
                        completed = value
                        transport_status = "ok"
                        return value
                    except ServiceError as error:
                        rejection = error
                        raise
                    except InvalidEnvelope:
                        rejection = ServiceError()
                        raise rejection from None
            except (httpx.NetworkError, httpx.ProtocolError) as error:
                if _has_tls_cause(error):
                    transport_status = "tls_failed"
                    rejection = ServiceError()
                    raise rejection from None
                reason = "protocol_error"
                for error_type, category in (
                    (httpx.ConnectError, "connect_error"),
                    (httpx.ReadError, "read_error"),
                    (httpx.WriteError, "write_error"),
                    (httpx.CloseError, "close_error"),
                ):
                    if isinstance(error, error_type):
                        reason = category
                        break
                transport_status = reason
                raise TransportUnavailable(reason) from None
            except httpx.TimeoutException as error:
                if _has_tls_cause(error):
                    transport_status = "tls_failed"
                    rejection = ServiceError()
                    raise rejection from None
                reason = "transport_timeout"
                for timeout_type, category in (
                    (httpx.ConnectTimeout, "connect_timeout"),
                    (httpx.ReadTimeout, "read_timeout"),
                    (httpx.WriteTimeout, "write_timeout"),
                    (httpx.PoolTimeout, "pool_timeout"),
                ):
                    if isinstance(error, timeout_type):
                        reason = category
                        break
                transport_status = reason
                raise TransportUnavailable(reason) from None
            except TransportUnavailable as error:
                transport_status = error.reason
                raise
            except DeadlineExpired:
                transport_status = "deadline_exceeded"
                raise
            except ServiceError as error:
                rejection = rejection or error
                raise
            finally:
                trace.finish()
                self.emit(
                    {
                        "operation": "search_transport",
                        "phase": "end",
                        "backend": "ecs",
                        "attempt_id": body["attempt_id"],
                        "timestamp_ms": int(time.time() * 1000),
                        "remaining_ms": round(max(0.0, deadline - time.monotonic()) * 1000, 3),
                        "status": transport_status,
                        "http_timing": asdict(trace.snapshot()),
                    }
                )
        except (DeadlineExpired, TransportUnavailable):
            # Cleanup cannot replace an already known rejection or replay a
            # fully parsed and validated success. Partial data has no marker.
            if rejection is not None:
                raise rejection from None
            if completed is not None:
                return completed
            raise


class LambdaBackend:
    def __init__(self, arn: str, client: object) -> None:
        from semantic_service.invoke_ownership import InvokeOwnership

        self.arn, self.client = arn, client
        self._ownership = InvokeOwnership(client)

    def __call__(self, body: dict[str, object], deadline: float) -> dict[str, object]:
        if deadline - time.monotonic() < MIN_ATTEMPT_SECONDS:
            raise ServiceError()
        completed: dict[str, object] | None = None
        rejection: ServiceError | TransportUnavailable | None = None
        try:
            with self._ownership.hold():
                response = self._invoke(body)
                # Establish a fully validated success before ownership cleanup.
                # A pending deadline delivered while unmasking may preserve only
                # this value, never partial bytes or a private failure response.
                try:
                    check_response(response)
                except (ServiceError, TransportUnavailable) as error:
                    rejection = error
                    raise
                completed = response
        except DeadlineExpired:
            if rejection is not None:
                raise rejection from None
            if completed is None:
                raise
        return completed

    def _invoke(self, body: dict[str, object]) -> dict[str, object]:
        response = cast(InvokeClient, self.client).invoke(
            FunctionName=self.arn, InvocationType="RequestResponse", Payload=encode_json(body)
        )
        # Botocore returns a bounded, closable StreamingBody at this boundary.
        stream = cast(StreamingPayload | None, response.get("Payload"))
        try:
            if response.get("StatusCode") != 200 or response.get("FunctionError") or stream is None:
                raise ServiceError()
            raw = stream.read(MAX_RESPONSE_BYTES + 1)
            if not isinstance(raw, bytes):
                raise ServiceError()
            return strict_json(raw)
        finally:
            if stream is not None:
                stream.close()


class StreamingPayload(Protocol):
    def read(self, amount: int) -> bytes: ...

    def close(self) -> None: ...


@lru_cache(maxsize=1)
def _router() -> Router:
    from semantic_service.route_telemetry import collect, stage

    with collect() as measured:
        try:
            with stage("handler.factory"):
                result = _build_router()
        except BaseException as error:
            raise _InitializationError(measured.snapshot(), error) from None
        result.initialization_timings = measured.snapshot()
        return result


def _build_router() -> Router:
    import re

    from semantic_service.route_telemetry import MeasuredSecrets, MeasuredTokenHTTP, stage

    with stage("runtime.import"):
        import httpx
        from botocore.config import Config
        from botocore.session import Session

        from semantic_service.catalog import OAuthTokenSource
        from semantic_service.secrets import SecretsCache

    region, account = os.environ["SEMSEARCH_REGION"], os.environ["SEMSEARCH_ACCOUNT"]
    arn = os.environ["SEMSEARCH_FALLBACK_ALIAS_ARN"]
    secret_id = os.environ["SEMSEARCH_ECS_OAUTH_SECRET_ID"]
    if region not in ("cn-north-1", "cn-northwest-1") or not re.fullmatch(r"\d{12}", account):
        raise ServiceError()
    if not re.fullmatch(
        rf"arn:aws-cn:lambda:{region}:{account}:function:[A-Za-z0-9_-]+:live", arn
    ) or not re.fullmatch(
        rf"arn:aws-cn:secretsmanager:{region}:{account}:secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{{6}}",
        secret_id,
    ):
        raise ServiceError()
    config = Config(
        connect_timeout=0.5,
        read_timeout=1.0,
        retries={"total_max_attempts": 1, "mode": "standard"},
    )
    with stage("runtime.aws_client"):
        # Match runtime._aws_session: shared credential/provider and local
        # endpoint/model-index warmup belongs to the session, not the first
        # service-specific bucket. No application secret/token is fetched here.
        with stage("runtime.aws_session"):
            session = Session()
            session.set_config_variable("region", region)
            session.get_credentials()
            session.get_available_partitions()
            loader = session.get_component("data_loader")
            loader.list_available_services("service-2")
            loader.list_available_services("endpoint-rule-set-1")
        with stage("runtime.aws_client.secretsmanager"):
            secrets = session.create_client("secretsmanager", region_name=region, config=config)
    with stage("runtime.aws_client"):
        with stage("runtime.aws_client.lambda"):
            worker = session.create_client(
                "lambda",
                region_name=region,
                config=Config(
                    connect_timeout=0.5,
                    read_timeout=FALLBACK_SECONDS,
                    retries={"total_max_attempts": 1, "mode": "standard"},
                ),
            )
    with stage("runtime.http_client"):
        http = httpx.Client(
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=4, max_keepalive_connections=2, keepalive_expiry=30
            ),
        )
    with stage("runtime.oauth_source"):
        tokens = OAuthTokenSource(
            SecretsCache(MeasuredSecrets(secrets), ttl_seconds=60),
            secret_id,
            os.environ["OIDC_TOKEN_ENDPOINT"],
            tuple(os.environ["OIDC_ALLOWED_HOSTS"].split(",")),
            (os.environ["OIDC_MACHINE_SCOPES"],),
            cast(httpx.Client, MeasuredTokenHTTP(http)),
            timeout_seconds=0.5,
        )
    return Router(
        RouteIdentity.from_env(),
        HTTPBackend(os.environ["SEMSEARCH_ECS_URL"], http, tokens),
        LambdaBackend(arn, worker),
    )


_COLD_START = True


def initialized_handler(route: Router, event: object, context: object) -> dict[str, object]:
    """Invoke an already-built route without redoing Lambda initialization."""
    global _COLD_START
    started = time.monotonic()
    cold_start, _COLD_START = _COLD_START, False
    return route(
        event,
        context,
        cold_start=cold_start,
        factory_ms=0.0,
        invocation_started=started,
    )


def handler(event: object, context: object) -> dict[str, object]:
    global _COLD_START
    started = time.monotonic()
    cold_start, _COLD_START = _COLD_START, False
    route: Router | None = None
    try:
        remaining = cast(LambdaContext, context).get_remaining_time_in_millis()
        if type(remaining) not in (int, float) or not math.isfinite(remaining):
            raise ServiceError()
        factory_end = min(
            started + ROUTE_SECONDS,
            time.monotonic() + remaining / 1000 - PLATFORM_RESERVE_SECONDS,
        )
        try:
            with enforce_deadline(factory_end):
                route = _router()
        except DeadlineExpired:
            if route is None:
                raise
    except (Exception, DeadlineExpired):
        emit_record(
            {
                "operation": "search_route",
                "phase": "end",
                "backend": "none",
                "outcome": "error",
                "status": "internal failure",
                "failure_stage": "factory",
                "fallback_reason": "none",
                "fallback_started": False,
                "fallback_not_started": "none",
                "correlation_id": None,
                "aws_request_id": platform_request_id(context),
                "ecs_attempt_id": None,
                "attempt_id": None,
                "timestamp_ms": int(time.time() * 1000),
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "factory_ms": round((time.monotonic() - started) * 1000, 3),
                "factory_ms_scope": "invocation",
                "cold_start": cold_start,
                "remaining_ms": 0.0,
            }
        )
        raise ServiceError() from None
    return route(
        event,
        context,
        cold_start=cold_start,
        factory_ms=(time.monotonic() - started) * 1000,
        invocation_started=started,
    )

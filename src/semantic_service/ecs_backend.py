"""Private Search backend admission shared by ECS and the Lambda fallback."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol, cast

from botocore.exceptions import ClientError

from semantic_core.catalog import canonical_json
from semantic_service._search_contract import validate_arguments
from semantic_service.auth import (
    AuthenticationError,
    EnvelopeBinding,
    EnvelopeCodec,
    InputError,
    OIDCConfig,
    OIDCVerifier,
    ServiceError,
    TokenVerifier,
)
from semantic_service.config import required
from semantic_service.ecs_logging import emit_record, exception_record
from semantic_service.route_budget import (
    CLOCK_SKEW_SECONDS,
    ECS_SECONDS,
    FALLBACK_SECONDS,
    RESULT_DELIVERY_SECONDS,
)
from semantic_service.transport import (
    MAX_RESPONSE_BYTES,
    RouteIdentity,
    decode_request,
    encode_json,
)

if TYPE_CHECKING:
    from semantic_core.snapshot import IndexSnapshot
    from semantic_service.search import SnapshotSource
    from semantic_service.storage import ControlRecord

_EXECUTION_END: ContextVar[float | None] = ContextVar("ecs_execution_end", default=None)
_SEARCH_COMPLETED: ContextVar[Callable[[], None] | None] = ContextVar(
    "ecs_search_completed", default=None
)
REPLAY_RETENTION_SECONDS = 120


class ReplayClient(Protocol):
    def put_item(self, **kwargs: object) -> Mapping[str, object]: ...


class ReplayRejected(ServiceError):
    message = "replay rejected"


class ReplayUnavailable(ServiceError):
    message = "replay unavailable"


class DeadlineExceeded(ServiceError):
    message = "deadline exceeded"


class ClockSkew(ServiceError):
    message = "clock skew"


@dataclass(frozen=True)
class Admission:
    request: dict[str, object]
    mcp_id: str
    backend: str
    deadline: int
    end: float
    platform_request_id: str | None = None


class _SynchronizedSnapshots:
    """Protect the existing Lambda reader's multi-field cache during ECS refresh."""

    def __init__(self, source: SnapshotSource) -> None:
        self.source = source
        self._lock = threading.Lock()

    def current(self, control: ControlRecord) -> IndexSnapshot:
        end = _EXECUTION_END.get()
        acquired = self._lock.acquire(
            timeout=max(0.0, end - time.monotonic()) if end is not None else 60.0
        )
        if not acquired:
            raise DeadlineExceeded()
        try:
            if end is not None and time.monotonic() >= end:
                raise DeadlineExceeded()
            return self.source.current(control)
        finally:
            self._lock.release()


def failure(kind: str, *, retryable: bool = False) -> dict[str, object]:
    """Only code-owned fixed kinds are passed here; never remote exception text."""
    return {"version": 1, "ok": False, "error": kind, "retryable": retryable}


class DDBReplayStore:
    """Consume a verified envelope once per backend using an atomic condition.

    Canonical decoded header/claims, rather than bearer bytes, prevent alternative
    base64 signature encodings from creating another admission key. The caller
    MUST complete signature and binding verification before consuming.
    """

    def __init__(self, client: ReplayClient, table: str) -> None:
        if not table or table != table.strip():
            raise ValueError("explicit replay table required")
        self.client, self.table = client, table

    def consume(self, envelope: str, backend: str) -> None:
        try:
            if backend not in {"ecs", "fallback"}:
                raise ValueError("invalid backend")
            header, body, _signature = envelope.split(".")
            decoded = [
                json.loads(
                    base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
                )
                for value in (header, body)
            ]
            if not all(isinstance(value, dict) for value in decoded):
                raise ValueError("invalid envelope")
            digest = hashlib.sha256(canonical_json(decoded)).hexdigest()
        except Exception:
            raise AuthenticationError() from None
        now = int(time.time())
        try:
            self.client.put_item(
                TableName=self.table,
                Item={
                    "pk": {"S": f"{backend}/{digest}"},
                    "expires_at": {"N": str(now + REPLAY_RETENTION_SECONDS)},
                },
                ConditionExpression="attribute_not_exists(pk) OR expires_at < :now",
                ExpressionAttributeValues={":now": {"N": str(now)}},
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ReplayRejected() from None
            raise ReplayUnavailable() from None
        except Exception:
            raise ReplayUnavailable() from None


class Backend:
    """Admit trusted transport then call the unchanged Search runtime."""

    def __init__(
        self,
        *,
        identity: RouteIdentity,
        envelope: EnvelopeCodec,
        replay: DDBReplayStore,
        search: Callable[[object, object], dict[str, object]],
        verifier: TokenVerifier | None = None,
        subject: str = "",
        scope: str = "",
    ) -> None:
        if verifier is not None and (
            not subject or not scope or len(scope.split()) != 1 or "," in scope
        ):
            raise ValueError("explicit service subject and scope required")
        self.identity, self.envelope, self.replay = identity, envelope, replay
        self.search, self.verifier = search, verifier
        self.subject, self.scope = subject, scope
        # OIDCVerifier owns a bounded mutable JWKS cache. Serialize its refreshes;
        # no request principal or envelope is retained on this long-lived object.
        self._verifier_lock = threading.Lock()

    def _authenticate(self, authorization: str | None, backend: str, end: float) -> None:
        if self.verifier is None:
            if backend == "fallback":
                return
            raise AuthenticationError()
        if (
            not isinstance(authorization, str)
            or not authorization.startswith("Bearer ")
            or len(authorization) > 16400
        ):
            raise AuthenticationError()
        token = authorization[7:]
        if not token or any(character.isspace() for character in token):
            raise AuthenticationError()
        if not self._verifier_lock.acquire(timeout=max(0.0, end - time.monotonic())):
            raise DeadlineExceeded()
        try:
            if time.monotonic() >= end:
                raise DeadlineExceeded()
            principal = self.verifier.verify(token)
        finally:
            self._verifier_lock.release()
        if principal.subject != self.subject or self.scope not in principal.scopes:
            raise AuthenticationError()

    @staticmethod
    def _deadline(deadline_ms: object, backend: str) -> int:
        now = int(time.time() * 1000)
        maximum = (
            (FALLBACK_SECONDS + CLOCK_SKEW_SECONDS) * 1000
            if backend == "fallback"
            else (ECS_SECONDS + CLOCK_SKEW_SECONDS) * 1000
        )
        if type(deadline_ms) is not int:
            raise InputError()
        if deadline_ms > now + maximum:
            raise ClockSkew()
        if deadline_ms <= now:
            raise DeadlineExceeded()
        if backend == "fallback":
            return min(deadline_ms, now + int(FALLBACK_SECONDS * 1000))
        return min(deadline_ms, now + int(ECS_SECONDS * 1000))

    @staticmethod
    def _remaining(deadline: int, end: float) -> None:
        if int(time.time() * 1000) >= deadline or time.monotonic() >= end:
            raise DeadlineExceeded()

    def admit(
        self,
        request: object,
        *,
        authorization: str | None = None,
        backend: str = "ecs",
        platform_request_id: str | None = None,
    ) -> Admission:
        """Authenticate before assigning Search work; no replay write or query here."""
        if backend not in {"ecs", "fallback"}:
            raise InputError()
        body = decode_request(encode_json(request))
        deadline = self._deadline(body["deadline_ms"], backend)
        # The incoming deadline is when the facade stops listening. Reserve the
        # worker's completed-result handoff *inside* that window; extending it
        # afterwards would make the facade rerun an already completed Search.
        delivery = RESULT_DELIVERY_SECONDS if backend == "ecs" else 0.0
        end = time.monotonic() + max(0.0, deadline / 1000 - time.time() - delivery)
        self._authenticate(authorization, backend, end)
        self._remaining(deadline, end)
        try:
            mcp_id = self.identity.validate_custom(body["custom"])
        except ValueError:
            raise AuthenticationError() from None
        arguments = validate_arguments(body["arguments"], require_envelope=True)
        binding = EnvelopeBinding(
            self.identity.gateway_id,
            self.identity.target_id,
            self.identity.tool_name,
            mcp_id,
            cast(str, arguments["query"]),
        )
        self.envelope.verify(arguments["_auth_context"], binding)
        self._remaining(deadline, end)
        return Admission(
            cast(dict[str, object], request), mcp_id, backend, deadline, end, platform_request_id
        )

    def execute(self, admitted: Admission) -> dict[str, object]:
        """Called only over the private parent/worker pipe after authenticated admission."""
        return self._observed(admitted=admitted)

    def handle(
        self,
        request: object,
        *,
        authorization: str | None = None,
        backend: str = "ecs",
        platform_request_id: str | None = None,
    ) -> dict[str, object]:
        return self._observed(
            request=request,
            authorization=authorization,
            backend=backend,
            platform_request_id=platform_request_id,
        )

    def _observed(
        self,
        *,
        admitted: Admission | None = None,
        request: object = None,
        authorization: str | None = None,
        backend: str = "ecs",
        platform_request_id: str | None = None,
    ) -> dict[str, object]:
        started = time.monotonic()
        outcome = "internal failure"
        search_started = False
        correlation: str | None = None
        attempt: str | None = None
        request_id = platform_request_id
        end: float | None = None
        if admitted is not None:
            backend = admitted.backend
            request_id = admitted.platform_request_id

        def emit(phase: str, status: str) -> None:
            print(
                json.dumps(
                    {
                        "operation": "search_backend",
                        "phase": phase,
                        "backend": backend if backend in {"ecs", "fallback"} else "invalid",
                        "correlation_id": correlation,
                        "attempt_id": attempt,
                        "aws_request_id": request_id,
                        "status": status,
                        "search_started": search_started,
                        "timestamp_ms": int(time.time() * 1000),
                        "remaining_ms": round(max(0.0, (end - time.monotonic()) * 1000), 3)
                        if end is not None
                        else None,
                        "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    }
                ),
                flush=True,
            )

        try:
            if admitted is None:
                admitted = self.admit(
                    request,
                    authorization=authorization,
                    backend=backend,
                    platform_request_id=platform_request_id,
                )
            body, end = admitted.request, admitted.end
            attempt = cast(str, body["attempt_id"])
            if backend == "ecs":
                request_id = str(uuid.UUID(hex=attempt))
            correlation = hashlib.sha256(admitted.mcp_id.encode("utf-8")).hexdigest()
            self._remaining(admitted.deadline, end)
            arguments = cast(dict[str, object], body["arguments"])
            self.replay.consume(cast(str, arguments["_auth_context"]), backend)
            self._remaining(admitted.deadline, end)
            context = self.identity.context(admitted.mcp_id)
            context.get_remaining_time_in_millis = lambda: max(
                0.0, (admitted.end - time.monotonic()) * 1000
            )
            context.aws_request_id = request_id
            search_started = True
            emit("start", "ok")
            token = _EXECUTION_END.set(end)
            try:
                result = self.search(arguments, context)
            finally:
                _EXECUTION_END.reset(token)
            # Search and its final authorization returned. Mark execution complete
            # before local validation/serialization; invalid output still fails
            # closed without authorizing a duplicate fallback.
            completed = _SEARCH_COMPLETED.get()
            if completed is not None:
                completed()
            response: dict[str, object] = {"version": 1, "ok": True, "result": result}
            if (
                set(result) != {"tools"}
                or not isinstance(result["tools"], list)
                or any(not isinstance(tool, dict) for tool in result["tools"])
                or len(encode_json(response)) > MAX_RESPONSE_BYTES
            ):
                raise ServiceError()
            outcome = "ok"
            return response
        except ServiceError as error:
            outcome = error.message
            return failure(
                error.message,
                retryable=isinstance(error, (DeadlineExceeded, ReplayUnavailable, ClockSkew)),
            )
        except Exception as error:
            emit_record(exception_record(error))
            return failure("internal failure")
        finally:
            emit("end", outcome)


@lru_cache(maxsize=3)
def backend_factory(backend: str) -> Backend:
    """Initialize once at ECS startup, lazily in the existing fallback function."""
    if backend not in {"ecs", "fallback", "admission"}:
        raise ValueError("unsupported backend")
    from semantic_service import runtime

    identity = RouteIdentity.from_env()
    # Reuse the existing cached SearchHandler, secret cache, provider adapters,
    # authorizer and final authorization rather than copying the retrieval path.
    if backend != "admission":
        search = runtime._search()
        if (
            search.search_tool.identity.gateway.gateway_id != identity.gateway_id
            or search.search_tool.identity.target_id != identity.target_id
            or search.search_tool.name != identity.tool_name
        ):
            raise ValueError("backend identity differs from configured Search identity")
        if backend == "ecs":
            if not isinstance(search.snapshots, _SynchronizedSnapshots):
                search.snapshots = _SynchronizedSnapshots(search.snapshots)
            search.snapshots.current(runtime._store().read_control())
    verifier = None
    subject, scope = "", ""
    if backend in {"ecs", "admission"}:
        verifier = OIDCVerifier(
            OIDCConfig(
                issuer=required(os.environ, "OIDC_ISSUER"),
                jwks_uri=required(os.environ, "OIDC_JWKS_URI"),
                audience=required(os.environ, "OIDC_AUDIENCE"),
                allowed_hosts=frozenset(required(os.environ, "OIDC_ALLOWED_HOSTS").split(",")),
                discovery_uri=required(os.environ, "OIDC_DISCOVERY_URI"),
                algorithms=tuple(os.environ.get("OIDC_ALGORITHMS", "RS256").split(",")),
                scope_claim=os.environ.get("OIDC_SCOPE_CLAIM", "scope"),
                groups_claim=os.environ.get("OIDC_GROUPS_CLAIM", "groups"),
                token_use_claim=os.environ.get("OIDC_TOKEN_USE_CLAIM", "token_use"),
                token_use=os.environ.get("OIDC_TOKEN_USE", "access"),
            ),
            runtime._http(),
        )
        subject = required(os.environ, "SEMSEARCH_ECS_OAUTH_SUBJECT")
        scope = required(os.environ, "SEMSEARCH_ECS_OAUTH_SCOPE")
    return Backend(
        identity=identity,
        envelope=runtime._envelope("search"),
        replay=DDBReplayStore(
            cast(ReplayClient, runtime._aws("dynamodb")),
            required(os.environ, "SEMSEARCH_REPLAY_TABLE"),
        ),
        search=runtime.search_handler,
        verifier=verifier,
        subject=subject,
        scope=scope,
    )

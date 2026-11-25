"""Gateway Lambda Target handler for the local search business contract."""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from inspect import getattr_static
from typing import TYPE_CHECKING, Protocol, cast

from semantic_core._timing import stage, timed
from semantic_core.catalog import ToolRecord, canonical_json
from semantic_core.providers import EmbeddingProvider, RequestContext, RerankProvider
from semantic_core.search import AuthorizationError as CoreAuthorizationError
from semantic_core.search import SearchConfig, SearchDiagnostics, SearchEngine, SearchMode
from semantic_core.snapshot import IndexSnapshot
from semantic_service._mcp_id import require_mcp_id
from semantic_service._search_contract import require_search, validate_arguments
from semantic_service.auth import (
    AuthenticationError,
    AuthorizationError,
    AuthorizationView,
    Authorizer,
    EnvelopeBinding,
    EnvelopeCodec,
    IndexNotReadyError,
    ServiceError,
)
from semantic_service.config import SEARCH_FINALIZATION_SECONDS, parse_search_config

if TYPE_CHECKING:
    from semantic_service.storage import ControlRecord

RESPONSE_LIMIT = 512 * 1024


class SnapshotSource(Protocol):
    def current(self, control: ControlRecord) -> IndexSnapshot: ...


def _provider_deadline(
    context: object, started: float, request_seconds: float
) -> tuple[float, bool]:
    """Use the application budget when an optional Lambda deadline is unavailable."""
    deadline = started + request_seconds
    name = "get_remaining_time_in_millis"
    try:
        remaining = getattr(context, name)
    except AttributeError:
        # A missing method is normal outside Lambda. A descriptor that raises
        # AttributeError is an invalid deadline source, not a missing method.
        try:
            getattr_static(context, name)
        except AttributeError:
            return deadline, False
        except Exception:
            return deadline, True
        return deadline, True
    except Exception:
        return deadline, True
    if not callable(remaining):
        return deadline, True
    try:
        milliseconds = remaining()
        if (
            type(milliseconds) not in (int, float)
            or not math.isfinite(milliseconds)
            or milliseconds < 0
        ):
            return deadline, True
        return min(deadline, started + milliseconds / 1000 - SEARCH_FINALIZATION_SECONDS), False
    except Exception:
        return deadline, True


class SearchHandler:
    """Signed identity and a cooperative budget with a Lambda authorization tail.

    Reserving time does not interrupt blocking work or replace final authorization.
    Only the Lambda deadline reserves 300 ms; the configured provider budget is intact.
    """

    def __init__(
        self,
        search_tool: ToolRecord,
        envelope: EnvelopeCodec,
        authorizer: Authorizer,
        snapshots: SnapshotSource,
        embedding: EmbeddingProvider | None = None,
        reranker: RerankProvider | None = None,
        *,
        config: SearchConfig | None = None,
        request_seconds: float = 3.0,
        observer: Callable[[SearchMode, bool], None] | None = None,
    ) -> None:
        if not search_tool.name.endswith("___search_tools"):
            raise ValueError("configured search tool has an invalid name")
        if (
            isinstance(request_seconds, bool)
            or not isinstance(request_seconds, (int, float))
            or not SEARCH_FINALIZATION_SECONDS < request_seconds <= 6
            or not math.isfinite(request_seconds)
        ):
            raise ValueError("invalid search deadline")
        self.search_tool, self.envelope, self.authorizer = search_tool, envelope, authorizer
        self.snapshots, self.embedding, self.reranker = snapshots, embedding, reranker
        self.config, self.request_seconds = config or parse_search_config({}), request_seconds
        self.observer = observer

    def __call__(
        self,
        event: object,
        context: object,
        *,
        diagnostics: Callable[[SearchDiagnostics], None] | None = None,
    ) -> dict[str, object]:
        # Invocation-local state remains isolated even when the handler is cached.
        lambda_deadline_ignored = False
        observed = SearchDiagnostics(
            configured_fusion=self.config.fusion_strategy,
            rerank_state="skipped" if self.config.rerank_enabled else "disabled",
            lambda_deadline_ignored=lambda_deadline_ignored,
        )

        def collect(value: SearchDiagnostics) -> None:
            nonlocal observed
            # A core empty result is not yet a successful Gateway response.
            observed = replace(
                value, empty_result=False, lambda_deadline_ignored=lambda_deadline_ignored
            )

        try:
            provider_deadline, lambda_deadline_ignored = _provider_deadline(
                context, time.monotonic(), self.request_seconds
            )
            observed = replace(observed, lambda_deadline_ignored=lambda_deadline_ignored)
            response = self._invoke(
                event, context, provider_deadline=provider_deadline, diagnostics=collect
            )
            observed = replace(observed, empty_result=not response["tools"])
            return response
        finally:
            if diagnostics is not None:
                try:
                    diagnostics(observed)
                # Optional diagnostics cannot change authorization or returned tools.
                except Exception:  # nosec B110
                    # Trusted diagnostics cannot change authorization or Gateway output.
                    pass

    def _invoke(
        self,
        event: object,
        context: object,
        *,
        provider_deadline: float,
        diagnostics: Callable[[SearchDiagnostics], None],
    ) -> dict[str, object]:
        arguments = validate_arguments(event, require_envelope=True)
        query = cast(str, arguments["query"])
        try:
            client_context = getattr(context, "client_context", None)
            custom: object = getattr(client_context, "custom", None)
            if (
                not isinstance(custom, Mapping)
                or custom.get("bedrockAgentCoreMessageVersion") != "1.0"
            ):
                raise AuthenticationError()
            if (
                custom.get("bedrockAgentCoreGatewayId")
                != self.search_tool.identity.gateway.gateway_id
                or custom.get("bedrockAgentCoreTargetId") != self.search_tool.identity.target_id
                or custom.get("bedrockAgentCoreToolName") != self.search_tool.name
            ):
                raise AuthenticationError()
            mcp_id = require_mcp_id(custom.get("bedrockAgentCoreMcpMessageId"))
            binding = EnvelopeBinding(
                self.search_tool.identity.gateway.gateway_id,
                self.search_tool.identity.target_id,
                self.search_tool.name,
                mcp_id,
                query,
            )
            verified = self.envelope.verify(arguments["_auth_context"], binding)
        except AuthenticationError:
            raise
        except Exception:
            raise AuthenticationError() from None

        principal = verified.principal

        @timed("search.authorize")
        def read() -> AuthorizationView:
            try:
                view = self.authorizer.read(principal, self.search_tool.identity.gateway)
                require_search(view, self.search_tool)
                if view.control.policy_epoch < verified.policy_epoch:
                    raise ServiceError()
                return view
            except ServiceError:
                raise
            except Exception:
                raise ServiceError() from None

        initial = read()
        try:
            snapshot = self.snapshots.current(initial.control)
            if snapshot.gateway != self.search_tool.identity.gateway:
                raise IndexNotReadyError()
        except Exception:
            raise IndexNotReadyError() from None

        def permitted(view: AuthorizationView) -> frozenset[str]:
            return frozenset(
                tool.tool_id
                for tool in snapshot.tools
                if tool.tool_id in view.allowed_ids
                and view.schema_digests.get(tool.tool_id) == tool.schema_digest
                and view.tools_by_name.get(tool.name) == tool.identity
                and tool.tool_id != self.search_tool.tool_id
            )

        # Re-read after content loading before any query goes to a provider.
        current = read()
        allowed = permitted(initial) & permitted(current)
        provider_allowed = initial.query_egress_allowed and current.query_egress_allowed
        authorization_fault = False

        def reauthorize() -> frozenset[str]:
            nonlocal authorization_fault
            try:
                latest = read()
            except AuthorizationError:
                raise
            except ServiceError:
                # The retrieval callback contract deliberately collapses failures to
                # CoreAuthorizationError. Keep only the classification here.
                authorization_fault = True
                raise
            # Revocation of provider egress cannot retroactively remove sent data,
            # but it must prevent subsequent rerank externalization and return.
            if provider_allowed and not latest.query_egress_allowed:
                raise AuthorizationError()
            return permitted(latest)

        try:
            result = SearchEngine(
                snapshot,
                self.embedding if provider_allowed else None,
                self.reranker if provider_allowed else None,
                self.config,
            ).search(
                query,
                allowed,
                RequestContext(
                    hashlib.sha256(canonical_json(binding.claims())).hexdigest(),
                    provider_deadline,
                ),
                reauthorize=reauthorize,
                diagnostics=diagnostics,
            )
            # Also cover the core's legitimate empty-allowlist early return.
            latest_allowed = reauthorize()
            with stage("search.response"):
                tools: list[dict[str, object]] = []
                for hit in result.hits:
                    if hit.tool.tool_id not in latest_allowed:
                        continue
                    candidate = tools + [hit.tool.descriptor]
                    if len(canonical_json({"tools": candidate})) > RESPONSE_LIMIT:
                        break
                    tools = candidate
            if self.observer is not None:
                self.observer(result.mode, result.abstained)
            return {"tools": tools}
        except CoreAuthorizationError:
            if authorization_fault:
                raise ServiceError() from None
            raise AuthorizationError() from None
        except ServiceError:
            raise
        except Exception:
            raise ServiceError() from None

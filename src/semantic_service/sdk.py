"""Retain documented target fields omitted by the pinned botocore service model.

Only this client's GetGatewayTarget response is extended. Public Botocore events
bind the response to the same call's inputs; no service models are patched.
The byte bound limits our reparse, after botocore has buffered and parsed HTTP.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol, cast

from semantic_core.catalog import GatewayIdentity, canonical_json
from semantic_service.catalog import CatalogError

MAX_TARGET_RESPONSE_BYTES = 16 * 1024 * 1024
_SERVICE = "bedrock-agentcore-control"
_OPERATION = "GetGatewayTarget"
_BINDING = "semantic_service.target_response_binding"
_FIELDS = ("listingMode", "mcpToolSchema")


class _Events(Protocol):
    def register(
        self, event_name: str, handler: Callable[..., None], *, unique_id: str
    ) -> None: ...


class _ServiceModel(Protocol):
    @property
    def service_name(self) -> str: ...


class _Meta(Protocol):
    @property
    def events(self) -> _Events: ...

    @property
    def service_model(self) -> _ServiceModel: ...


class _Client(Protocol):
    @property
    def meta(self) -> _Meta: ...


class _OperationModel(Protocol):
    @property
    def name(self) -> str: ...


class _HTTPResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def content(self) -> bytes: ...


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return cast(dict[str, object], value)


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON key")
        result[name] = value
    return result


def _invalid_constant(value: str) -> object:
    raise ValueError("nonfinite JSON constant")


class _TargetResponseAdapter:
    def __init__(self, gateway: GatewayIdentity) -> None:
        if gateway.region not in ("cn-north-1", "cn-northwest-1"):
            raise ValueError("target adapter requires a China deployment")
        self.gateway_id = gateway.gateway_id
        self.gateway_arn = (
            f"arn:aws-cn:bedrock-agentcore:{gateway.region}:{gateway.account}"
            f":gateway/{gateway.gateway_id}"
        )

    def bind(
        self,
        params: dict[str, object],
        model: _OperationModel,
        context: dict[str, object],
        **kwargs: object,
    ) -> None:
        target_id = params.get("targetId")
        if (
            model.name != _OPERATION
            or params.get("gatewayIdentifier") not in (self.gateway_id, self.gateway_arn)
            or not isinstance(target_id, str)
            or not target_id
        ):
            raise CatalogError("target response compatibility validation failed")
        context[_BINDING] = (self.gateway_arn, target_id)

    def restore(
        self,
        http_response: _HTTPResponse,
        parsed: dict[str, object],
        model: _OperationModel,
        context: dict[str, object],
        **kwargs: object,
    ) -> None:
        # Let botocore retain its normal modeled errors. Never inspect or copy
        # error response bodies, and never turn a failed call into a success.
        if http_response.status_code != 200:
            return
        try:
            if model.name != _OPERATION:
                raise ValueError("unexpected operation")
            binding = context[_BINDING]
            content = http_response.content
            if not isinstance(content, bytes) or not 0 < len(content) <= MAX_TARGET_RESPONSE_BYTES:
                raise ValueError("invalid response size")
            raw = _object(
                json.loads(
                    content.decode("utf-8"),
                    object_pairs_hook=_unique,
                    parse_constant=_invalid_constant,
                )
            )
            # Also reject numeric overflow such as 1e9999, at any depth.
            canonical_json(raw)
            if (
                binding != (self.gateway_arn, raw.get("targetId"))
                or raw.get("gatewayArn") != self.gateway_arn
                or parsed.get("gatewayArn") != raw["gatewayArn"]
                or parsed.get("targetId") != raw["targetId"]
            ):
                raise ValueError("response differs from request")
            raw_mcp = _object(_object(raw.get("targetConfiguration")).get("mcp"))
            parsed_mcp = _object(_object(parsed.get("targetConfiguration")).get("mcp"))
            if "mcpServer" not in raw_mcp:
                if "mcpServer" in parsed_mcp:
                    raise ValueError("target type disagreement")
                return
            if set(raw_mcp) != {"mcpServer"} or set(parsed_mcp) != {"mcpServer"}:
                raise ValueError("ambiguous target configuration")
            raw_server, parsed_server = (
                _object(raw_mcp["mcpServer"]),
                _object(parsed_mcp["mcpServer"]),
            )
            endpoint = raw_server.get("endpoint")
            if (
                not isinstance(endpoint, str)
                or not endpoint
                or parsed_server.get("endpoint") != endpoint
            ):
                raise ValueError("endpoint disagreement")
            # A future SDK may already model the fields. Accept equal JSON
            # values, including types, but never overwrite a disagreement.
            for name in _FIELDS:
                if name in parsed_server and (
                    name not in raw_server
                    or canonical_json(parsed_server[name]) != canonical_json(raw_server[name])
                ):
                    raise ValueError("modeled response disagreement")
            for name in _FIELDS:
                if name in raw_server:
                    parsed_server[name] = raw_server[name]
        except Exception:
            raise CatalogError("target response compatibility validation failed") from None


def install_target_response_adapter(client: object, gateway: GatewayIdentity) -> None:
    """Register only public, client-scoped hooks; repeat installation is idempotent."""
    meta = cast(_Client, client).meta
    if meta.service_model.service_name != _SERVICE:
        raise ValueError("target adapter requires the control client")
    adapter = _TargetResponseAdapter(gateway)
    event = f"{_SERVICE}.{_OPERATION}"
    meta.events.register(
        f"provide-client-params.{event}",
        adapter.bind,
        unique_id="semantic-service-target-input-v1",
    )
    meta.events.register(
        f"after-call.{event}",
        adapter.restore,
        unique_id="semantic-service-target-output-v1",
    )

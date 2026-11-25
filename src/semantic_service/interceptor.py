"""MCP REQUEST interceptor with captured China Gateway contract regressions."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import cast

from semantic_core.catalog import ToolRecord
from semantic_service._mcp_id import require_mcp_id
from semantic_service._search_contract import require_search, validate_arguments
from semantic_service.auth import (
    AuthenticationError,
    AuthorizationError,
    Authorizer,
    EnvelopeBinding,
    EnvelopeCodec,
    InputError,
    Limiter,
    ServiceError,
    TokenVerifier,
)


@dataclass(frozen=True, slots=True)
class InterceptorConfig:
    search_tool: ToolRecord
    machine_principals: frozenset[tuple[str, str]]


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InputError()
    return cast(dict[str, object], value)


def _forward(body: dict[str, object]) -> dict[str, object]:
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayRequest": {"body": body}},
    }


class RequestInterceptor:
    """No execution-role inference and no authorization derived from arguments."""

    def __init__(
        self,
        config: InterceptorConfig,
        verifier: TokenVerifier,
        envelope: EnvelopeCodec,
        authorizer: Authorizer,
        limiter: Limiter,
    ) -> None:
        self.config, self.verifier, self.envelope = config, verifier, envelope
        self.authorizer, self.limiter = authorizer, limiter

    def __call__(self, event: object, context: object) -> dict[str, object]:
        outer = _object(event)
        if outer.get("interceptorInputVersion") != "1.0":
            raise InputError()
        mcp = _object(outer.get("mcp"))
        if mcp.get("gatewayResponse") is not None:
            raise InputError()
        request = _object(mcp.get("gatewayRequest"))
        if request.get("path") != "/mcp" or request.get("httpMethod") != "POST":
            raise InputError()
        raw_headers = request.get("headers")
        if not isinstance(raw_headers, dict) or any(
            not isinstance(name, str) for name in raw_headers
        ):
            raise AuthenticationError()
        headers = cast(dict[str, object], raw_headers)
        auth_headers = [value for name, value in headers.items() if name.lower() == "authorization"]
        if len(auth_headers) != 1 or not isinstance(auth_headers[0], str):
            raise AuthenticationError()
        authorization = auth_headers[0]
        if not authorization.startswith("Bearer ") or not authorization[7:].strip():
            raise AuthenticationError()
        principal = self.verifier.verify(authorization[7:])
        body = copy.deepcopy(_object(request.get("body")))
        if body.get("jsonrpc") != "2.0":
            raise InputError()
        method = body.get("method")
        if method == "notifications/initialized":
            if "id" in body:
                raise InputError()
        else:
            try:
                require_mcp_id(body.get("id"))
            except ValueError:
                raise InputError() from None
        search_tool = self.config.search_tool
        gateway = search_tool.identity.gateway
        machine = (principal.issuer, principal.subject) in self.config.machine_principals
        # Every call, including protocol negotiation and machine listing, is
        # subject to current tombstone/subject revocation and the shared limiter.
        try:
            view = self.authorizer.read(principal, gateway)
        except ServiceError:
            raise
        except Exception:
            raise ServiceError() from None
        if view.control.tombstoned:
            raise AuthorizationError()
        self.limiter.admit(principal, gateway, machine=machine)
        if machine:
            if method not in {"initialize", "notifications/initialized", "ping", "tools/list"}:
                raise AuthorizationError()
        else:
            require_search(view, search_tool)
        params: dict[str, object] = {}
        if method in {"tools/list", "tools/call"}:
            params = _object(body.get("params", {}))
            allowed = (
                {"_meta", "cursor"} if method == "tools/list" else {"_meta", "name", "arguments"}
            )
            if set(params) - allowed:
                raise InputError()
            if "_meta" in params:
                _object(params["_meta"])
                # Protocol metadata is not a tool argument or trusted identity.
                params.pop("_meta")
            if (
                method == "tools/list"
                and "cursor" in params
                and not isinstance(params["cursor"], str)
            ):
                raise InputError()
        if machine:
            return _forward(body)
        if method in {"initialize", "notifications/initialized", "ping"}:
            return _forward(body)
        if method == "tools/list":
            return {
                "interceptorOutputVersion": "1.0",
                "mcp": {
                    "transformedGatewayResponse": {
                        "statusCode": 200,
                        "body": {
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "result": {"tools": [search_tool.descriptor]},
                        },
                    }
                },
            }
        if method != "tools/call":
            raise AuthorizationError()
        name = params.get("name")
        if not isinstance(name, str):
            raise InputError()
        identity = view.tools_by_name.get(name)
        if (
            identity is None
            or identity.key not in view.allowed_ids
            or identity.key not in view.schema_digests
        ):
            raise AuthorizationError()
        arguments = _object(params.get("arguments", {}))
        # Never propagate a caller-supplied trusted context, for any tool.
        arguments.pop("_auth_context", None)
        if name == search_tool.name:
            validate_arguments(arguments, require_envelope=False)
            binding = EnvelopeBinding(
                gateway.gateway_id,
                identity.target_id,
                name,
                cast(str | int, body["id"]),
                cast(str, arguments["query"]),
            )
            arguments["_auth_context"] = self.envelope.issue(
                principal, binding, policy_epoch=view.control.policy_epoch
            )
        params["arguments"] = arguments
        return _forward(body)

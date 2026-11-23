"""Bounded internal transport; never infer trusted identity from a signed payload."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

MAX_REQUEST_BYTES = 32768
PUBLIC_RESPONSE_BYTES = 524288
# Preserve the public Search cap while accommodating the private result envelope.
MAX_RESPONSE_BYTES = PUBLIC_RESPONSE_BYTES + 1024


class InvalidEnvelope(ValueError):
    """Preserve the existing Search authentication classification at admission."""


def encode_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate internal field")
        result[key] = value
    return result


def strict_json(raw: bytes, *, limit: int = MAX_RESPONSE_BYTES) -> dict[str, object]:
    if not raw or len(raw) > limit:
        raise ValueError("internal byte limit")
    value: object = json.loads(raw, object_pairs_hook=_unique, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("internal object required")
    return cast(dict[str, object], value)


def _invalid_constant(value: str) -> object:
    raise ValueError("nonfinite internal number")


def arguments(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) - {"query", "_auth_context"}:
        raise ValueError("invalid arguments")
    query, envelope = value.get("query"), value.get("_auth_context")
    if not isinstance(query, str) or not query.strip() or len(query) > 2048:
        raise ValueError("invalid arguments")
    if not isinstance(envelope, str) or not envelope.strip() or len(envelope) > 8192:
        raise InvalidEnvelope("invalid envelope")
    return cast(dict[str, object], value)


@dataclass(frozen=True)
class RouteIdentity:
    gateway_id: str
    target_id: str
    tool_name: str

    def __post_init__(self) -> None:
        if (
            any(
                not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) is None
                for value in (self.gateway_id, self.target_id)
            )
            or re.fullmatch(r"[A-Za-z0-9_-]+___search_tools", self.tool_name) is None
        ):
            raise ValueError("invalid route identity")

    @classmethod
    def from_env(cls) -> RouteIdentity:
        return cls(
            os.environ["SEMSEARCH_GATEWAY_ID"],
            os.environ["SEMSEARCH_SEARCH_TARGET_ID"],
            os.environ["SEMSEARCH_SEARCH_TOOL_NAME"],
        )

    def validate_custom(self, custom: object) -> str:
        if not isinstance(custom, Mapping):
            raise ValueError("missing trusted context")
        expected = {
            "bedrockAgentCoreMessageVersion": "1.0",
            "bedrockAgentCoreGatewayId": self.gateway_id,
            "bedrockAgentCoreTargetId": self.target_id,
            "bedrockAgentCoreToolName": self.tool_name,
        }
        if any(custom.get(key) != value for key, value in expected.items()):
            raise ValueError("route identity mismatch")
        mcp_id = custom.get("bedrockAgentCoreMcpMessageId")
        if not isinstance(mcp_id, str) or not mcp_id.strip() or len(mcp_id) > 256:
            raise ValueError("invalid MCP id")
        return mcp_id

    def context(self, mcp_id: str, deadline_ms: int | None = None) -> SimpleNamespace:
        custom = {
            "bedrockAgentCoreMessageVersion": "1.0",
            "bedrockAgentCoreGatewayId": self.gateway_id,
            "bedrockAgentCoreTargetId": self.target_id,
            "bedrockAgentCoreToolName": self.tool_name,
            "bedrockAgentCoreMcpMessageId": mcp_id,
        }
        self.validate_custom(custom)
        context = SimpleNamespace(client_context=SimpleNamespace(custom=custom))
        if deadline_ms is not None:
            # Convert once: later wall-clock changes cannot extend this attempt.
            end = time.monotonic() + max(0, deadline_ms / 1000 - time.time())
            context.get_remaining_time_in_millis = lambda: max(0, (end - time.monotonic()) * 1000)
        return context


def decode_request(raw: bytes) -> dict[str, object]:
    value = strict_json(raw, limit=MAX_REQUEST_BYTES)
    if (
        set(value) != {"version", "arguments", "custom", "deadline_ms", "attempt_id"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or type(value["deadline_ms"]) is not int
        or not isinstance(value["custom"], dict)
        or not isinstance(value["attempt_id"], str)
        or re.fullmatch(r"[a-f0-9]{32}", value["attempt_id"]) is None
    ):
        raise ValueError("invalid internal request")
    arguments(value["arguments"])
    return value

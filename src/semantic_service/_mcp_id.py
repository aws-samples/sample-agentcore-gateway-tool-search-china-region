"""Lossless JSON-RPC IDs for trusted bindings and private transport."""

from __future__ import annotations

import hashlib
import json


def require_mcp_id(value: object) -> str | int:
    """Validate a bounded binding ID without accepting bool or coercing types."""
    if type(value) is str:
        if value.strip() and len(value) <= 256:
            value.encode("utf-8")
            return value
    elif type(value) is int:
        # Bound decimal size without converting an unbounded integer to text.
        if -(10**256) < value < 10**256:
            return value
    raise ValueError("invalid MCP ID")


def correlation_id(value: str | int) -> str:
    """Domain-separate JSON IDs from the legacy same-value string digest."""
    encoded = json.dumps(require_mcp_id(value), ensure_ascii=False, separators=(",", ":"))
    prefix = "i:" if type(value) is int else "s:"
    return hashlib.sha256((prefix + encoded).encode("utf-8")).hexdigest()

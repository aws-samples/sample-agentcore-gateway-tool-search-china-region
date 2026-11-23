"""REQUEST 与 Search 共用的业务边界，不依赖检索引擎或索引。"""

from __future__ import annotations

from typing import cast

from semantic_core.catalog import ToolRecord
from semantic_service.auth import (
    AuthenticationError,
    AuthorizationError,
    AuthorizationView,
    InputError,
)


def validate_arguments(event: object, *, require_envelope: bool) -> dict[str, object]:
    if not isinstance(event, dict) or set(event) - {"query", "_auth_context"}:
        raise InputError()
    query = event.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 2048:
        raise InputError()
    if require_envelope and not isinstance(event.get("_auth_context"), str):
        raise AuthenticationError()
    return cast(dict[str, object], event)


def require_search(view: AuthorizationView, search_tool: ToolRecord) -> None:
    if (
        view.control.tombstoned
        or search_tool.tool_id not in view.allowed_ids
        or view.tools_by_name.get(search_tool.name) != search_tool.identity
        or view.schema_digests.get(search_tool.tool_id) != search_tool.schema_digest
    ):
        raise AuthorizationError()

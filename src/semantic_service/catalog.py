"""Read-only, machine-authenticated MCP catalog transport and trusted registration."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx

from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord, canonical_json, digest
from semantic_core.providers import ProviderError, RequestContext
from semantic_service._indexer_telemetry import increment, replay_attempt
from semantic_service.secrets import SecretsCache
from semantic_service.storage import _decode, _object, _string

_MCP_TIMEOUT_SECONDS = 10.0


class CatalogError(RuntimeError):
    """The full catalog could not be proven complete and stable."""


class _RefreshUnavailable(CatalogError):
    """同步读取无法在已知剩余预算内安全启动。"""


class _CatalogDeadlineExceeded(CatalogError):
    """保留 CatalogError 契约，供遥测区分预算截止与令牌获取失败。"""


class MachineTokenSource(Protocol):
    def token(self, context: RequestContext, *, force_refresh: bool = False) -> str: ...


class SecretResult(Protocol):
    @property
    def value(self) -> Mapping[str, object]: ...


class SecretSource(Protocol):
    def get(
        self,
        secret_id: str,
        *,
        stage: Literal["AWSCURRENT", "AWSPREVIOUS"] = "AWSCURRENT",
        force_refresh: bool = False,
    ) -> SecretResult: ...


def _url(url: str, allowed_hosts: Sequence[str]) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint must be allowlisted HTTPS without credentials or query")


def _remaining(context: RequestContext) -> float:
    try:
        return context.remaining()
    except ProviderError:
        raise _CatalogDeadlineExceeded("catalog deadline exceeded") from None


class OAuthTokenSource:
    """Client credentials from one explicit Secrets Manager secret; warm short-lived cache."""

    def __init__(
        self,
        cache: SecretSource,
        secret_id: str,
        token_endpoint: str,
        allowed_hosts: tuple[str, ...],
        scopes: tuple[str, ...],
        client: httpx.Client,
        *,
        timeout_seconds: float = 5,
    ) -> None:
        _url(token_endpoint, allowed_hosts)
        if not secret_id or not scopes or not 0 < timeout_seconds <= 30:
            raise ValueError("invalid machine OAuth configuration")
        self.cache, self.secret_id, self.endpoint = cache, secret_id, token_endpoint
        self.scopes, self.client, self.timeout = scopes, client, timeout_seconds
        self._token: str | None = None
        self._expires = 0.0

    def token(self, context: RequestContext, *, force_refresh: bool = False) -> str:
        _remaining(context)
        if not force_refresh and self._token is not None and time.monotonic() < self._expires:
            return self._token
        if not force_refresh and self._token is not None:
            increment("token_proactive_refresh_count")
        try:
            for attempt in range(2):
                if force_refresh or attempt > 0:
                    if not isinstance(self.cache, SecretsCache):
                        raise _RefreshUnavailable("machine authentication unavailable")
                    refreshed = self.cache._get_for_refresh(
                        self.secret_id, context.deadline, self.timeout
                    )
                    if refreshed is None:
                        raise _RefreshUnavailable("machine authentication unavailable")
                    secret: SecretResult = refreshed
                else:
                    secret = self.cache.get(self.secret_id)
                client_id = _string(secret.value.get("client_id"))
                client_secret = _string(secret.value.get("client_secret"))
                with self.client.stream(
                    "POST",
                    self.endpoint,
                    data={"grant_type": "client_credentials", "scope": " ".join(self.scopes)},
                    auth=httpx.BasicAuth(client_id, client_secret),
                    timeout=min(self.timeout, _remaining(context)),
                    follow_redirects=False,
                ) as response:
                    if response.status_code in (400, 401) and attempt == 0:
                        continue
                    if response.status_code != 200:
                        raise CatalogError("machine authentication failed")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        _remaining(context)
                        if len(body) + len(chunk) > 64 * 1024:
                            raise CatalogError("machine token response exceeds byte budget")
                        body.extend(chunk)
                    raw = _decode(bytes(body))
                token = _string(raw.get("access_token"))
                expires = raw.get("expires_in")
                if (
                    str(raw.get("token_type", "")).lower() != "bearer"
                    or type(expires) is not int
                    or not 1 <= expires <= 86400
                    or len(token) > 16384
                    or any(ord(char) < 33 for char in token)
                ):
                    raise CatalogError("invalid machine token response")
                self._token = token
                # 不晚于半寿命刷新，同时保留更早的安全余量和缓存上限。
                self._expires = time.monotonic() + max(0, min(expires / 2, expires - 30, 300))
                return token
            raise CatalogError("machine authentication failed")
        except CatalogError:
            raise
        except Exception:
            raise CatalogError("machine authentication unavailable") from None


class MCPClient:
    def __init__(
        self,
        client: httpx.Client,
        endpoint: str,
        allowed_hosts: tuple[str, ...],
        tokens: MachineTokenSource,
        *,
        protocol_versions: tuple[str, ...] = ("2025-03-26",),
        max_pages: int = 1000,
        max_tools: int = 20_100,
        max_response_bytes: int = 6 * 1024 * 1024,
    ) -> None:
        _url(endpoint, allowed_hosts)
        if not protocol_versions or max_pages < 1 or max_tools < 1 or max_response_bytes < 1:
            raise ValueError("invalid MCP limits")
        self.client, self.endpoint, self.tokens = client, endpoint, tokens
        self.protocol_versions, self.max_pages = protocol_versions, max_pages
        self.max_tools, self.max_response_bytes = max_tools, max_response_bytes
        self._id = 0
        self._session: str | None = None
        self._protocol: str | None = None

    def _request(
        self,
        method: str,
        params: dict[str, object],
        context: RequestContext,
        *,
        notification: bool = False,
    ) -> dict[str, object]:
        self._id += 1
        request_id = str(self._id)
        payload: dict[str, object] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = request_id
        try:
            for attempt in range(2):
                token_context = context
                if attempt:
                    # 从同一截止时间预留现有 Gateway HTTP 预算，不延长请求。
                    if context.deadline - time.monotonic() <= _MCP_TIMEOUT_SECONDS:
                        increment("gateway_replay_skipped_count")
                        raise CatalogError("MCP request failed")
                    token_context = RequestContext(
                        context.request_id, context.deadline - _MCP_TIMEOUT_SECONDS
                    )
                try:
                    token = self.tokens.token(token_context, force_refresh=attempt > 0)
                except _RefreshUnavailable:
                    if attempt:
                        increment("gateway_replay_skipped_count")
                        raise CatalogError("MCP request failed") from None
                    raise CatalogError("machine authentication unavailable") from None
                except _CatalogDeadlineExceeded:
                    if attempt:
                        increment("gateway_replay_skipped_count")
                    raise
                except Exception:
                    if attempt:
                        increment("gateway_auth_refresh_failure_count")
                    raise
                headers = {
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                }
                if self._session is not None:
                    headers["Mcp-Session-Id"] = self._session
                if self._protocol is not None:
                    headers["MCP-Protocol-Version"] = self._protocol
                # 预算检查在重试计数之前；响应流关闭失败也属于这次 HTTP 尝试失败。
                try:
                    timeout = min(_MCP_TIMEOUT_SECONDS, _remaining(context))
                except _CatalogDeadlineExceeded:
                    if attempt:
                        increment("gateway_replay_skipped_count")
                    raise
                with (
                    replay_attempt(attempt > 0),
                    self.client.stream(
                        "POST",
                        self.endpoint,
                        json=payload,
                        headers=headers,
                        timeout=timeout,
                        follow_redirects=False,
                    ) as response,
                ):
                    if response.status_code in (401, 403):
                        increment(f"gateway_{response.status_code}_count")
                        if attempt == 0:
                            continue
                    if notification and response.status_code in (200, 202, 204):
                        return {}
                    if response.status_code != 200:
                        raise CatalogError("MCP request failed")
                    session = response.headers.get("Mcp-Session-Id")
                    if session is not None:
                        if not 0 < len(session) <= 1024 or any(ord(c) < 33 for c in session):
                            raise CatalogError("invalid MCP session")
                        self._session = session
                    body = bytearray()
                    for part in response.iter_bytes():
                        _remaining(context)
                        if len(body) + len(part) > self.max_response_bytes:
                            raise CatalogError("MCP response exceeds byte budget")
                        body.extend(part)
                    content_type = response.headers.get("content-type", "").split(";", 1)[0]
                    raw = self._parse(bytes(body), content_type, request_id)
                    if (
                        raw.get("jsonrpc") != "2.0"
                        or type(raw.get("id")) is not str
                        or raw.get("id") != request_id
                        or "error" in raw
                        or "result" not in raw
                    ):
                        raise CatalogError("invalid MCP response envelope")
                    return _object(raw["result"])
            raise CatalogError("MCP authentication failed")
        except CatalogError:
            raise
        except Exception:
            raise CatalogError("MCP catalog unavailable") from None

    @staticmethod
    def _parse(body: bytes, content_type: str, request_id: str) -> dict[str, object]:
        if content_type == "text/event-stream":
            matches: list[dict[str, object]] = []
            # MCP SSE framing: comments and notification events do not constitute a result.
            for frame in body.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
                data = "\n".join(
                    line[5:].lstrip(" ") for line in frame.split("\n") if line.startswith("data:")
                )
                if data:
                    item = _decode(data.encode())
                    if type(item.get("id")) is str and item["id"] == request_id:
                        matches.append(item)
            if len(matches) != 1:
                raise CatalogError("missing or duplicate MCP SSE result")
            return matches[0]
        if content_type not in ("application/json", ""):
            raise CatalogError("unsupported MCP response content type")
        return _decode(body)

    def initialize(self, context: RequestContext) -> None:
        self._session, self._protocol = None, None
        result = self._request(
            "initialize",
            {
                "protocolVersion": self.protocol_versions[0],
                "capabilities": {},
                "clientInfo": {"name": "catalog-indexer", "version": "1"},
            },
            context,
        )
        version = result.get("protocolVersion")
        if not isinstance(version, str) or version not in self.protocol_versions:
            raise CatalogError("unsupported negotiated MCP version")
        capabilities = _object(result.get("capabilities"))
        if "tools" not in capabilities:
            raise CatalogError("MCP tools capability missing")
        self._protocol = version
        self._request("notifications/initialized", {}, context, notification=True)

    def list_tools(self, context: RequestContext) -> list[dict[str, object]]:
        self.initialize(context)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        descriptors: dict[str, dict[str, object]] = {}
        for _ in range(self.max_pages):
            result = self._request(
                "tools/list", {} if cursor is None else {"cursor": cursor}, context
            )
            page = result.get("tools")
            if not isinstance(page, list):
                raise CatalogError("invalid MCP tools page")
            for value in page:
                descriptor = _object(value)
                name = _string(descriptor.get("name"))
                existing = descriptors.get(name)
                if existing is not None and canonical_json(existing) != canonical_json(descriptor):
                    raise CatalogError("conflicting duplicate tool name")
                descriptors[name] = descriptor
                if len(descriptors) > self.max_tools:
                    raise CatalogError("catalog exceeds tool budget")
            if "nextCursor" not in result:
                return [descriptors[name] for name in sorted(descriptors)]
            cursor = result["nextCursor"] if isinstance(result["nextCursor"], str) else None
            if not cursor or cursor in seen_cursors or len(cursor) > 8192:
                raise CatalogError("invalid or repeated catalog cursor")
            seen_cursors.add(cursor)
        raise CatalogError("catalog page budget exceeded")


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    name: str
    target_id: str
    incarnation: str
    role: str = "business"

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.name, self.target_id, self.incarnation)
        ) or self.role not in ("business", "discovery"):
            raise ValueError("invalid explicit tool registration")


@dataclass(frozen=True, slots=True)
class TargetRegistry:
    gateway: GatewayIdentity
    tools: tuple[RegisteredTool, ...]
    _by_name: Mapping[str, RegisteredTool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("duplicate registered tool")
        object.__setattr__(self, "_by_name", {tool.name: tool for tool in self.tools})

    def materialize(self, descriptors: Sequence[dict[str, object]]) -> tuple[ToolRecord, ...]:
        result: list[ToolRecord] = []
        for descriptor in descriptors:
            name = descriptor.get("name")
            if name == "x_amz_bedrock_agentcore_search":
                # Native discovery has no business authorization identity in this registry.
                continue
            if not isinstance(name, str) or name not in self._by_name:
                raise CatalogError("tool has no trusted target registration")
            registered = self._by_name[name]
            try:
                result.append(
                    ToolRecord.from_descriptor(
                        ToolIdentity(
                            self.gateway, registered.target_id, registered.incarnation, name
                        ),
                        descriptor,
                    )
                )
            except ValueError:
                raise CatalogError("invalid catalog descriptor") from None
        return tuple(sorted(result, key=lambda tool: tool.name))

    def subset(self, tools: Sequence[ToolRecord]) -> TargetRegistry:
        """Attest observed descriptors; absent deleted tools need no live target."""
        return TargetRegistry(self.gateway, tuple(self._by_name[tool.name] for tool in tools))

    def searchable(self, tools: Sequence[ToolRecord]) -> tuple[ToolRecord, ...]:
        discovery_targets = {item.target_id for item in self.tools if item.role == "discovery"}
        return tuple(tool for tool in tools if tool.identity.target_id not in discovery_targets)


def _static_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("duplicate static schema JSON key")
        result[key] = value
    return result


def _static_json_constant(_: str) -> object:
    raise CatalogError("nonfinite static schema JSON number")


def _static_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise CatalogError("nonfinite static schema JSON number")
    return parsed


def _validate_static_schema(value: object) -> None:
    """Validate the documented inlinePayload-string / S3-reference union.

    This checks explicit static configuration, not an undocumented inline schema
    envelope. The observed tools/list descriptors receive their own validation.
    """
    if not isinstance(value, dict) or set(value) not in ({"inlinePayload"}, {"s3"}):
        raise CatalogError("invalid static MCP schema union")
    if "inlinePayload" in value:
        payload = value["inlinePayload"]
        if not isinstance(payload, str) or not payload.strip():
            raise CatalogError("empty or malformed inline MCP schema")
        try:
            json.loads(
                payload,
                object_pairs_hook=_static_json_object,
                parse_constant=_static_json_constant,
                parse_float=_static_json_float,
            )
        except (ValueError, RecursionError):
            raise CatalogError("invalid inline MCP schema JSON") from None
        return
    reference = value["s3"]
    if (
        not isinstance(reference, dict)
        or "uri" not in reference
        or not set(reference) <= {"uri", "bucketOwnerAccountId"}
    ):
        raise CatalogError("invalid static MCP schema S3 reference")
    uri = reference["uri"]
    if not isinstance(uri, str) or re.fullmatch(r"s3://.{1,2043}", uri) is None:
        raise CatalogError("invalid static MCP schema S3 URI")
    if "bucketOwnerAccountId" in reference:
        owner = reference["bucketOwnerAccountId"]
        if not isinstance(owner, str) or re.fullmatch(r"[0-9]{12}", owner) is None:
            raise CatalogError("invalid static MCP schema bucket owner")


def _validate_shared_mcp_scope(value: object) -> None:
    if not isinstance(value, dict):
        raise CatalogError("invalid MCP server configuration")
    # No mode is inferred from endpoint-only responses. Unknown future modes and
    # DYNAMIC remain unsupported even when an explicit static field also exists.
    if "listingMode" in value and value["listingMode"] != "DEFAULT":
        raise CatalogError("MCP listing mode is outside the shared catalog scope")
    if "mcpToolSchema" in value:
        _validate_static_schema(value["mcpToolSchema"])
    elif "listingMode" not in value:
        raise CatalogError("MCP target listing-mode provenance is unobservable")


class TargetClient(Protocol):
    def list_gateway_targets(self, **kwargs: object) -> dict[str, object]: ...
    def get_gateway_target(self, **kwargs: object) -> dict[str, object]: ...


class TargetStateSource:
    """Read-only control-plane registration fingerprint; no synchronization mutations."""

    def __init__(
        self, client: TargetClient, gateway: GatewayIdentity, *, max_pages: int = 100
    ) -> None:
        self.client, self.gateway, self.max_pages = client, gateway, max_pages
        self._observed: dict[str, dict[str, object]] = {}

    def fingerprint(self, context: RequestContext) -> str:
        self._observed = {}
        token: str | None = None
        seen: set[str] = set()
        targets: list[dict[str, object]] = []
        try:
            for _ in range(self.max_pages):
                _remaining(context)
                params: dict[str, object] = {
                    "gatewayIdentifier": self.gateway.gateway_id,
                    "maxResults": 100,
                }
                if token is not None:
                    params["nextToken"] = token
                response = self.client.list_gateway_targets(**params)
                items = response.get("items")
                if not isinstance(items, list):
                    raise CatalogError("invalid target page")
                for value in items:
                    item = _object(value)
                    target_id = _string(item.get("targetId"))
                    _remaining(context)
                    target = self.client.get_gateway_target(
                        gatewayIdentifier=self.gateway.gateway_id, targetId=target_id
                    )
                    if target.get("targetId") != target_id or target_id in self._observed:
                        raise CatalogError("invalid or duplicate current target identity")
                    if target.get("status") != "READY":
                        raise CatalogError("gateway target is not ready")
                    self._observed[target_id] = target
                    # Datetimes and the complete configuration are included, with stable strings.
                    targets.append(
                        {key: target[key] for key in sorted(target) if key != "ResponseMetadata"}
                    )
                if "nextToken" not in response:
                    targets.sort(key=lambda item: str(item["targetId"]))
                    encoded = json.dumps(
                        targets, sort_keys=True, separators=(",", ":"), default=str
                    ).encode()
                    return digest(encoded)
                token = _string(response["nextToken"])
                if token in seen:
                    raise CatalogError("repeated target cursor")
                seen.add(token)
            raise CatalogError("target page budget exceeded")
        except CatalogError:
            raise
        except Exception:
            raise CatalogError("target control state unavailable") from None

    def validate_registry(self, registry: TargetRegistry) -> None:
        """Bind configured incarnation identities to the just-observed live targets."""
        if registry.gateway != self.gateway:
            raise CatalogError("registry gateway mismatch")
        for tool in registry.tools:
            target = self._observed.get(tool.target_id)
            if target is None:
                raise CatalogError("registered target identity no longer exists")
            target_name = _string(target.get("name"))
            if not tool.name.startswith(target_name + "___"):
                raise CatalogError("registered tool is not owned by its target")
            configuration = _object(_object(target.get("targetConfiguration")).get("mcp"))
            if "mcpServer" in configuration:
                _validate_shared_mcp_scope(configuration["mcpServer"])
            elif not ("lambda" in configuration or "openApiSchema" in configuration):
                raise CatalogError("target type is outside the shared catalog scope")

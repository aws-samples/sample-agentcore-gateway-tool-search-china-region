"""Lazy Lambda entry points; every external destination comes from deployment config."""

# ruff: noqa: E402
from __future__ import annotations

import time

# Keep this timestamp before the imports it measures. It excludes Lambda's
# platform/runtime bootstrap; only the platform REPORT knows full Init Duration.
_MODULE_IMPORT_STARTED = time.perf_counter()

# isort: split
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict
from functools import lru_cache
from threading import Lock
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

import httpx
from botocore.config import Config
from botocore.session import Session

from semantic_core._timing import collect as collect_timings
from semantic_core._timing import stage, timed
from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord
from semantic_service.auth import (
    CounterClient,
    DDBAuthorizer,
    EnvelopeCodec,
    FixedWindowLimiter,
    OIDCConfig,
    OIDCVerifier,
    ServiceError,
    collect_binding_diagnostics,
    collect_oidc_attempts,
    collect_oidc_diagnostics,
)
from semantic_service.config import (
    ServiceSettings,
    parse_search_config,
    parse_search_request_seconds,
    required,
    scheduled_tick_is_current,
)
from semantic_service.deployment_config import (
    CONFIG_REFERENCE_FIELDS,
    ConfigurationClient,
    ConfigurationReference,
    load_configuration,
)
from semantic_service.interceptor import InterceptorConfig, RequestInterceptor
from semantic_service.secrets import SecretsCache, SecretsClient, SigningSecretsKeys
from semantic_service.storage import DynamoClient, S3Client, SnapshotReader, SnapshotStore

if TYPE_CHECKING:
    from semantic_core.providers import RequestContext
    from semantic_core.search import SearchDiagnostics, SearchMode
    from semantic_service.catalog import TargetRegistry
    from semantic_service.enhancer import CatalogEnhancer
    from semantic_service.indexer import CatalogIndexer
    from semantic_service.models import RotatingModelProviders
    from semantic_service.search import SearchHandler

_FIRST_INVOCATION = True
_INVOCATION_LOCK = Lock()
_AWS_SERVICES = ("s3", "dynamodb", "secretsmanager", "bedrock-agentcore-control")


def _flag(name: str, default: bool = False, *, env: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if env is None else env).get(name, str(default)).lower()
    if value not in ("true", "false"):
        raise ValueError("boolean setting must be true or false")
    return value == "true"


def _csv(name: str, env: Mapping[str, str]) -> tuple[str, ...]:
    values = tuple(value.strip() for value in required(env, name).split(","))
    if not all(values):
        raise ValueError("empty configuration list entry")
    return values


def _json(name: str, env: Mapping[str, str]) -> object:
    return json.loads(required(env, name))


@lru_cache(maxsize=4)
@timed("runtime.config")
def _loaded_configuration(
    reference: ConfigurationReference, gateway: GatewayIdentity, operation: str
) -> Mapping[str, str]:
    return load_configuration(cast(ConfigurationClient, _aws("s3")), reference, gateway, operation)


def _configuration(operation: str) -> Mapping[str, str]:
    base = dict(os.environ)
    if not CONFIG_REFERENCE_FIELDS.intersection(base):
        # Preserve explicit environment-only service use; partial references never fall back.
        return MappingProxyType(base)
    gateway = _settings().gateway
    reference = ConfigurationReference.from_env(base, gateway, operation)
    assert reference is not None
    return MappingProxyType(base | dict(_loaded_configuration(reference, gateway, operation)))


@lru_cache(maxsize=1)
def _settings() -> ServiceSettings:
    return ServiceSettings.from_env()


@lru_cache(maxsize=1)
@timed("runtime.aws_session")
def _aws_session() -> Session:
    # 同一运行环境只使用部署区域；凭证链和共享模型索引不归给首个服务。
    session = Session()
    session.set_config_variable("region", _settings().gateway.region)
    session.get_credentials()
    session.get_available_partitions()
    loader = session.get_component("data_loader")
    # 与 load_service_model 使用相同参数形式，复用 Loader 的实例缓存。
    loader.list_available_services("service-2")
    loader.list_available_services("endpoint-rule-set-1")
    return session


@lru_cache(maxsize=len(_AWS_SERVICES))
@timed("runtime.aws_client")
def _aws(service: str) -> object:
    if service not in _AWS_SERVICES:
        raise ValueError("unsupported AWS service")
    session = _aws_session()
    with stage(f"runtime.aws_client.{service.replace('-', '_')}"):
        client = cast(
            object,
            session.create_client(
                service,
                region_name=_settings().gateway.region,
                config=Config(
                    connect_timeout=1,
                    read_timeout=3,
                    retries={"total_max_attempts": 1, "mode": "standard"},
                ),
            ),
        )
        if service == "bedrock-agentcore-control":
            from semantic_service.sdk import install_target_response_adapter

            install_target_response_adapter(client, _settings().gateway)
        return client


@lru_cache(maxsize=1)
@timed("runtime.http_client")
def _http() -> httpx.Client:
    return httpx.Client(trust_env=False, follow_redirects=False)


@lru_cache(maxsize=1)
def _store() -> SnapshotStore:
    settings = _settings()
    return SnapshotStore(
        cast(S3Client, _aws("s3")),
        cast(DynamoClient, _aws("dynamodb")),
        settings.bucket,
        settings.table,
        settings.protected_table,
        settings.gateway,
    )


@lru_cache(maxsize=1)
def _secrets() -> SecretsCache:
    # These scopes must be distinct even if a deployment shares a physical account.
    names = (
        os.environ.get("MODEL_SECRET_ID", "semsearch/model-api"),
        os.environ.get("SEMSEARCH_SIGNING_SECRET_ID", "semsearch/signing"),
        os.environ.get("SEMSEARCH_OAUTH_SECRET_ID", "semsearch/indexer-oauth"),
    )
    if len(set(names)) != len(names):
        raise ValueError("model, OAuth and signing secrets must be distinct")
    return SecretsCache(cast(SecretsClient, _aws("secretsmanager")))


@lru_cache(maxsize=4)
def _envelope(operation: str = "search") -> EnvelopeCodec:
    configuration = _configuration(operation)
    keys = SigningSecretsKeys(
        _secrets(),
        os.environ.get("SEMSEARCH_SIGNING_SECRET_ID", "semsearch/signing"),
        revoked_kids=frozenset(
            value for value in configuration.get("SEMSEARCH_REVOKED_KIDS", "").split(",") if value
        ),
    )
    return EnvelopeCodec(keys)


@lru_cache(maxsize=4)
def _search_tool(operation: str = "search") -> ToolRecord:
    settings = _settings()
    configuration = _configuration(operation)
    descriptor = _json("SEMSEARCH_SEARCH_DESCRIPTOR_JSON", configuration)
    if not isinstance(descriptor, dict):
        raise ValueError("Search descriptor must be an object")
    return ToolRecord.from_descriptor(
        ToolIdentity(
            settings.gateway,
            settings.search_target_id,
            required(os.environ, "SEMSEARCH_SEARCH_INCARNATION"),
            settings.search_tool_name,
        ),
        descriptor,
    )


@lru_cache(maxsize=4)
@timed("runtime.models")
def _models(operation: str = "search") -> RotatingModelProviders | None:
    configuration = _configuration(operation)
    parse_search_config(configuration)
    if not _flag("SEMSEARCH_MODELS_ENABLED", env=configuration):
        return None
    from semantic_service.models import ModelSettings, RotatingModelProviders

    return RotatingModelProviders(_secrets(), ModelSettings.from_env(configuration), _http())


def _retrieval_mode(mode: SearchMode, abstained: bool) -> None:
    print(json.dumps({"operation": "retrieval", "mode": mode, "abstained": abstained}))


@lru_cache(maxsize=1)
def _search() -> SearchHandler:
    from semantic_service.search import SearchHandler

    configuration = _configuration("search")
    config = parse_search_config(configuration)
    models = _models("search")
    return SearchHandler(
        _search_tool(),
        _envelope("search"),
        DDBAuthorizer(_store()),
        SnapshotReader(_store()),
        embedding=models.embedding if models else None,
        reranker=models.rerank if models else None,
        config=config,
        request_seconds=parse_search_request_seconds(configuration),
        observer=_retrieval_mode,
    )


@lru_cache(maxsize=1)
def _request() -> RequestInterceptor:
    configuration = _configuration("request")
    machines = _json("SEMSEARCH_MACHINE_PRINCIPALS_JSON", configuration)
    if not isinstance(machines, list) or any(
        not isinstance(pair, list)
        or len(pair) != 2
        or any(not isinstance(item, str) or not item for item in pair)
        for pair in machines
    ):
        raise ValueError("machine principals must be explicit issuer/subject pairs")
    verifier = OIDCVerifier(
        OIDCConfig(
            issuer=required(configuration, "OIDC_ISSUER"),
            jwks_uri=required(configuration, "OIDC_JWKS_URI"),
            audience=required(configuration, "OIDC_AUDIENCE"),
            allowed_hosts=frozenset(_csv("OIDC_ALLOWED_HOSTS", configuration)),
            discovery_uri=required(configuration, "OIDC_DISCOVERY_URI"),
            algorithms=tuple(configuration.get("OIDC_ALGORITHMS", "RS256").split(",")),
            scope_claim=configuration.get("OIDC_SCOPE_CLAIM", "scope"),
            groups_claim=configuration.get("OIDC_GROUPS_CLAIM", "groups"),
            token_use_claim=configuration.get("OIDC_TOKEN_USE_CLAIM", "token_use"),
            token_use=configuration.get("OIDC_TOKEN_USE", "access"),
        ),
        _http(),
    )
    return RequestInterceptor(
        InterceptorConfig(
            _search_tool("request"), frozenset((pair[0], pair[1]) for pair in machines)
        ),
        verifier,
        _envelope("request"),
        DDBAuthorizer(_store()),
        FixedWindowLimiter(
            cast(CounterClient, _aws("dynamodb")),
            _settings().table,
            ordinary_limit=int(configuration.get("SEMSEARCH_SUBJECT_CALLS_PER_MINUTE", "600")),
            machine_limit=int(configuration.get("SEMSEARCH_MACHINE_CALLS_PER_MINUTE", "600")),
        ),
    )


@lru_cache(maxsize=1)
def _registry() -> TargetRegistry:
    from semantic_service.catalog import RegisteredTool, TargetRegistry

    raw = _json("SEMSEARCH_REGISTRY_JSON", _configuration("indexer"))
    if not isinstance(raw, list):
        raise ValueError("registry must be a list")
    entries = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"name", "target_id", "incarnation", "role"}:
            raise ValueError("invalid registry entry")
        if any(not isinstance(value, str) for value in item.values()):
            raise ValueError("registry values must be strings")
        entries.append(RegisteredTool(**item))
    settings = _settings()
    search = [item for item in entries if item.name == settings.search_tool_name]
    if len(search) != 1 or search[0].role != "discovery":
        raise ValueError("register Search explicitly as discovery")
    if search[0].target_id != settings.search_target_id:
        raise ValueError("Search registration target mismatch")
    if search[0].incarnation != required(os.environ, "SEMSEARCH_SEARCH_INCARNATION"):
        raise ValueError("Search registration incarnation mismatch")
    return TargetRegistry(settings.gateway, tuple(entries))


@lru_cache(maxsize=1)
def _indexer() -> CatalogIndexer:
    from semantic_service.catalog import (
        MCPClient,
        OAuthTokenSource,
        TargetClient,
        TargetStateSource,
    )
    from semantic_service.indexer import CatalogIndexer

    configuration = _configuration("indexer")
    tokens = OAuthTokenSource(
        _secrets(),
        os.environ.get("SEMSEARCH_OAUTH_SECRET_ID", "semsearch/indexer-oauth"),
        required(configuration, "OIDC_TOKEN_ENDPOINT"),
        _csv("OIDC_ALLOWED_HOSTS", configuration),
        _csv("OIDC_MACHINE_SCOPES", configuration),
        _http(),
    )
    mcp = MCPClient(
        _http(),
        required(configuration, "SEMSEARCH_GATEWAY_URL"),
        _csv("SEMSEARCH_GATEWAY_ALLOWED_HOSTS", configuration),
        tokens,
        protocol_versions=tuple(
            configuration.get("SEMSEARCH_MCP_VERSIONS", "2025-03-26").split(",")
        ),
    )
    return CatalogIndexer(
        _store(),
        _registry(),
        mcp,
        TargetStateSource(
            cast(TargetClient, _aws("bedrock-agentcore-control")), _settings().gateway
        ),
    )


@lru_cache(maxsize=1)
def _enhancer() -> CatalogEnhancer:
    from semantic_service.enhancer import CatalogEnhancer, EnhancementRepository

    models = _models("enhancer")
    if models is None:
        raise ValueError("models are disabled")
    # Generated examples remain pending until an explicit review callback is supplied.
    return CatalogEnhancer(_store(), EnhancementRepository(_store()), models.embedding, models.llm)


def _context(context: object, seconds: float) -> RequestContext:
    from semantic_core.providers import RequestContext

    request_id = getattr(context, "aws_request_id", "scheduled")
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if callable(remaining):
        seconds = min(seconds, max(0.0, float(remaining()) / 1000 - 0.5))
    request = RequestContext(request_id, time.monotonic() + seconds)
    request.remaining()
    return request


def _observed[T](
    operation: str,
    action: Callable[[], T],
    *,
    fields: Mapping[str, object] | None = None,
    context: object | None = None,
) -> T:
    global _FIRST_INVOCATION

    with _INVOCATION_LOCK:
        first_invocation, _FIRST_INVOCATION = _FIRST_INVOCATION, False
    started = time.monotonic()
    status = "internal failure"
    outcome_handled = False
    auth_fields: dict[str, object] = {}
    request_id = getattr(context, "aws_request_id", None)
    if not isinstance(request_id, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", request_id
    ):
        request_id = None

    def log_oidc_attempt(attempt: dict[str, object]) -> None:
        print(json.dumps({**attempt, "aws_request_id": request_id}))

    with (
        collect_timings() as timings,
        collect_binding_diagnostics() as binding_fields,
        collect_oidc_attempts(log_oidc_attempt if operation == "request" else None),
    ):
        try:
            result = action()
            outcome_handled = True
            status = "ok"
            if isinstance(result, dict) and isinstance(result.get("status"), str):
                status = result["status"]
            return result
        except ServiceError as exc:
            outcome_handled = True
            status = exc.message
            if exc.oidc_failure is not None:
                auth_fields["auth_failure"] = asdict(exc.oidc_failure)
            raise
        except Exception:
            outcome_handled = True
            status = "internal failure"
            raise ServiceError() from None
        finally:
            stages = timings.snapshot()
            logged_fields = dict(fields or {})
            if operation != "request" or not outcome_handled or "auth_failure" in auth_fields:
                logged_fields.pop("auth_recovered", None)
            payload = json.dumps(
                {
                    "operation": operation,
                    "status": status,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    **logged_fields,
                    "aws_request_id": request_id,
                    "first_invocation": first_invocation,
                    "runtime_module_init_ms": _RUNTIME_MODULE_INIT_MS,
                    "stage_timings": stages,
                    **auth_fields,
                    **binding_fields,
                }
            )
            try:
                print(payload)
            except Exception:  # nosec B110
                # Optional logs cannot replace the result or authentication error.
                pass


def search_handler(event: object, context: object) -> dict[str, object]:
    # No request state is retained by the cached SearchHandler. Unknown config is
    # null until initialization succeeds; no unvalidated environment text is logged.
    fields: dict[str, object] = {
        "configured_fusion": None,
        "ranking_mode": None,
        "empty_result": False,
        "below_threshold": False,
        "degraded": False,
        "vector_availability": "not_attempted",
        "rerank_state": "skipped",
        "embedding_error": None,
        "embedding_timing": None,
        "lambda_deadline_ignored": False,
    }

    def collect(value: SearchDiagnostics) -> None:
        fields.update(
            {
                **asdict(value),
                "embedding_error": (
                    None if value.embedding_error is None else value.embedding_error.as_log_fields()
                ),
            }
        )

    def run() -> dict[str, object]:
        with stage("handler.factory"):
            handler = _search()
        with stage("handler.work"):
            return handler(event, context, diagnostics=collect)

    return _observed("search", run, fields=fields, context=context)


def request_handler(event: object, context: object) -> dict[str, object]:
    def run() -> dict[str, object]:
        with stage("handler.factory"):
            handler = _request()
        with stage("handler.work"):
            return handler(event, context)

    with collect_oidc_diagnostics() as fields:
        return _observed("request", run, fields=fields, context=context)


def indexer_handler(event: object, context: object) -> dict[str, str]:
    from semantic_service._indexer_telemetry import collect

    def run() -> dict[str, str]:
        if not scheduled_tick_is_current(event):
            return {"status": "expired"}
        request = _context(context, 45)
        return {"status": _indexer().run(request)}

    with collect() as fields:
        return _observed("indexer", run, fields=fields, context=context)


def enhancer_handler(event: object, context: object) -> dict[str, str]:
    def run() -> dict[str, str]:
        if not scheduled_tick_is_current(event):
            return {"status": "expired"}
        configuration = _configuration("enhancer")
        parse_search_config(configuration)
        if not _flag("SEMSEARCH_MODELS_ENABLED", env=configuration):
            return {"status": "disabled"}
        request = _context(context, 240)
        return {"status": _enhancer().run(request)}

    return _observed("enhancer", run, context=context)


_RUNTIME_MODULE_INIT_MS = round((time.perf_counter() - _MODULE_IMPORT_STARTED) * 1000, 3)

"""Explicit public deployment settings; no lookups, credentials or secret values."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord, canonical_json
from semantic_service.catalog import RegisteredTool, TargetRegistry
from semantic_service.config import parse_search_config
from semantic_service.deployment_config import CONFIGURATION_FIELDS
from semantic_service.models import ModelSettings


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("expected a configuration object")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("expected nonempty configuration text")
    return value


def _strings(value: object) -> dict[str, str]:
    result = {}
    for key, item in _object(value).items():
        try:
            result[key] = _text(item)
        except ValueError:
            raise ValueError(f"{key} must be nonempty configuration text") from None
    return result


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration key")
        result[key] = value
    return result


def _https(value: str, hosts: list[str]) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise ValueError("HTTPS destination must match an explicit allowed host")


def _validate_search_schema(schema: dict[str, object]) -> None:
    """精确匹配已批准形状及参与授权摘要的说明文字。"""

    properties = _object(schema.get("properties"))
    if set(properties) != {"query", "_auth_context"}:
        raise ValueError("Search schema must declare only query and auth context")
    auth_context = {
        "type": "string",
        "description": "Server-managed signed context. Clients must omit this field.",
    }
    metadata = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "_auth_context": auth_context},
        "required": ["query"],
    }
    constrained = {
        **metadata,
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 2048},
            "_auth_context": {**auth_context, "maxLength": 8192},
        },
        "additionalProperties": False,
    }
    # 说明文字计入 schema_digest，校验不能剥离、改写或补回它。
    # JSON 比较继续区分 bool/float 和整数边界；运行时检查独立保留。
    if canonical_json(schema) not in (canonical_json(metadata), canonical_json(constrained)):
        raise ValueError("Search schema must preserve the approved query/auth-context contract")


@dataclass(frozen=True)
class SecretReference:
    name: str
    suffix: str
    kms_key_id: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9/_+=.@-]{1,512}", self.name):
            raise ValueError("invalid secret name")
        if not re.fullmatch(r"[A-Za-z0-9]{6}", self.suffix):
            raise ValueError("use the exact existing secret ARN's six-character suffix")
        if self.kms_key_id is not None and not re.fullmatch(
            r"(?:[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|mrk-[a-f0-9]{32})",
            self.kms_key_id,
        ):
            raise ValueError("use an exact KMS key ID, never a mutable alias")


@dataclass(frozen=True)
class EcsSearchConfig:
    """Public references only; creating identity, certificate and DNS needs approval."""

    image_uri: str
    hostname: str
    certificate_arn: str
    availability_zones: tuple[str, str]
    oauth_secret: SecretReference
    oauth_subject: str
    oauth_scope: str

    def validate(
        self, account: str, region: str, existing_secrets: dict[str, SecretReference]
    ) -> None:
        if not re.fullmatch(
            rf"{account}\.dkr\.ecr\.{region}\.amazonaws\.com\.cn/"
            r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}",
            self.image_uri,
        ):
            raise ValueError("ECS image must use a same-account/region ECR SHA-256 digest")
        if not re.fullmatch(
            r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
            r"[a-z][a-z0-9-]{0,62}",
            self.hostname,
        ):
            raise ValueError("ECS hostname must be an explicit lowercase DNS name")
        if not re.fullmatch(
            rf"arn:aws-cn:acm:{region}:{account}:certificate/[a-f0-9-]{{36}}",
            self.certificate_arn,
        ):
            raise ValueError("ECS requires an existing same-account/region ACM certificate ARN")
        if (
            len(self.availability_zones) != 2
            or len(set(self.availability_zones)) != 2
            or any(
                not re.fullmatch(re.escape(region) + "[a-z]", zone)
                for zone in self.availability_zones
            )
        ):
            raise ValueError("ECS requires two explicit distinct regional availability zones")
        if not isinstance(self.oauth_secret, SecretReference) or self.oauth_secret.name in {
            ref.name for ref in existing_secrets.values()
        }:
            raise ValueError("ECS OAuth secret must be dedicated and separately referenced")
        if not re.fullmatch(r"[!-~]{1,256}", self.oauth_subject):
            raise ValueError("ECS requires an explicit OAuth service subject")
        if "," in self.oauth_scope or not re.fullmatch(r"[!#-\[\]-~]{1,256}", self.oauth_scope):
            raise ValueError("ECS requires one explicit OAuth scope")


@dataclass(frozen=True)
class DeploymentConfig:
    account: str
    region: str
    stage: str
    deployment_name: str
    gateway_id: str
    gateway_url: str
    gateway_allowed_hosts: list[str]
    gateway_execution_role_name: str
    search_target_id: str
    search_incarnation: str
    search_descriptor: dict[str, object]
    registry: list[dict[str, str]]
    machine_principals: list[list[str]]
    oidc: dict[str, str]
    secrets: dict[str, SecretReference]
    model_settings: dict[str, str] = field(default_factory=dict)
    service_settings: dict[str, str] = field(default_factory=dict)
    models_enabled: bool = False
    schedules_enabled: bool = False
    request_package: str = "shared"
    request_provisioned_concurrency: int = 0
    log_retention_days: int = 30
    alarm_topic_arns: tuple[str, ...] = ()
    ecs_search: EcsSearchConfig | None = None

    def __post_init__(self) -> None:
        for options, namespace in ((self.oidc, "OIDC_"), (self.model_settings, "MODEL_")):
            if any(
                not key.startswith(namespace) or key not in CONFIGURATION_FIELDS for key in options
            ):
                raise ValueError("settings cannot override other configuration categories")
        if not re.fullmatch(r"[0-9]{12}", self.account):
            raise ValueError("explicit 12-digit account required; credential lookup is disabled")
        if self.region not in ("cn-north-1", "cn-northwest-1"):
            raise ValueError("only the two China regions are supported by the service")
        for value in (self.stage, self.deployment_name):
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,11}", value):
                raise ValueError("stage/deployment_name must be 1-12 lowercase name characters")
        for value in (self.gateway_id, self.search_target_id, self.search_incarnation):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
                raise ValueError("invalid Gateway/target/incarnation")
        if not re.fullmatch(r"[A-Za-z0-9_+=,.@-]{1,64}", self.gateway_execution_role_name):
            raise ValueError("existing same-account Gateway execution role name is required")
        _https(self.gateway_url, self.gateway_allowed_hosts)
        if self.log_retention_days not in (7, 14, 30, 60, 90, 180, 365):
            raise ValueError("unsupported explicit log retention")
        if type(self.models_enabled) is not bool or type(self.schedules_enabled) is not bool:
            raise ValueError("feature flags must be booleans")
        if self.request_package not in ("shared", "request"):
            raise ValueError("request_package must be shared or request")
        if (
            type(self.request_provisioned_concurrency) is not int
            or self.request_provisioned_concurrency < 0
        ):
            raise ValueError("request_provisioned_concurrency must be a nonnegative integer")
        if self.request_provisioned_concurrency and self.request_package != "request":
            raise ValueError("request_provisioned_concurrency requires request_package=request")
        if set(self.secrets) != {"model", "signing", "oauth"}:
            raise ValueError("separate model/signing/OAuth references are required")
        if len({ref.name for ref in self.secrets.values()}) != 3:
            raise ValueError("model, signing and OAuth secrets must be distinct")
        # Search switches are valid even when global model egress is disabled.
        parse_search_config(self.model_settings | self.service_settings)
        if self.models_enabled and not {
            "MODEL_ALLOWED_HOSTS",
            "MODEL_EMBEDDING_DIMENSIONS",
        }.issubset(self.model_settings):
            raise ValueError("enabled models require explicit allowed hosts and dimensions")
        if self.models_enabled:
            ModelSettings.from_env(self.model_settings)
        for topic in self.alarm_topic_arns:
            if not re.fullmatch(
                rf"arn:aws-cn:sns:{self.region}:{self.account}:[A-Za-z0-9_-]+", topic
            ):
                raise ValueError("alarm topics must be exact same-account/region China SNS ARNs")
        identity = GatewayIdentity(self.account, self.region, self.gateway_id)
        name = _text(self.search_descriptor.get("name"))
        if not re.fullmatch(r"[A-Za-z0-9_-]+___search_tools", name):
            raise ValueError("supply the exact Gateway-prefixed Search tool name")
        tool = ToolRecord.from_descriptor(
            ToolIdentity(identity, self.search_target_id, self.search_incarnation, name),
            self.search_descriptor,
        )
        _validate_search_schema(_object(self.search_descriptor.get("inputSchema")))
        registered = TargetRegistry(
            identity, tuple(RegisteredTool(**item) for item in self.registry)
        )
        search = [item for item in registered.tools if item.name == tool.name]
        if len(search) != 1 or search[0].role != "discovery":
            raise ValueError("register exactly one Search discovery entry")
        if (
            search[0].target_id != self.search_target_id
            or search[0].incarnation != self.search_incarnation
        ):
            raise ValueError("Search registration identity mismatch")
        if not self.machine_principals or any(
            not isinstance(pair, list)
            or len(pair) != 2
            or any(not isinstance(value, str) or not value for value in pair)
            for pair in self.machine_principals
        ):
            raise ValueError("explicit machine issuer/subject pairs are required")
        required_oidc = {
            "OIDC_ISSUER",
            "OIDC_AUDIENCE",
            "OIDC_DISCOVERY_URI",
            "OIDC_JWKS_URI",
            "OIDC_ALLOWED_HOSTS",
            "OIDC_TOKEN_ENDPOINT",
            "OIDC_MACHINE_SCOPES",
        }
        if not required_oidc.issubset(self.oidc):
            raise ValueError("complete explicit OIDC configuration required")
        hosts = self.oidc["OIDC_ALLOWED_HOSTS"].split(",")
        for key in ("OIDC_ISSUER", "OIDC_DISCOVERY_URI", "OIDC_JWKS_URI", "OIDC_TOKEN_ENDPOINT"):
            _https(self.oidc[key], hosts)
        if self.ecs_search is not None:
            if not isinstance(self.ecs_search, EcsSearchConfig):
                raise ValueError("ecs_search must be an explicit ECS configuration")
            self.ecs_search.validate(self.account, self.region, self.secrets)
            if not self.alarm_topic_arns:
                raise ValueError("ecs_search requires nonempty alarm_topic_arns")

    @property
    def prefix(self) -> str:
        return f"semsearch-{self.stage}-{self.deployment_name}"

    @property
    def search_tool_name(self) -> str:
        return _text(self.search_descriptor["name"])

    @classmethod
    def from_file(
        cls, path: Path, *, stage: str | None = None, region: str | None = None
    ) -> DeploymentConfig:
        raw = _object(json.loads(path.read_text(), object_pairs_hook=_unique))
        allowed = set(cls.__dataclass_fields__)
        if set(raw) - allowed:
            raise ValueError("unknown deployment configuration fields")
        secret_defaults = {
            "model": "semsearch/model-api",
            "signing": "semsearch/signing",
            "oauth": "semsearch/indexer-oauth",
        }
        secrets = {}
        for kind, item in _object(raw.pop("secrets")).items():
            value = _object(item)
            if set(value) - {"name", "suffix", "kms_key_id"}:
                raise ValueError("secret configuration contains an unsupported field")
            secrets[kind] = SecretReference(
                _text(value.get("name", secret_defaults.get(kind))),
                _text(value.get("suffix")),
                _text(value["kms_key_id"]) if "kms_key_id" in value else None,
            )
        for key in ("oidc", "model_settings", "service_settings"):
            if key in raw:
                raw[key] = _strings(raw[key])
        if raw.get("ecs_search") is not None:
            ecs = _object(raw["ecs_search"])
            if set(ecs) != set(EcsSearchConfig.__dataclass_fields__):
                raise ValueError("ecs_search requires exactly its explicit public fields")
            reference = _object(ecs["oauth_secret"])
            if set(reference) - {"name", "suffix", "kms_key_id"}:
                raise ValueError("ECS OAuth secret configuration contains unsupported fields")
            zones = ecs["availability_zones"]
            if not isinstance(zones, list) or any(not isinstance(zone, str) for zone in zones):
                raise ValueError("ECS availability_zones must be an explicit list")
            raw["ecs_search"] = EcsSearchConfig(
                image_uri=_text(ecs["image_uri"]),
                hostname=_text(ecs["hostname"]),
                certificate_arn=_text(ecs["certificate_arn"]),
                availability_zones=cast(tuple[str, str], tuple(zones)),
                oauth_secret=SecretReference(
                    _text(reference.get("name")),
                    _text(reference.get("suffix")),
                    _text(reference["kms_key_id"]) if "kms_key_id" in reference else None,
                ),
                oauth_subject=_text(ecs["oauth_subject"]),
                oauth_scope=_text(ecs["oauth_scope"]),
            )
        raw["secrets"] = secrets
        raw["alarm_topic_arns"] = tuple(cast(list[str], raw.get("alarm_topic_arns", [])))
        if stage is not None:
            raw["stage"] = stage
        if region is not None:
            raw["region"] = region
        return cls(**raw)  # type: ignore[arg-type]

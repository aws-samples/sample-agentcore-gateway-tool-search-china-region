"""Read only explicitly pinned, immutable, nonsecret deployment configuration."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, cast

from semantic_core.catalog import GatewayIdentity

CONFIG_MAX_BYTES = 1024 * 1024
CONFIG_REFERENCE_FIELDS = frozenset(
    {
        "SEMSEARCH_CONFIG_BUCKET",
        "SEMSEARCH_CONFIG_KEY",
        "SEMSEARCH_CONFIG_VERSION_ID",
        "SEMSEARCH_CONFIG_SHA256",
        "SEMSEARCH_CONFIG_BYTES",
    }
)
CONFIGURATION_FIELDS = frozenset(
    {
        "SEMSEARCH_SEARCH_DESCRIPTOR_JSON",
        "SEMSEARCH_REGISTRY_JSON",
        "SEMSEARCH_MACHINE_PRINCIPALS_JSON",
        "SEMSEARCH_TOP_K",
        "SEMSEARCH_SEARCH_TIMEOUT_SECONDS",
        "SEMSEARCH_FUSION_STRATEGY",
        "SEMSEARCH_MIN_VECTOR_SIMILARITY",
        "SEMSEARCH_MIN_VECTOR_MARGIN",
        "SEMSEARCH_MODELS_ENABLED",
        "SEMSEARCH_REVOKED_KIDS",
        "SEMSEARCH_SUBJECT_CALLS_PER_MINUTE",
        "SEMSEARCH_MACHINE_CALLS_PER_MINUTE",
        "SEMSEARCH_GATEWAY_URL",
        "SEMSEARCH_GATEWAY_ALLOWED_HOSTS",
        "SEMSEARCH_MCP_VERSIONS",
        "OIDC_ISSUER",
        "OIDC_AUDIENCE",
        "OIDC_DISCOVERY_URI",
        "OIDC_JWKS_URI",
        "OIDC_ALLOWED_HOSTS",
        "OIDC_ALGORITHMS",
        "OIDC_SCOPE_CLAIM",
        "OIDC_GROUPS_CLAIM",
        "OIDC_TOKEN_USE_CLAIM",
        "OIDC_TOKEN_USE",
        "OIDC_TOKEN_ENDPOINT",
        "OIDC_MACHINE_SCOPES",
        "MODEL_ALLOWED_HOSTS",
        "MODEL_BASE_URL",
        "MODEL_CONFIG_VERSION",
        "MODEL_EMBEDDING_MODEL",
        "MODEL_EMBEDDING_REVISION",
        "MODEL_EMBEDDING_DIMENSIONS",
        "MODEL_EMBEDDING_PATH",
        "MODEL_EMBEDDING_MAX_BATCH",
        "MODEL_EMBEDDING_MAX_TEXT_BYTES",
        "MODEL_EMBEDDING_SEND_DIMENSIONS",
        "MODEL_EMBEDDING_SEND_FLOAT_FORMAT",
        "MODEL_EMBEDDING_EXTRA_BODY",
        "MODEL_EMBEDDING_TIMEOUT_SECONDS",
        "MODEL_DOCUMENT_TIMEOUT_SECONDS",
        "MODEL_CHAT_MODEL",
        "MODEL_CHAT_REVISION",
        "MODEL_CHAT_PATH",
        "MODEL_CHAT_MAX_TEXT_BYTES",
        "MODEL_CHAT_MAX_TOKENS",
        "MODEL_CHAT_EXTRA_BODY",
        "MODEL_CHAT_TIMEOUT_SECONDS",
        "MODEL_RERANK_ENABLED",
        "MODEL_RERANK_MODEL",
        "MODEL_RERANK_REVISION",
        "MODEL_RERANK_PATH",
        "MODEL_RERANK_ENVELOPE",
        "MODEL_RERANK_SCORE_FIELD",
        "MODEL_RERANK_MAX_DOCUMENTS",
        "MODEL_RERANK_MAX_QUERY_BYTES",
        "MODEL_RERANK_MAX_DOCUMENT_BYTES",
        "MODEL_RERANK_MAX_TOTAL_BYTES",
        "MODEL_RERANK_TIMEOUT_SECONDS",
        "MODEL_AUTH_HEADER_NAME",
        "MODEL_AUTH_HEADER_FORMAT",
        "MODEL_RESPONSE_ALIASES",
        "MODEL_VALIDATE_MODEL_ECHO",
    }
)
_OPERATIONS = frozenset({"search", "request", "indexer", "enhancer"})


class ConfigurationError(ValueError):
    def __init__(self) -> None:
        super().__init__("deployment configuration unavailable or invalid")


class ConfigurationClient(Protocol):
    def get_object(self, **kwargs: object) -> dict[str, object]: ...


class _Body(Protocol):
    def read(self, size: int = -1) -> bytes: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class ConfigurationReference:
    bucket: str
    key: str
    version_id: str
    sha256: str
    bytes: int

    @classmethod
    def from_env(
        cls, environment: Mapping[str, str], gateway: GatewayIdentity, operation: str
    ) -> ConfigurationReference | None:
        present = CONFIG_REFERENCE_FIELDS.intersection(environment)
        if not present:
            return None
        if present != CONFIG_REFERENCE_FIELDS or operation not in _OPERATIONS:
            raise ConfigurationError()
        bucket = environment["SEMSEARCH_CONFIG_BUCKET"]
        key = environment["SEMSEARCH_CONFIG_KEY"]
        version = environment["SEMSEARCH_CONFIG_VERSION_ID"]
        digest = environment["SEMSEARCH_CONFIG_SHA256"]
        raw_size = environment["SEMSEARCH_CONFIG_BYTES"]
        namespace = "/".join(gateway.as_dict().values()) + "/"
        if (
            re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) is None
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or key != f"{namespace}configuration/{operation}/{digest}.json"
            or not version
            or version == "null"
            or len(version) > 1024
            or any(ord(character) < 33 or ord(character) > 126 for character in version)
            or re.fullmatch(r"[1-9][0-9]{0,6}", raw_size) is None
            or not 1 <= int(raw_size) <= CONFIG_MAX_BYTES
        ):
            raise ConfigurationError()
        return cls(bucket, key, version, digest, int(raw_size))


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError()
        result[key] = value
    return result


def _nonfinite(_: str) -> object:
    raise ConfigurationError()


def parse_configuration(data: bytes, gateway: GatewayIdentity, operation: str) -> Mapping[str, str]:
    """Validate the document without network access, also usable before publication."""
    try:
        if not 0 < len(data) <= CONFIG_MAX_BYTES or operation not in _OPERATIONS:
            raise ConfigurationError()
        payload = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_nonfinite
        )
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "gateway", "operation", "settings"}
            or type(payload["version"]) is not int
            or payload["version"] != 1
            or payload["gateway"] != gateway.as_dict()
            or payload["operation"] != operation
            or not isinstance(payload["settings"], dict)
        ):
            raise ConfigurationError()
        settings = payload["settings"]
        # Base resource identity and secret references remain in the small trusted
        # environment. Secret values are never part of deployment configuration.
        if not set(settings) <= CONFIGURATION_FIELDS or any(
            not isinstance(value, str) for value in settings.values()
        ):
            raise ConfigurationError()
        return MappingProxyType(cast(dict[str, str], settings))
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ConfigurationError() from None


def load_configuration(
    client: ConfigurationClient,
    reference: ConfigurationReference,
    gateway: GatewayIdentity,
    operation: str,
) -> Mapping[str, str]:
    """GET one exact object version, bounded by its expected bytes and SHA-256."""
    try:
        # Revalidate callers constructing the dataclass directly before any GET.
        checked = ConfigurationReference.from_env(
            {
                "SEMSEARCH_CONFIG_BUCKET": reference.bucket,
                "SEMSEARCH_CONFIG_KEY": reference.key,
                "SEMSEARCH_CONFIG_VERSION_ID": reference.version_id,
                "SEMSEARCH_CONFIG_SHA256": reference.sha256,
                "SEMSEARCH_CONFIG_BYTES": str(reference.bytes),
            },
            gateway,
            operation,
        )
        if checked != reference:
            raise ConfigurationError()
        response = client.get_object(
            Bucket=reference.bucket, Key=reference.key, VersionId=reference.version_id
        )
        body = cast(_Body, response["Body"])
        try:
            if (
                response.get("VersionId") != reference.version_id
                or type(response.get("ContentLength")) is not int
                or response["ContentLength"] != reference.bytes
            ):
                raise ConfigurationError()
            data = body.read(reference.bytes + 1)
        finally:
            body.close()
        if len(data) != reference.bytes or hashlib.sha256(data).hexdigest() != reference.sha256:
            raise ConfigurationError()
        return parse_configuration(data, gateway, operation)
    except Exception:
        raise ConfigurationError() from None

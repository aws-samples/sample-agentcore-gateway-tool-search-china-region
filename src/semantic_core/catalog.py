"""Immutable, versioned catalog identities and lossless tool descriptors."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from jsonschema import Draft202012Validator, ValidationError

if TYPE_CHECKING:
    from semantic_core.bm25 import DocumentFields

DESCRIPTOR_VERSION = 1
DESCRIPTOR_LIMIT = 128 * 1024
_DESCRIPTOR_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["name", "description", "inputSchema"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "inputSchema": {"type": "object"},
        "outputSchema": {"type": "object"},
        "annotations": {"type": "object"},
        "title": {"type": "string"},
        "_meta": {"type": "object"},
    },
    "additionalProperties": False,
}


def canonical_json(value: object) -> bytes:
    """Non-executable, deterministic JSON with non-finite numbers forbidden."""
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class GatewayIdentity:
    account: str
    region: str
    gateway_id: str

    def __post_init__(self) -> None:
        if any(not isinstance(v, str) or not v.strip() for v in self.as_dict().values()):
            raise ValueError("invalid gateway identity")

    def as_dict(self) -> dict[str, str]:
        return {"account": self.account, "region": self.region, "gateway_id": self.gateway_id}


@dataclass(frozen=True, slots=True)
class ToolIdentity:
    gateway: GatewayIdentity
    target_id: str
    incarnation: str
    full_name: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(v, str) or not v.strip()
            for v in (self.target_id, self.incarnation, self.full_name)
        ):
            raise ValueError("invalid tool identity")

    @property
    def key(self) -> str:
        """A tuple encoding avoids ambiguous concatenation and name reuse."""
        return canonical_json(
            [
                self.gateway.account,
                self.gateway.region,
                self.gateway.gateway_id,
                self.target_id,
                self.incarnation,
                self.full_name,
            ]
        ).decode("utf-8")

    def as_dict(self) -> dict[str, object]:
        return {
            "gateway": self.gateway.as_dict(),
            "target_id": self.target_id,
            "incarnation": self.incarnation,
            "full_name": self.full_name,
        }


@dataclass(frozen=True, slots=True)
class ToolRecord:
    identity: ToolIdentity
    descriptor_json: str
    examples: tuple[str, ...] = ()
    _tool_id: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            raw: object = json.loads(self.descriptor_json)
            Draft202012Validator(_DESCRIPTOR_SCHEMA).validate(raw)
            encoded = canonical_json(raw)
        except (ValueError, TypeError, ValidationError) as exc:
            raise ValueError("invalid tool descriptor") from exc
        if len(encoded) > DESCRIPTOR_LIMIT:
            raise ValueError("tool descriptor exceeds size budget")
        descriptor = cast(dict[str, object], raw)
        if descriptor["name"] != self.identity.full_name:
            raise ValueError("descriptor identity mismatch")
        if (
            not isinstance(self.examples, tuple)
            or len(self.examples) > 6
            or len(set(self.examples)) != len(self.examples)
            or any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in self.examples)
        ):
            raise ValueError("invalid reviewed examples")
        object.__setattr__(self, "descriptor_json", encoded.decode("utf-8"))
        object.__setattr__(self, "_tool_id", self.identity.key)

    @classmethod
    def from_descriptor(
        cls,
        identity: ToolIdentity,
        descriptor: dict[str, object],
        examples: tuple[str, ...] = (),
    ) -> ToolRecord:
        return cls(identity, canonical_json(descriptor).decode("utf-8"), examples)

    @property
    def descriptor(self) -> dict[str, object]:
        """Return a copy; callers cannot mutate the indexed descriptor."""
        return cast(dict[str, object], json.loads(self.descriptor_json))

    @property
    def tool_id(self) -> str:
        return self._tool_id

    @property
    def name(self) -> str:
        return self.identity.full_name

    @property
    def description(self) -> str:
        return cast(str, self.descriptor["description"])

    @property
    def schema_digest(self) -> str:
        desc = self.descriptor
        return digest(
            canonical_json({k: desc[k] for k in ("inputSchema", "outputSchema") if k in desc})
        )

    @property
    def descriptor_digest(self) -> str:
        return digest(self.descriptor_json.encode("utf-8"))

    @property
    def parameters(self) -> str:
        """Extract names/descriptions only; defaults and metadata are never summaries."""
        terms: list[str] = []

        def walk(value: object, depth: int = 0) -> None:
            if depth > 32:
                raise ValueError("schema nesting exceeds retrieval budget")
            if not isinstance(value, dict):
                return
            description = value.get("description")
            if isinstance(description, str):
                terms.append(description)
            properties = value.get("properties")
            if isinstance(properties, dict):
                for name, child in properties.items():
                    terms.append(str(name))
                    walk(child, depth + 1)
            walk(value.get("items"), depth + 1)
            for keyword in ("allOf", "anyOf", "oneOf"):
                children = value.get(keyword)
                if isinstance(children, list):
                    for child in children:
                        walk(child, depth + 1)

        walk(self.descriptor["inputSchema"])
        return " ".join(terms)

    def fields(self) -> DocumentFields:
        from semantic_core.bm25 import DocumentFields

        return DocumentFields(self.name, self.description, self.parameters, self.examples)

    @property
    def summary(self) -> str:
        return "\n".join((self.name, self.description, self.parameters)).strip()

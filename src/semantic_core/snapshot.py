"""Versioned, non-executable, integrity-checked local index snapshots."""

from __future__ import annotations

# Python requirement is >=3.12,<3.13; no pre-3.7 compatibility.
# nosemgrep: python37-compatibility-importlib2
import importlib.resources
import io
import json
import os
import stat
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO, Protocol, cast

import numpy as np
from jsonschema import Draft202012Validator, ValidationError
from numpy.typing import NDArray

from semantic_core import _index_config
from semantic_core.bm25 import BM25Index
from semantic_core.catalog import (
    DESCRIPTOR_VERSION,
    GatewayIdentity,
    ToolIdentity,
    ToolRecord,
    canonical_json,
    digest,
)
from semantic_core.providers import ModelProfile
from semantic_core.tokenization import ChineseTokenizer
from semantic_core.vectors import VectorIndex

SCHEMA_VERSION = 1
_FILE_LIMITS = {
    "manifest.json": 64 * 1024,
    "catalog.json": 128 * 1024 * 1024,
    "bm25.json": 128 * 1024 * 1024,
    "vectors.npy": 1024 * 1024 * 1024,
    "parents.json": 4 * 1024 * 1024,
}
_MAX_TOOLS = 20_000
_MAX_VECTORS = 140_000
_MAX_DIMENSION = 4096


class _HeaderReader(Protocol):
    def __call__(
        self, stream: BinaryIO, *, max_header_size: int
    ) -> tuple[tuple[int, ...], bool, np.dtype[np.generic]]: ...


# NumPy 2.3.4 exposes these public APIs, but its shipped stubs omit their
# signatures (including max_header_size). Keep that narrow boundary explicit.
_read_magic = cast(Callable[[BinaryIO], tuple[int, int]], np.lib.format.read_magic)
_read_header_v1 = cast(_HeaderReader, np.lib.format.read_array_header_1_0)
_read_header_v2 = cast(_HeaderReader, np.lib.format.read_array_header_2_0)


class SnapshotError(ValueError):
    """Reject the snapshot as a whole; never degrade integrity errors to BM25."""


class _ValidatedTokenizerMismatch(SnapshotError):
    """All snapshot checks passed, but its base tokenizer environment differs."""

    def __init__(self, snapshot: IndexSnapshot) -> None:
        super().__init__("snapshot validation failed")
        # Storage must still bind these identities to its trusted remote manifest.
        self.gateway = snapshot.gateway
        self.catalog_revision = snapshot.catalog_revision
        self.build_id = snapshot.build_id


@dataclass(frozen=True, slots=True)
class IndexSnapshot:
    gateway: GatewayIdentity
    tools: tuple[ToolRecord, ...]
    bm25: BM25Index
    catalog_revision: int = 1
    build_id: str = "local-v1"
    # A lambda factory has a valid implicit return; this is not module-level return.
    # nosemgrep: return-not-in-function
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    vectors: VectorIndex | None = None
    model_profile: ModelProfile | None = None

    def __post_init__(self) -> None:
        if type(self.catalog_revision) is not int or self.catalog_revision < 1:
            raise SnapshotError("invalid catalog revision")
        if not isinstance(self.build_id, str) or not self.build_id.strip():
            raise SnapshotError("invalid build identity")
        try:
            timestamp = datetime.fromisoformat(self.created_at)
            if timestamp.utcoffset() is None:
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise SnapshotError("invalid snapshot timestamp") from exc
        if not isinstance(self.tools, tuple):
            raise SnapshotError("catalog must be immutable")
        if any(tool.identity.gateway != self.gateway for tool in self.tools):
            raise SnapshotError("cross-gateway catalog")
        if len({tool.tool_id for tool in self.tools}) != len(self.tools):
            raise SnapshotError("duplicate tool identity")
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise SnapshotError("duplicate tool name")
        if self.bm25.document_count != len(self.tools):
            raise SnapshotError("catalog and lexical index differ")
        if tuple(tool.fields() for tool in self.tools) != self.bm25.documents:
            raise SnapshotError("catalog and lexical fields differ")
        if len(self.tools) > _MAX_TOOLS:
            raise SnapshotError("catalog exceeds the local snapshot bound")
        if (self.vectors is None) != (self.model_profile is None):
            raise SnapshotError("vector profile must accompany vectors")
        if self.vectors is not None and self.model_profile is not None:
            if (
                self.vectors.tool_count != len(self.tools)
                or self.vectors.dimension != self.model_profile.dimension
                or self.model_profile.normalization != "l2"
                or self.vectors.dimension > _MAX_DIMENSION
                or len(self.vectors.vectors) > _MAX_VECTORS
            ):
                raise SnapshotError("vector profile mismatch")

    @classmethod
    def build(
        cls,
        gateway: GatewayIdentity,
        tools: Sequence[ToolRecord],
        *,
        tokenizer: ChineseTokenizer | None = None,
        catalog_revision: int = 1,
        build_id: str = "local-v1",
        vectors: VectorIndex | None = None,
        model_profile: ModelProfile | None = None,
    ) -> IndexSnapshot:
        return cls(
            gateway=gateway,
            tools=tuple(tools),
            bm25=BM25Index.build(
                [tool.fields() for tool in tools],
                tokenizer
                if tokenizer is not None
                else ChineseTokenizer(_index_config.TOKENIZER_CONFIG),
            ),
            catalog_revision=catalog_revision,
            build_id=build_id,
            created_at=datetime.now(UTC).isoformat(),
            vectors=vectors,
            model_profile=model_profile,
        )


def save_snapshot(snapshot: IndexSnapshot, directory: Path) -> None:
    """Publish one new local snapshot; the manifest is always the final file.

    Existing destinations are never overwritten. An interrupted save may leave
    an incomplete directory; readers reject missing or inconsistent objects.
    Successful return syncs files, snapshot entries, and the new parent entry.
    S3 object VersionIds belong to the future storage adapter, not this format.
    """
    snapshot.__post_init__()
    try:
        catalog = [
            {
                "identity": tool.identity.as_dict(),
                "descriptor": tool.descriptor,
                "examples": list(tool.examples),
                "descriptor_digest": tool.descriptor_digest,
                "schema_digest": tool.schema_digest,
            }
            for tool in snapshot.tools
        ]
        objects: dict[str, bytes] = {
            "catalog.json": canonical_json(catalog),
            "bm25.json": canonical_json(snapshot.bm25.to_dict()),
        }
        vector_count = 0
        profile: dict[str, object] | None = None
        if snapshot.vectors is not None and snapshot.model_profile is not None:
            vector_count = len(snapshot.vectors.vectors)
            stream = io.BytesIO()
            np.save(stream, snapshot.vectors.vectors, allow_pickle=False)
            objects["vectors.npy"] = stream.getvalue()
            objects["parents.json"] = canonical_json(snapshot.vectors.parent_indices.tolist())
            profile = _profile_dict(snapshot.model_profile)
        manifest: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "descriptor_version": DESCRIPTOR_VERSION,
            "storage": "local",
            "gateway": snapshot.gateway.as_dict(),
            "catalog_revision": snapshot.catalog_revision,
            "build_id": snapshot.build_id,
            "created_at": snapshot.created_at,
            "tool_count": len(snapshot.tools),
            "vector_count": vector_count,
            "model_profile": profile,
            "objects": {
                name: {"bytes": len(data), "sha256": digest(data)} for name, data in objects.items()
            },
        }
        _validate_manifest(manifest)
        objects["manifest.json"] = canonical_json(manifest)
        if any(len(data) > _FILE_LIMITS[name] for name, data in objects.items()):
            raise SnapshotError("snapshot object exceeds local size bound")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise SnapshotError("cannot serialize snapshot") from None
    # mkdir is the exclusive reservation; FileExistsError deliberately remains
    # visible to the caller as the immutable destination contract.
    directory.mkdir()
    try:
        with _directory_fd(directory) as directory_fd:
            for name, data in objects.items():
                if name == "manifest.json":
                    # Persist asset entries before publishing the completion marker.
                    os.fsync(directory_fd)
                with (directory / name).open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            # Persist the manifest entry after its file contents have been synced.
            os.fsync(directory_fd)
        with _directory_fd(directory.parent) as parent_fd:
            # mkdir created a new entry in this already-existing parent.
            os.fsync(parent_fd)
    except OSError:
        raise SnapshotError("snapshot write failed") from None


def load_snapshot(
    directory: Path,
    *,
    expected_gateway: GatewayIdentity,
    expected_model: ModelProfile | None = None,
) -> IndexSnapshot:
    """Load only a complete, bounded snapshot belonging to the trusted namespace."""
    try:
        with _directory_fd(directory) as directory_fd:
            manifest = _dictionary(_decode_json(_read_file(directory_fd, "manifest.json")))
            _validate_manifest(manifest)
            gateway = _gateway(manifest["gateway"])
            if gateway != expected_gateway:
                raise SnapshotError("snapshot gateway mismatch")
            profile = _profile(manifest["model_profile"])
            if expected_model is not None and profile != expected_model:
                raise SnapshotError("snapshot model mismatch")
            references = _dictionary(manifest["objects"])
            objects: dict[str, bytes] = {}
            for name, raw_reference in references.items():
                reference = _dictionary(raw_reference)
                data = _read_file(directory_fd, name)
                if len(data) != reference["bytes"] or digest(data) != reference["sha256"]:
                    raise SnapshotError("snapshot object integrity mismatch")
                objects[name] = data
        catalog = _decode_json(objects["catalog.json"])
        if not isinstance(catalog, list) or len(catalog) != _integer(manifest["tool_count"]):
            raise SnapshotError("catalog count mismatch")
        tools = tuple(_tool(value, gateway) for value in catalog)
        bm25, tokenizer_mismatch = BM25Index._restore(
            _dictionary(_decode_json(objects["bm25.json"]))
        )
        vectors = (
            _restore_vectors(
                objects["vectors.npy"],
                _decode_json(objects["parents.json"]),
                len(tools),
                _integer(manifest["vector_count"]),
                profile,
            )
            if profile is not None
            else None
        )
        snapshot = IndexSnapshot(
            gateway=gateway,
            tools=tools,
            bm25=bm25,
            catalog_revision=_integer(manifest["catalog_revision"]),
            build_id=_string(manifest["build_id"]),
            created_at=_string(manifest["created_at"]),
            vectors=vectors,
            model_profile=profile,
        )
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise SnapshotError("snapshot validation failed") from None
    if tokenizer_mismatch:
        # Never return or publish the validation-only object. Incompatibility is
        # recoverable only after integrity, statistics, vectors, and identities
        # within this snapshot have all passed their normal validation.
        raise _ValidatedTokenizerMismatch(snapshot)
    return snapshot


@lru_cache(maxsize=1)
def _schema() -> dict[str, object]:
    source = importlib.resources.files("semantic_core").joinpath("schemas/snapshot-v1.json")
    return _dictionary(_decode_json(source.read_bytes()))


def _validate_manifest(manifest: dict[str, object]) -> None:
    try:
        Draft202012Validator(_schema()).validate(manifest)
    except ValidationError:
        raise SnapshotError("invalid snapshot manifest") from None
    for name in (
        "schema_version",
        "descriptor_version",
        "catalog_revision",
        "tool_count",
        "vector_count",
    ):
        _integer(manifest[name])
    for value in _dictionary(manifest["objects"]).values():
        _integer(_dictionary(value)["bytes"])


@contextmanager
def _directory_fd(directory: Path) -> Iterator[int]:
    # dir_fd + O_NOFOLLOW prevent a symlink swap between checking and opening an
    # asset. Linux (the target runtime) and macOS (local development) support it.
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield directory_fd
    finally:
        os.close(directory_fd)


def _read_file(directory_fd: int, name: str) -> bytes:
    if name not in _FILE_LIMITS:
        raise SnapshotError("unknown snapshot object")
    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(file_fd, "rb") as handle:
        details = os.fstat(handle.fileno())
        limit = _FILE_LIMITS[name]
        if not stat.S_ISREG(details.st_mode) or not 0 < details.st_size <= limit:
            raise SnapshotError("invalid snapshot object size or file type")
        data = handle.read(details.st_size + 1)
        if len(data) != details.st_size or len(data) > limit:
            raise SnapshotError("snapshot object changed while reading")
        return data


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite_json(_: str) -> object:
    raise SnapshotError("non-finite JSON number")


def _decode_json(data: bytes) -> object:
    decoded: object = json.loads(
        data, object_pairs_hook=_unique_object, parse_constant=_nonfinite_json
    )
    return decoded


def _dictionary(value: object, keys: set[str] | None = None) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or any(not isinstance(key, str) for key in value)
        or (keys is not None and set(value) != keys)
    ):
        raise SnapshotError("invalid object fields")
    return cast(dict[str, object], value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise SnapshotError("invalid string")
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise SnapshotError("invalid integer")
    return value


def _gateway(value: object) -> GatewayIdentity:
    record = _dictionary(value, {"account", "region", "gateway_id"})
    return GatewayIdentity(
        _string(record["account"]),
        _string(record["region"]),
        _string(record["gateway_id"]),
    )


def _profile_dict(profile: ModelProfile) -> dict[str, object]:
    return {
        "model_id": profile.model_id,
        "revision": profile.revision,
        "dimension": profile.dimension,
        "normalization": profile.normalization,
        "config_version": profile.config_version,
    }


def _profile(value: object) -> ModelProfile | None:
    if value is None:
        return None
    record = _dictionary(
        value, {"model_id", "revision", "dimension", "normalization", "config_version"}
    )
    return ModelProfile(
        _string(record["model_id"]),
        _string(record["revision"]),
        _integer(record["dimension"]),
        _string(record["normalization"]),
        _string(record["config_version"]),
    )


def _tool(value: object, expected_gateway: GatewayIdentity) -> ToolRecord:
    row = _dictionary(
        value, {"identity", "descriptor", "examples", "schema_digest", "descriptor_digest"}
    )
    identity = _dictionary(row["identity"], {"gateway", "target_id", "incarnation", "full_name"})
    gateway = _gateway(identity["gateway"])
    if gateway != expected_gateway:
        raise SnapshotError("cross-gateway catalog identity")
    examples = row["examples"]
    if not isinstance(examples, list) or any(not isinstance(value, str) for value in examples):
        raise SnapshotError("invalid catalog examples")
    tool = ToolRecord.from_descriptor(
        ToolIdentity(
            gateway,
            _string(identity["target_id"]),
            _string(identity["incarnation"]),
            _string(identity["full_name"]),
        ),
        _dictionary(row["descriptor"]),
        tuple(examples),
    )
    if (
        tool.schema_digest != row["schema_digest"]
        or tool.descriptor_digest != row["descriptor_digest"]
    ):
        raise SnapshotError("catalog descriptor digest mismatch")
    return tool


def _restore_vectors(
    data: bytes,
    parents: object,
    tool_count: int,
    vector_count: int,
    profile: ModelProfile,
) -> VectorIndex:
    stream = io.BytesIO(data)
    version = _read_magic(stream)
    if version == (1, 0):
        shape, fortran, dtype = _read_header_v1(stream, max_header_size=4096)
    elif version == (2, 0):
        shape, fortran, dtype = _read_header_v2(stream, max_header_size=4096)
    else:
        raise SnapshotError("unsupported vector array version")
    dimension = profile.dimension
    if (
        dimension is None
        or not 1 <= dimension <= _MAX_DIMENSION
        or not 0 <= vector_count <= _MAX_VECTORS
        or dtype != np.dtype(np.float32)
        or fortran
        or shape != (vector_count, dimension)
        or any(type(size) is not int for size in shape)
        or len(data) - stream.tell() != vector_count * dimension * 4
    ):
        raise SnapshotError("invalid vector array header or length")
    matrix: NDArray[np.float32] = np.frombuffer(
        data, dtype=np.float32, offset=stream.tell()
    ).reshape(vector_count, dimension)
    # Validate in bounded batches, avoiding a float64 copy of the entire matrix.
    for offset in range(0, vector_count, 1024):
        batch = matrix[offset : offset + 1024].astype(np.float64)
        norms = np.linalg.norm(batch, axis=1)
        if not np.isfinite(batch).all() or not np.allclose(norms, 1.0, atol=1e-6, rtol=0):
            raise SnapshotError("stored vectors must be finite and L2 normalized")
    if (
        not isinstance(parents, list)
        or len(parents) != vector_count
        or any(type(parent) is not int or not 0 <= parent < tool_count for parent in parents)
    ):
        raise SnapshotError("invalid vector parent references")
    # All VectorIndex invariants have been checked above. Restoring the validated
    # immutable buffers directly avoids re-normalization changing float32 bytes.
    result = object.__new__(VectorIndex)
    result.tool_count = tool_count
    result.dimension = dimension
    result.vectors = matrix
    result.parent_indices = np.frombuffer(
        np.asarray(parents, dtype=np.intp).tobytes(), dtype=np.intp
    )
    return result

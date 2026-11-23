"""Version-bound objects with physically separate content and authorization tables.

Only an authorization administrator writes PROTECTED policy references or tombstones.
Index writers check the protected table while CAS-writing CONTROL in the state table.
Legacy combined records and shared-table PROTECTED items require an admin migration.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from semantic_core._timing import stage, timed
from semantic_core.catalog import GatewayIdentity, ToolRecord, canonical_json, digest

if TYPE_CHECKING:
    from semantic_core.providers import RequestContext
    from semantic_core.snapshot import IndexSnapshot

_OBJECT_LIMIT = 1024 * 1024 * 1024
_FILES = {"manifest.json", "catalog.json", "bm25.json", "vectors.npy", "parents.json"}
_PROTECTED_FIELDS = {"policy_epoch", "tombstoned", "policy", "protected_version"}
_SNAPSHOT_WORKERS = 3
_SNAPSHOT_CHUNK_BYTES = 256 * 1024
# 按最小的 Indexer/Enhancer 1024 MiB 校准，Search 为 2048 MiB。
# 500 工具 × 6 例句 × 1024 维的加载、换代和完整增强发布已本地测量；
# 包括旧新索引/多词典共存，校准目标为进程峰值仍留至少 256 MiB 安全余量。
# 共同准入取最小函数内存的 1/4，读写一致；infra 测试防止部署内存漂移。
# 测量条件、峰值与预算推导见[容量报告](../../docs/capacity.md)。
# 这是容量准入策略，不是任意自定义词典或模型输出的全进程 RSS 上界。
_SNAPSHOT_RESTORE_BUDGET = (1024 * 1024 * 1024) // 4


def _check_snapshot_sizes(sizes: Mapping[str, int]) -> None:
    """共享读写准入；恢复预算是保守策略，不是全进程 RSS 的数学上界。"""
    from semantic_core.snapshot import _FILE_LIMITS

    if not {"manifest.json", "catalog.json", "bm25.json"} <= sizes.keys() <= _FILES:
        raise StorageError("invalid snapshot object set")
    if any(not 0 < size <= _FILE_LIMITS[name] for name, size in sizes.items()):
        raise StorageError("snapshot object size rejected")
    # JSON 的对象/校验副本比编码大；向量恢复还有 float64 归一性校验临时量。
    # 兼顾 1 GiB Enhancer 的多份索引，避免把格式上限当作运行内存承诺。
    estimated = sum(size * (8 if name == "vectors.npy" else 64) for name, size in sizes.items())
    if estimated > _SNAPSHOT_RESTORE_BUDGET:
        raise StorageError("snapshot restore budget exceeded")


class StorageError(RuntimeError):
    """An unverifiable control record or object must fail closed."""


class _SnapshotIncompatible(StorageError):
    """Only a fully validated snapshot may request a tokenizer migration."""


class PublishConflict(StorageError):
    """A competing publish, policy update, or tombstone won the CAS."""


class S3Client(Protocol):
    def put_object(self, **kwargs: object) -> dict[str, object]: ...
    def get_object(self, **kwargs: object) -> dict[str, object]: ...


class DynamoClient(Protocol):
    def get_item(self, **kwargs: object) -> dict[str, object]: ...
    def put_item(self, **kwargs: object) -> dict[str, object]: ...
    def transact_write_items(self, **kwargs: object) -> dict[str, object]: ...


class _Body(Protocol):
    def read(self, size: int = -1) -> bytes: ...
    def close(self) -> None: ...


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise StorageError("invalid stored object")
    return cast(dict[str, object], value)


def _integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise StorageError("invalid stored integer")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise StorageError("invalid stored string")
    return value


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StorageError("duplicate stored JSON key")
        result[key] = value
    return result


def _decode(data: bytes) -> dict[str, object]:
    try:
        return _object(json.loads(data, object_pairs_hook=_unique))
    except (ValueError, TypeError):
        raise StorageError("invalid stored JSON") from None


def catalog_digest(tools: Sequence[ToolRecord]) -> str:
    """Content identity intentionally excludes generated examples and build IDs."""
    return digest(
        canonical_json(
            [
                {"id": tool.tool_id, "descriptor": tool.descriptor_digest}
                for tool in sorted(tools, key=lambda item: item.name)
            ]
        )
    )


@dataclass(frozen=True, slots=True)
class ObjectRef:
    key: str
    version_id: str
    bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _string(self.key)
        _string(self.version_id)
        if self.version_id == "null" or not 0 < _integer(self.bytes) <= _OBJECT_LIMIT:
            raise StorageError("object versioning or size is invalid")
        if not re.fullmatch("[0-9a-f]{64}", self.sha256):
            raise StorageError("invalid object digest")

    @classmethod
    def parse(cls, raw: object) -> ObjectRef:
        value = _object(raw)
        if set(value) != {"key", "version_id", "bytes", "sha256"}:
            raise StorageError("invalid object reference")
        return cls(
            _string(value["key"]),
            _string(value["version_id"]),
            _integer(value["bytes"], 1),
            _string(value["sha256"]),
        )


@dataclass(frozen=True, slots=True)
class ControlRecord:
    gateway: GatewayIdentity
    manifest_version: int = 0
    catalog_revision: int = 0
    catalog_digest: str = ""
    safety_revision: int = 0
    policy_epoch: int = 1
    protected_version: int = 1
    tombstoned: bool = False
    manifest: ObjectRef | None = None
    safety: ObjectRef | None = None
    policy: ObjectRef | None = None
    reconciled_at: float = 0.0

    def __post_init__(self) -> None:
        for value in (self.manifest_version, self.catalog_revision, self.safety_revision):
            _integer(value)
        _integer(self.policy_epoch, 1)
        _integer(self.protected_version, 1)
        if type(self.tombstoned) is not bool or not math.isfinite(self.reconciled_at):
            raise StorageError("invalid control record")
        if self.catalog_revision > 0 and (
            self.manifest is None
            or self.safety is None
            or not re.fullmatch("[0-9a-f]{64}", self.catalog_digest)
        ):
            raise StorageError("incomplete active control record")

    def as_dict(self) -> dict[str, object]:
        result = cast(dict[str, object], asdict(self))
        result["gateway"] = self.gateway.as_dict()
        return result

    @classmethod
    def parse(cls, value: object) -> ControlRecord:
        raw = _object(value)
        expected = set(cls.__dataclass_fields__)
        if set(raw) != expected:
            raise StorageError("invalid control fields")
        gateway = _object(raw["gateway"])
        identity = GatewayIdentity(
            *(_string(gateway[key]) for key in ("account", "region", "gateway_id"))
        )
        timestamp = raw["reconciled_at"]
        if type(timestamp) not in (int, float):
            raise StorageError("invalid reconciliation timestamp")
        if type(raw["tombstoned"]) is not bool:
            raise StorageError("invalid tombstone")
        return cls(
            gateway=identity,
            manifest_version=_integer(raw["manifest_version"]),
            catalog_revision=_integer(raw["catalog_revision"]),
            catalog_digest=cast(str, raw["catalog_digest"]),
            safety_revision=_integer(raw["safety_revision"]),
            policy_epoch=_integer(raw["policy_epoch"], 1),
            protected_version=_integer(raw["protected_version"], 1),
            tombstoned=raw["tombstoned"],
            manifest=ObjectRef.parse(raw["manifest"]) if raw["manifest"] is not None else None,
            safety=ObjectRef.parse(raw["safety"]) if raw["safety"] is not None else None,
            policy=ObjectRef.parse(raw["policy"]) if raw["policy"] is not None else None,
            reconciled_at=float(cast(float, timestamp)),
        )


class SnapshotStore:
    def __init__(
        self,
        s3: S3Client,
        dynamodb: DynamoClient,
        bucket: str,
        table: str,
        protected_table: str,
        gateway: GatewayIdentity,
    ) -> None:
        if any("/" in part or part in (".", "..") for part in gateway.as_dict().values()):
            raise ValueError("unsafe gateway namespace")
        if any(
            not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", value)
            for value in (table, protected_table)
        ):
            raise ValueError("invalid table name")
        if table == protected_table:
            raise ValueError("state and protected tables must be distinct")
        self.s3, self.dynamodb = s3, dynamodb
        self.bucket, self.table, self.gateway = bucket, table, gateway
        self.protected_table = protected_table
        self.prefix = "/".join(gateway.as_dict().values()) + "/"
        self.key = self.prefix + "CONTROL"
        self.protected_key = self.prefix + "PROTECTED"

    @staticmethod
    def _content_dict(control: ControlRecord) -> dict[str, object]:
        content = {
            key: value for key, value in control.as_dict().items() if key not in _PROTECTED_FIELDS
        }
        content["format_version"] = 2
        return content

    @staticmethod
    def _protected_dict(control: ControlRecord) -> dict[str, object]:
        if control.policy is None:
            raise StorageError("protected policy reference is required")
        return {
            "format_version": 1,
            "gateway": control.gateway.as_dict(),
            "protected_version": control.protected_version,
            "policy_epoch": control.policy_epoch,
            "tombstoned": control.tombstoned,
            "policy": asdict(control.policy),
        }

    def _item(self, control: ControlRecord) -> dict[str, object]:
        return {
            "pk": {"S": self.key},
            "data": {"S": canonical_json(self._content_dict(control)).decode()},
            "manifest_version": {"N": str(control.manifest_version)},
            "catalog_revision": {"N": str(control.catalog_revision)},
            "safety_revision": {"N": str(control.safety_revision)},
        }

    def _protected_item(self, control: ControlRecord) -> dict[str, object]:
        return {
            "pk": {"S": self.protected_key},
            "data": {"S": canonical_json(self._protected_dict(control)).decode()},
            "protected_version": {"N": str(control.protected_version)},
            "policy_epoch": {"N": str(control.policy_epoch)},
            "tombstoned": {"BOOL": control.tombstoned},
        }

    def _read_item(self, table: str, key: str) -> dict[str, object]:
        result = self.dynamodb.get_item(
            TableName=table, Key={"pk": {"S": key}}, ConsistentRead=True
        )
        return _object(result["Item"])

    def _read_protected(self) -> ControlRecord:
        item = self._read_item(self.protected_table, self.protected_key)
        raw = _decode(_string(_object(item["data"])["S"]).encode())
        if (
            set(raw) != _PROTECTED_FIELDS | {"gateway", "format_version"}
            or type(raw["format_version"]) is not int
            or raw["format_version"] != 1
            or raw["gateway"] != self.gateway.as_dict()
            or type(raw["tombstoned"]) is not bool
        ):
            raise StorageError("invalid protected state")
        protected = ControlRecord(
            self.gateway,
            policy_epoch=_integer(raw["policy_epoch"], 1),
            protected_version=_integer(raw["protected_version"], 1),
            tombstoned=raw["tombstoned"],
            policy=ObjectRef.parse(raw["policy"]),
        )
        if item != self._protected_item(protected):
            raise StorageError("protected CAS fields differ")
        return protected

    @timed("aws.control")
    def read_control(self) -> ControlRecord:
        """Read a consistent content/security pair or fail closed on an admin race."""
        try:
            protected = self._read_protected()
            item = self._read_item(self.table, self.key)
            raw = _string(_object(item["data"])["S"])
            content = _decode(raw.encode())
            if (
                set(content)
                != (set(ControlRecord.__dataclass_fields__) - _PROTECTED_FIELDS)
                | {"format_version"}
                or type(content["format_version"]) is not int
                or content["format_version"] != 2
            ):
                raise StorageError("control format requires administrative migration")
            del content["format_version"]
            content.update(
                {
                    key: value
                    for key, value in protected.as_dict().items()
                    if key in _PROTECTED_FIELDS
                }
            )
            control = ControlRecord.parse(content)
            if control.gateway != self.gateway:
                raise StorageError("control gateway mismatch")
            if item != self._item(control):
                raise StorageError("control CAS fields differ")
            # Every writer transaction is conditioned on this monotonic protected
            # generation. Equal surrounding strong reads establish a valid pair
            # at the CONTROL read; a concurrent admin change is never hidden.
            if self._read_protected() != protected:
                raise StorageError("protected state changed during read")
            return control
        except StorageError:
            raise
        except Exception:
            raise StorageError("control unavailable") from None

    @staticmethod
    def _checkpoint(context: RequestContext | None) -> None:
        if context is not None:
            from semantic_core.providers import ProviderError

            try:
                context.remaining()
            except ProviderError:
                raise StorageError("publish deadline exceeded") from None

    def _put(self, key: str, data: bytes, *, context: RequestContext | None = None) -> ObjectRef:
        self._checkpoint(context)
        if not key.startswith(self.prefix) or len(data) > _OBJECT_LIMIT:
            raise StorageError("object namespace or size rejected")
        try:
            response = self.s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                IfNoneMatch="*",
                ServerSideEncryption="AES256",
            )
            reference = ObjectRef(key, _string(response["VersionId"]), len(data), digest(data))
            self._checkpoint(context)
            if self._get(reference) != data:
                raise StorageError("uploaded object mismatch")
            self._checkpoint(context)
            return reference
        except StorageError:
            raise
        except Exception:
            raise StorageError("immutable object upload failed") from None

    def _check_reference(self, reference: ObjectRef, *, limit: int = _OBJECT_LIMIT) -> None:
        if (
            not reference.key.startswith(self.prefix)
            or "/.." in reference.key
            or reference.bytes > limit
        ):
            raise StorageError("object namespace or size rejected")

    def _get(self, reference: ObjectRef, *, limit: int = _OBJECT_LIMIT) -> bytes:
        self._check_reference(reference, limit=limit)
        try:
            response = self.s3.get_object(
                Bucket=self.bucket, Key=reference.key, VersionId=reference.version_id
            )
            body = cast(_Body, response["Body"])
            try:
                if (
                    response.get("VersionId") != reference.version_id
                    or response.get("ContentLength") != reference.bytes
                ):
                    raise StorageError("object metadata mismatch")
                data = body.read(reference.bytes + 1)
            finally:
                body.close()
            if len(data) != reference.bytes or digest(data) != reference.sha256:
                raise StorageError("object integrity mismatch")
            return data
        except StorageError:
            raise
        except Exception:
            raise StorageError("object unavailable") from None

    def _download_snapshot_object(self, reference: ObjectRef, destination: Path) -> None:
        """只持有一个有界数据块，写入私有临时文件并流式校验完整性。"""
        self._check_reference(reference)
        try:
            response = self.s3.get_object(
                Bucket=self.bucket, Key=reference.key, VersionId=reference.version_id
            )
            body = cast(_Body, response["Body"])
            try:
                if (
                    response.get("VersionId") != reference.version_id
                    or response.get("ContentLength") != reference.bytes
                ):
                    raise StorageError("object metadata mismatch")
                remaining = reference.bytes
                hasher = hashlib.sha256()
                with destination.open("xb") as output:
                    while remaining:
                        requested = min(_SNAPSHOT_CHUNK_BYTES, remaining)
                        chunk = body.read(requested)
                        if not isinstance(chunk, bytes) or not 0 < len(chunk) <= requested:
                            raise StorageError("object integrity mismatch")
                        hasher.update(chunk)
                        output.write(chunk)
                        remaining -= len(chunk)
                        # 下一次 read 求值前释放上一块，保证每个 worker 仅一块。
                        del chunk
                    if body.read(1) != b"" or hasher.hexdigest() != reference.sha256:
                        raise StorageError("object integrity mismatch")
            finally:
                body.close()
        except StorageError:
            raise
        except Exception:
            raise StorageError("object unavailable") from None

    def initialize(self, policy: Mapping[str, object], *, policy_epoch: int = 1) -> ControlRecord:
        """Admin-only atomic creation; neither an old CONTROL nor tombstone is replaced."""
        if policy.get("epoch") != policy_epoch:
            raise StorageError("policy epoch mismatch")
        reference = self._put(
            self.prefix + "policies/" + uuid.uuid4().hex + ".json", canonical_json(dict(policy))
        )
        control = ControlRecord(self.gateway, policy_epoch=policy_epoch, policy=reference)
        try:
            self.dynamodb.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.protected_table,
                            "Item": self._protected_item(control),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table,
                            "Item": self._item(control),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
        except Exception as exc:
            self._write_error(exc)
        return control

    @staticmethod
    def _write_error(exc: Exception) -> None:
        response = getattr(exc, "response", {})
        if isinstance(response, dict):
            code = response.get("Error", {}).get("Code")
            if code in {"ConditionalCheckFailedException", "TransactionConflictException"}:
                raise PublishConflict("conditional publish lost") from None
            if code == "TransactionCanceledException":
                reasons = response.get("CancellationReasons", [])
                codes = {reason.get("Code") for reason in reasons if isinstance(reason, dict)}
                if codes & {"ConditionalCheckFailed", "TransactionConflict"} and codes <= {
                    None,
                    "None",
                    "ConditionalCheckFailed",
                    "TransactionConflict",
                }:
                    raise PublishConflict("conditional publish lost") from None
        raise StorageError("control write failed") from None

    def _content_condition(self, expected: ControlRecord) -> dict[str, object]:
        return {
            "ConditionExpression": (
                "manifest_version = :v AND catalog_revision = :r "
                "AND safety_revision = :s AND #data = :data"
            ),
            "ExpressionAttributeNames": {"#data": "data"},
            "ExpressionAttributeValues": {
                ":data": {"S": canonical_json(self._content_dict(expected)).decode()},
                ":v": {"N": str(expected.manifest_version)},
                ":r": {"N": str(expected.catalog_revision)},
                ":s": {"N": str(expected.safety_revision)},
            },
        }

    def _protected_condition(self, expected: ControlRecord) -> dict[str, object]:
        return {
            "ConditionExpression": (
                "protected_version = :pv AND policy_epoch = :p "
                "AND tombstoned = :false AND #data = :data"
            ),
            "ExpressionAttributeNames": {"#data": "data"},
            "ExpressionAttributeValues": {
                ":data": {"S": canonical_json(self._protected_dict(expected)).decode()},
                ":pv": {"N": str(expected.protected_version)},
                ":p": {"N": str(expected.policy_epoch)},
                ":false": {"BOOL": False},
            },
        }

    def _commit(self, expected: ControlRecord, updated: ControlRecord) -> None:
        if expected.gateway != self.gateway or updated.gateway != self.gateway:
            raise StorageError("cross-gateway publish rejected")
        if expected.tombstoned:
            raise PublishConflict("gateway permanently deleted")
        if self._protected_dict(expected) != self._protected_dict(updated):
            raise StorageError("background publication cannot modify protected state")
        try:
            self.dynamodb.transact_write_items(
                TransactItems=[
                    {
                        "ConditionCheck": {
                            "TableName": self.protected_table,
                            "Key": {"pk": {"S": self.protected_key}},
                            **self._protected_condition(expected),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table,
                            "Item": self._item(updated),
                            **self._content_condition(expected),
                        }
                    },
                ]
            )
        except Exception as exc:
            self._write_error(exc)

    def _commit_protected(self, expected: ControlRecord, updated: ControlRecord) -> None:
        """Admin operation requiring PutItem on PROTECTED and ConditionCheckItem on CONTROL."""
        if expected.gateway != self.gateway or updated.gateway != self.gateway:
            raise StorageError("cross-gateway protected change rejected")
        if expected.tombstoned:
            raise PublishConflict("gateway permanently deleted")
        policy_update = (
            not updated.tombstoned
            and updated.policy_epoch == expected.policy_epoch + 1
            and updated.policy is not None
            and updated.policy != expected.policy
        )
        deletion = (
            updated.tombstoned
            and updated.policy_epoch == expected.policy_epoch
            and updated.policy == expected.policy
        )
        if (
            self._content_dict(expected) != self._content_dict(updated)
            or updated.protected_version != expected.protected_version + 1
            or not (policy_update or deletion)
        ):
            raise StorageError("invalid protected state transition")
        try:
            self.dynamodb.transact_write_items(
                TransactItems=[
                    {
                        "ConditionCheck": {
                            "TableName": self.table,
                            "Key": {"pk": {"S": self.key}},
                            **self._content_condition(expected),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.protected_table,
                            "Item": self._protected_item(updated),
                            **self._protected_condition(expected),
                        }
                    },
                ]
            )
        except Exception as exc:
            self._write_error(exc)

    def _save(self, snapshot: IndexSnapshot, context: RequestContext | None = None) -> ObjectRef:
        from semantic_core.snapshot import save_snapshot

        if snapshot.gateway != self.gateway:
            raise StorageError("cross-gateway snapshot")
        build_prefix = self.prefix + "versions/" + uuid.uuid4().hex + "/"
        try:
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "snapshot"
                save_snapshot(snapshot, directory)
                paths = sorted(directory.iterdir())
                _check_snapshot_sizes({path.name: path.stat().st_size for path in paths})
                objects = {
                    path.name: asdict(
                        self._put(build_prefix + path.name, path.read_bytes(), context=context)
                    )
                    for path in paths
                }
            envelope = {
                "version": 1,
                "gateway": self.gateway.as_dict(),
                "catalog_revision": snapshot.catalog_revision,
                "build_id": snapshot.build_id,
                "objects": objects,
            }
            return self._put(
                build_prefix + "remote.json", canonical_json(envelope), context=context
            )
        except StorageError:
            raise
        except (OSError, ValueError):
            raise StorageError("snapshot upload failed") from None

    def publish_base(
        self,
        expected: ControlRecord,
        snapshot: IndexSnapshot,
        safety_tools: Sequence[ToolRecord],
        *,
        context: RequestContext | None = None,
    ) -> ControlRecord:
        if snapshot.catalog_revision != expected.catalog_revision + 1:
            raise PublishConflict("base revision must advance once")
        if any(tool.identity.gateway != self.gateway for tool in safety_tools):
            raise StorageError("cross-gateway safety catalog")
        safety_map = {tool.tool_id: tool.schema_digest for tool in safety_tools}
        if len(safety_map) != len(safety_tools) or any(
            safety_map.get(tool.tool_id) != tool.schema_digest for tool in snapshot.tools
        ):
            raise StorageError("content and safety identities differ")
        manifest = self._save(snapshot, context)
        revision = expected.safety_revision + 1
        safety = self._put(
            self.prefix + "safety/" + uuid.uuid4().hex + ".json",
            canonical_json(
                {
                    "version": 1,
                    "gateway": self.gateway.as_dict(),
                    "revision": revision,
                    "tools": safety_map,
                }
            ),
            context=context,
        )
        updated = replace(
            expected,
            manifest_version=expected.manifest_version + 1,
            catalog_revision=snapshot.catalog_revision,
            catalog_digest=catalog_digest(safety_tools),
            safety_revision=revision,
            manifest=manifest,
            safety=safety,
            reconciled_at=time.time(),
        )
        self._checkpoint(context)
        self._commit(expected, updated)
        return updated

    def publish_enhanced(
        self,
        expected: ControlRecord,
        snapshot: IndexSnapshot,
        *,
        context: RequestContext | None = None,
    ) -> ControlRecord:
        if snapshot.catalog_revision != expected.catalog_revision or expected.catalog_revision < 1:
            raise PublishConflict("enhancer catalog revision changed")
        safety = self.load_safety(expected)
        if any(safety.get(tool.tool_id) != tool.schema_digest for tool in snapshot.tools):
            raise StorageError("enhancer identity or schema changed")
        original = self.load_snapshot(expected)
        if catalog_digest(snapshot.tools) != catalog_digest(original.tools):
            raise StorageError("enhancer changed descriptor catalog")
        if snapshot.bm25.tokenizer.config != original.bm25.tokenizer.config:
            raise StorageError("enhancer changed tokenizer configuration")
        reference = self._save(snapshot, context)
        # A successful same-digest poll does not cancel this catalog revision.
        # Rebase only its age field, never identity, policy, safety or content state.
        for _ in range(3):
            self._checkpoint(context)
            current = self.read_control()
            if replace(expected, reconciled_at=current.reconciled_at) != current:
                raise PublishConflict("enhancer observed a newer control generation")
            updated = replace(
                current, manifest_version=current.manifest_version + 1, manifest=reference
            )
            self._checkpoint(context)
            try:
                self._commit(current, updated)
                return updated
            except PublishConflict:
                continue
        raise PublishConflict("enhancer reconciliation race exceeded retry budget")

    def update_policy(self, expected: ControlRecord, policy: Mapping[str, object]) -> ControlRecord:
        """Authorization-administrator only; runtime index/search roles never call this."""
        epoch = expected.policy_epoch + 1
        if type(policy.get("epoch")) is not int or policy.get("epoch") != epoch:
            raise StorageError("policy epoch must advance once")
        reference = self._put(
            self.prefix + "policies/" + uuid.uuid4().hex + ".json", canonical_json(dict(policy))
        )
        updated = replace(
            expected,
            policy_epoch=epoch,
            protected_version=expected.protected_version + 1,
            policy=reference,
        )
        self._commit_protected(expected, updated)
        return updated

    def reconcile(
        self,
        expected: ControlRecord,
        *,
        reconciled_at: float | None = None,
        context: RequestContext | None = None,
    ) -> ControlRecord:
        updated = replace(
            expected, reconciled_at=time.time() if reconciled_at is None else reconciled_at
        )
        self._checkpoint(context)
        self._commit(expected, updated)
        return updated

    def tombstone(self, expected: ControlRecord) -> ControlRecord:
        """Called only after independently confirmed Gateway deletion."""
        updated = replace(
            expected, tombstoned=True, protected_version=expected.protected_version + 1
        )
        self._commit_protected(expected, updated)
        return updated

    @timed("aws.policy")
    def load_policy(self, control: ControlRecord) -> dict[str, object]:
        if control.gateway != self.gateway or control.tombstoned or control.policy is None:
            raise StorageError("policy unavailable")
        policy = _decode(self._get(control.policy, limit=16 * 1024 * 1024))
        if policy.get("epoch") != control.policy_epoch:
            raise StorageError("policy epoch mismatch")
        return policy

    @timed("aws.safety")
    def load_safety(self, control: ControlRecord) -> dict[str, str]:
        if control.gateway != self.gateway or control.tombstoned or control.safety is None:
            raise StorageError("safety catalog unavailable")
        raw = _decode(self._get(control.safety, limit=16 * 1024 * 1024))
        if (
            set(raw) != {"version", "gateway", "revision", "tools"}
            or raw["version"] != 1
            or raw["gateway"] != self.gateway.as_dict()
            or raw["revision"] != control.safety_revision
        ):
            raise StorageError("safety catalog revision mismatch")
        mapping = _object(raw["tools"])
        if any(
            not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value)
            for value in mapping.values()
        ):
            raise StorageError("invalid safety schema digest")
        return cast(dict[str, str], mapping)

    def load_snapshot(self, control: ControlRecord) -> IndexSnapshot:
        from concurrent.futures import ThreadPoolExecutor

        from semantic_core.snapshot import (
            SnapshotError,
            _ValidatedTokenizerMismatch,
            load_snapshot,
        )

        if control.gateway != self.gateway or control.tombstoned or control.manifest is None:
            raise StorageError("index not ready")
        with stage("snapshot.manifest"):
            envelope = _decode(self._get(control.manifest, limit=64 * 1024))
        if (
            set(envelope) != {"version", "gateway", "catalog_revision", "build_id", "objects"}
            or _integer(envelope["version"], 1) != 1
            or envelope["gateway"] != self.gateway.as_dict()
            or _integer(envelope["catalog_revision"], 1) != control.catalog_revision
        ):
            raise StorageError("remote manifest identity mismatch")
        build_id = _string(envelope["build_id"])
        references = _object(envelope["objects"])
        if not {"manifest.json", "catalog.json", "bm25.json"} <= references.keys() <= _FILES:
            raise StorageError("invalid snapshot object set")
        objects = {name: ObjectRef.parse(raw) for name, raw in references.items()}
        for reference in objects.values():
            self._check_reference(reference)
        _check_snapshot_sizes({name: reference.bytes for name, reference in objects.items()})
        try:
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)

                def download(item: tuple[str, ObjectRef]) -> None:
                    name, reference = item
                    self._download_snapshot_object(reference, directory / name)

                # 全部引用/大小/预算检查完成后才启动；应用读取缓冲最多三个数据块。
                with (
                    stage("snapshot.objects"),
                    ThreadPoolExecutor(max_workers=_SNAPSHOT_WORKERS) as executor,
                ):
                    # Consume the entire iterator to wait and re-raise each worker error.
                    # nosemgrep: pass-body-range
                    for _ in executor.map(download, objects.items()):
                        pass
                # All downloads and their integrity checks must succeed before loading.
                with stage("snapshot.load"):
                    snapshot = load_snapshot(directory, expected_gateway=self.gateway)
            if (
                snapshot.catalog_revision != control.catalog_revision
                or snapshot.build_id != build_id
            ):
                raise StorageError("snapshot revision mismatch")
            return snapshot
        except _ValidatedTokenizerMismatch as error:
            if (
                error.gateway != self.gateway
                or error.catalog_revision != control.catalog_revision
                or error.build_id != build_id
            ):
                raise StorageError("snapshot revision mismatch") from None
            raise _SnapshotIncompatible("snapshot validation failed: tokenizer mismatch") from None
        except (OSError, SnapshotError):
            raise StorageError("snapshot validation failed") from None


class SnapshotReader:
    """Request-driven immutable swap; safety and policy changes bypass the TTL."""

    def __init__(
        self,
        store: SnapshotStore,
        *,
        check_seconds: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 <= check_seconds <= 30:
            raise ValueError("manifest checks must be at most thirty seconds apart")
        self.store, self.check_seconds, self.clock = store, check_seconds, clock
        self._control: ControlRecord | None = None
        self._snapshot: IndexSnapshot | None = None
        self._checked = float("-inf")

    @timed("snapshot.refresh")
    def current(self, control: ControlRecord) -> IndexSnapshot:
        if control.gateway != self.store.gateway or control.tombstoned:
            raise StorageError("gateway unavailable")
        now = self.clock()
        old = self._control
        if (
            old is None
            or self._snapshot is None
            or old.safety_revision != control.safety_revision
            or old.policy_epoch != control.policy_epoch
            or now - self._checked >= self.check_seconds
        ):
            if old is None or old.manifest != control.manifest or self._snapshot is None:
                loaded = self.store.load_snapshot(control)
                safety = self.store.load_safety(control)
                if any(safety.get(tool.tool_id) != tool.schema_digest for tool in loaded.tools):
                    raise StorageError("snapshot no longer matches current safety catalog")
                self._snapshot = loaded
            self._control, self._checked = control, now
        if self._snapshot is None:
            raise StorageError("index not ready")
        return self._snapshot

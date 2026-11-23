"""快照下载可以并发，但不可变对象校验和读者原子交换必须保持。"""

from __future__ import annotations

import io
import json
import threading
import time
from dataclasses import asdict, replace

import boto3
import pytest
from botocore.session import Session

from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord, canonical_json, digest
from semantic_core.providers import ModelProfile
from semantic_core.snapshot import IndexSnapshot, save_snapshot
from semantic_core.vectors import VectorIndex
from semantic_service.storage import (
    ControlRecord,
    ObjectRef,
    SnapshotReader,
    SnapshotStore,
    StorageError,
)

GW = GatewayIdentity("000000000000", "cn-north-1", "offline-snapshot")
ASSETS = {"manifest.json", "catalog.json", "bm25.json", "parents.json", "vectors.npy"}


class DownloadProbe:
    """用等待中的独立对象观测重叠；所有数据都来自本地内存。"""

    def __init__(self):
        self.objects = {}
        self.calls = []
        self.active = 0
        self.maximum_active = 0
        self.finished_assets = 0
        self.overlap = None
        self.failure_name = None
        self.response_change = None
        self.lock = threading.Lock()

    def put(self, key, data):
        reference = ObjectRef(key, "offline-version", len(data), digest(data))
        self.objects[key] = data
        return reference

    def reset(self):
        self.calls.clear()
        self.active = self.maximum_active = self.finished_assets = 0

    def get_object(self, **kwargs):
        name = kwargs["Key"].rsplit("/", 1)[-1]
        is_asset = name in ASSETS
        with self.lock:
            self.calls.append(name)
            if is_asset:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
                if self.overlap is not None and self.active >= 3:
                    self.overlap.set()
        try:
            if is_asset and self.overlap is not None:
                # 串行版本仍能结束并给出明确的并发数断言失败，不留下阻塞线程。
                self.overlap.wait(timeout=0.2)
                time.sleep(0.02)
            if name == self.failure_name:
                raise RuntimeError("offline worker failure")
            data = self.objects[kwargs["Key"]]
            result = {
                "VersionId": kwargs["VersionId"],
                "ContentLength": len(data),
                "Body": io.BytesIO(data),
            }
            if is_asset and self.response_change is not None:
                self.response_change(result)
            return result
        finally:
            if is_asset:
                with self.lock:
                    self.active -= 1
                    self.finished_assets += 1


def bundle(tmp_path, *, vectors=True):
    probe = DownloadProbe()
    store = SnapshotStore(
        probe, object(), "offline-bucket", "offline-state", "offline-protected", GW
    )
    record = ToolRecord.from_descriptor(
        ToolIdentity(GW, "offline-target", "v1", "orders___lookup"),
        {
            "name": "orders___lookup",
            "description": "查询订单状态",
            "inputSchema": {"type": "object"},
        },
    )
    snapshot = IndexSnapshot.build(
        GW,
        (record,),
        vectors=VectorIndex([[1, 0]], [0], 1, 2) if vectors else None,
        model_profile=ModelProfile("offline", "v1", 2) if vectors else None,
    )
    directory = tmp_path / "snapshot"
    save_snapshot(snapshot, directory)
    references = {
        path.name: asdict(probe.put(store.prefix + path.name, path.read_bytes()))
        for path in sorted(directory.iterdir())
    }
    envelope = {
        "version": 1,
        "gateway": GW.as_dict(),
        "catalog_revision": 1,
        "build_id": snapshot.build_id,
        "objects": references,
    }
    remote = probe.put(store.prefix + "remote.json", canonical_json(envelope))
    safety = probe.put(
        store.prefix + "safety.json",
        canonical_json(
            {
                "version": 1,
                "gateway": GW.as_dict(),
                "revision": 1,
                "tools": {record.tool_id: record.schema_digest},
            }
        ),
    )
    control = ControlRecord(
        GW,
        catalog_revision=1,
        manifest=remote,
        safety=safety,
        safety_revision=1,
        catalog_digest="0" * 64,
    )
    return store, probe, control, snapshot, envelope


def replace_remote(store, probe, control, envelope):
    reference = probe.put(store.prefix + "changed-remote.json", canonical_json(envelope))
    return replace(control, manifest=reference)


@pytest.mark.parametrize("vectors, asset_count", [(False, 3), (True, 5)])
def test_independent_snapshot_downloads_overlap_with_at_most_three_workers(
    tmp_path, monkeypatch, vectors, asset_count
):
    store, probe, control, original, _ = bundle(tmp_path, vectors=vectors)
    probe.overlap = threading.Event()

    def forbidden_client(*args, **kwargs):
        raise AssertionError("snapshot download must reuse the existing S3 client")

    monkeypatch.setattr(boto3, "client", forbidden_client)
    monkeypatch.setattr(Session, "create_client", forbidden_client)
    loaded = store.load_snapshot(control)

    assert loaded.tools == original.tools
    assert loaded.model_profile == original.model_profile
    assert probe.calls[0] == "remote.json"
    assert len(probe.calls) == asset_count + 1
    assert 2 <= probe.maximum_active <= 3
    assert probe.finished_assets == asset_count
    assert probe.active == 0


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(version=2),
        lambda value: value.update(gateway={**GW.as_dict(), "gateway_id": "another"}),
        lambda value: value.update(catalog_revision=2),
        lambda value: value["objects"].update({"../escape": value["objects"]["bm25.json"]}),
        lambda value: value["objects"].pop("manifest.json"),
    ],
)
def test_invalid_remote_manifest_starts_no_asset_downloads(tmp_path, change):
    store, probe, control, _, envelope = bundle(tmp_path)
    change(envelope)
    control = replace_remote(store, probe, control, envelope)

    with pytest.raises(StorageError):
        store.load_snapshot(control)

    assert probe.calls == ["changed-remote.json"]
    assert probe.finished_assets == 0


def test_all_object_references_are_validated_before_starting_downloads(tmp_path):
    store, probe, control, _, envelope = bundle(tmp_path)
    # 故意让坏引用排在最后，避免只验证第一个引用的实现偶然通过。
    envelope["objects"]["vectors.npy"]["sha256"] = "invalid"
    control = replace_remote(store, probe, control, envelope)

    with pytest.raises(StorageError, match="digest"):
        store.load_snapshot(control)

    assert probe.calls == ["changed-remote.json"]


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda result: result.update(VersionId="different-version"), "metadata"),
        (lambda result: result.update(ContentLength=1), "metadata"),
        (lambda result: result.update(Body=io.BytesIO(b"corrupted")), "integrity"),
    ],
)
def test_each_concurrent_object_keeps_version_length_and_hash_checks(tmp_path, change, message):
    store, probe, control, _, _ = bundle(tmp_path)
    probe.overlap = threading.Event()
    probe.response_change = change

    with pytest.raises(StorageError, match=message):
        store.load_snapshot(control)

    assert probe.active == 0


def test_worker_failure_propagates_without_replacing_resident_snapshot(tmp_path):
    store, probe, control, _, envelope = bundle(tmp_path)
    reader = SnapshotReader(store, check_seconds=0)
    resident = reader.current(control)
    changed = replace_remote(store, probe, control, envelope)
    probe.reset()
    probe.overlap = threading.Event()
    probe.failure_name = "bm25.json"

    with pytest.raises(StorageError, match="object unavailable"):
        reader.current(changed)

    assert reader._snapshot is resident
    assert reader._control is control
    assert "safety.json" not in probe.calls
    assert probe.active == 0


def test_late_safety_failure_cannot_replace_resident_snapshot(tmp_path):
    store, probe, control, _, envelope = bundle(tmp_path)
    reader = SnapshotReader(store, check_seconds=0)
    resident = reader.current(control)
    changed = replace_remote(store, probe, control, envelope)
    probe.reset()
    probe.overlap = threading.Event()
    # 下载和索引恢复成功后，仍需经过原有 safety 内容校验才能交换。
    safety = json.loads(probe.objects[control.safety.key])
    safety["tools"] = {}
    changed = replace(
        changed, safety=probe.put(store.prefix + "changed-safety.json", canonical_json(safety))
    )

    with pytest.raises(StorageError, match="current safety catalog"):
        reader.current(changed)

    assert probe.finished_assets == 5
    assert probe.calls[-1] == "changed-safety.json"
    assert reader._snapshot is resident
    assert reader._control is control

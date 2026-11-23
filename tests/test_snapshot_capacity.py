"""完整单 Gateway 容量：500 工具、1024 维、每工具 3/6 条已批准例句。"""

from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pytest
from test_service_storage import DynamoStub

from semantic_core.catalog import canonical_json, digest
from semantic_core.providers import ModelProfile
from semantic_core.snapshot import IndexSnapshot, save_snapshot
from semantic_core.vectors import VectorIndex
from semantic_eval.dataset import load_dataset
from semantic_eval.harness import _records
from semantic_service.storage import ObjectRef, SnapshotReader, SnapshotStore


class DiskObjects:
    """模拟版本化对象；不把全部对象及历史上传 body 常驻测试进程。"""

    def __init__(self, directory):
        self.directory = directory
        self.objects = {}

    def put_object(self, **kwargs):
        key = kwargs["Key"]
        assert key not in self.objects
        version = f"offline-{len(self.objects)}"
        path = self.directory / version
        path.write_bytes(kwargs["Body"])
        self.objects[key] = (version, path)
        return {"VersionId": version}

    def get_object(self, **kwargs):
        version, path = self.objects[kwargs["Key"]]
        assert version == kwargs["VersionId"]
        return {
            "VersionId": version,
            "ContentLength": path.stat().st_size,
            "Body": path.open("rb"),
        }

    def reference(self, key, data):
        version = self.put_object(Key=key, Body=data)["VersionId"]
        return ObjectRef(key, version, len(data), digest(data))


@pytest.fixture(scope="module", params=[3, 6], ids=["500-tools-3-examples", "500-tools-6-examples"])
def capacity_snapshot(request):
    dataset = load_dataset(Path(__file__).resolve().parents[1] / "data/synthetic-v1.json")
    tools = tuple(replace(tool, examples=tool.examples[: request.param]) for tool in dataset.tools)
    records, _ = _records(tools, examples=True, gateway_override="capacity-single")
    count = len(records) * (1 + request.param)
    values = np.random.default_rng(20261010).standard_normal((count, 1024), dtype=np.float32)
    parents = np.repeat(np.arange(len(records)), 1 + request.param)
    snapshot = IndexSnapshot.build(
        records[0].identity.gateway,
        records,
        catalog_revision=1,
        build_id=f"capacity-{request.param}",
        vectors=VectorIndex(values, parents, len(records), 1024),
        model_profile=ModelProfile("capacity-fixture", "v1", 1024),
    )
    assert len(snapshot.tools) == 500
    assert all(len(tool.examples) == request.param for tool in snapshot.tools)
    assert snapshot.vectors is not None
    assert snapshot.vectors.vectors.shape == (count, 1024)
    return snapshot


def base_store(tmp_path, snapshot):
    store = SnapshotStore(
        DiskObjects(tmp_path),
        DynamoStub(),
        "offline-bucket",
        "offline-state",
        "offline-protected",
        snapshot.gateway,
    )
    initial = store.initialize(
        {"version": 1, "epoch": 1, "principals": [], "tools": {}}, policy_epoch=1
    )
    records = tuple(replace(tool, examples=()) for tool in snapshot.tools)
    base = IndexSnapshot.build(snapshot.gateway, records, catalog_revision=1, build_id="base")
    return store, store.publish_base(initial, base, records)


def assert_capacity(actual, expected):
    assert actual.tools == expected.tools
    assert actual.model_profile == expected.model_profile
    assert actual.vectors is not None and expected.vectors is not None
    np.testing.assert_array_equal(actual.vectors.vectors, expected.vectors.vectors)
    np.testing.assert_array_equal(actual.vectors.parent_indices, expected.vectors.parent_indices)


def test_reader_accepts_full_enhanced_capacity_and_replaces_old_snapshot(
    tmp_path, capacity_snapshot
):
    snapshot = capacity_snapshot
    store, control = base_store(tmp_path, snapshot)
    reader = SnapshotReader(store, check_seconds=0)
    old = reader.current(control)
    directory = tmp_path / "snapshot"
    save_snapshot(snapshot, directory)
    references = {
        path.name: asdict(
            store.s3.reference(store.prefix + "capacity/" + path.name, path.read_bytes())
        )
        for path in sorted(directory.iterdir())
    }
    envelope = {
        "version": 1,
        "gateway": snapshot.gateway.as_dict(),
        "catalog_revision": 1,
        "build_id": snapshot.build_id,
        "objects": references,
    }
    remote = store.s3.reference(store.prefix + "capacity/remote.json", canonical_json(envelope))
    changed = replace(control, manifest=remote, manifest_version=control.manifest_version + 1)
    loaded = reader.current(changed)
    assert loaded is not old
    assert reader._snapshot is loaded
    assert_capacity(loaded, snapshot)


def test_enhancer_can_publish_and_read_back_full_capacity(tmp_path, capacity_snapshot):
    store, control = base_store(tmp_path, capacity_snapshot)
    published = store.publish_enhanced(control, capacity_snapshot)
    assert published.manifest_version == control.manifest_version + 1
    assert store.read_control() == published
    assert_capacity(SnapshotReader(store).current(published), capacity_snapshot)

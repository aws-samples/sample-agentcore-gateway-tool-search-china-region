"""远端快照必须先准入，再以有界缓冲下载；三个生产入口共用同一保护。"""

from __future__ import annotations

import io
import json
import threading
import time
from dataclasses import asdict, replace

import pytest
from test_coldstart_snapshot import ASSETS, bundle, replace_remote

from semantic_core.catalog import canonical_json
from semantic_core.providers import MockEmbeddingProvider, MockLLMProvider, RequestContext
from semantic_service.enhancer import CatalogEnhancer, EnhancementRepository
from semantic_service.storage import SnapshotReader, StorageError

CHUNK_BYTES = 256 * 1024


def load_via(callsite, store, control, snapshot, monkeypatch):
    if callsite == "reader":
        return SnapshotReader(store).current(control)
    if callsite == "publish_enhanced":
        return store.publish_enhanced(control, snapshot)
    monkeypatch.setattr(store, "read_control", lambda: control)
    enhancer = CatalogEnhancer(
        store,
        EnhancementRepository(store),
        MockEmbeddingProvider(2),
        MockLLMProvider(),
        prompt_version="mock-queries-v1",
    )
    return enhancer.run(RequestContext("offline", time.monotonic() + 10))


@pytest.mark.parametrize("callsite", ["reader", "publish_enhanced", "enhancer_run"])
@pytest.mark.parametrize("damage", ["namespace", "per_file_size", "total_budget"])
def test_all_preflight_checks_reject_before_any_asset_get(tmp_path, monkeypatch, callsite, damage):
    store, probe, control, snapshot, envelope = bundle(tmp_path)
    references = envelope["objects"]
    if damage == "namespace":
        references["vectors.npy"]["key"] = "outside/namespace/vectors.npy"
    elif damage == "per_file_size":
        references["manifest.json"]["bytes"] = 64 * 1024 + 1
    else:
        # 每份均低于格式上限，但合计已超出恢复工作集预算；不分配对应大对象。
        references["catalog.json"]["bytes"] = 4 * 1024 * 1024
        references["bm25.json"]["bytes"] = 4 * 1024 * 1024
    changed = replace(
        control,
        manifest=probe.put(store.prefix + "next-remote.json", canonical_json(envelope)),
    )
    with pytest.raises(StorageError):
        load_via(callsite, store, changed, snapshot, monkeypatch)
    assert not set(probe.calls) & ASSETS, probe.calls


def padded_bundle(tmp_path):
    store, probe, control, snapshot, envelope = bundle(tmp_path)
    local_manifest_key = envelope["objects"]["manifest.json"]["key"]
    local_manifest = json.loads(probe.objects[local_manifest_key])
    # 有效 JSON 后的空白保留相同索引，文件足够大以区分整块/分块读取。
    for name in ("catalog.json", "bm25.json"):
        key = envelope["objects"][name]["key"]
        data = probe.objects[key] + b" " * (CHUNK_BYTES + 17)
        reference = probe.put(key, data)
        envelope["objects"][name] = asdict(reference)
        local_manifest["objects"][name] = {
            "bytes": reference.bytes,
            "sha256": reference.sha256,
        }
    envelope["objects"]["manifest.json"] = asdict(
        probe.put(local_manifest_key, canonical_json(local_manifest))
    )
    return store, probe, replace_remote(store, probe, control, envelope), snapshot


def test_download_read_size_is_bounded_and_short_reads_are_valid(tmp_path, monkeypatch):
    store, probe, control, original = padded_bundle(tmp_path)
    get_object = probe.get_object
    reads = []
    bodies = []

    class ShortBody(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            assert 0 < size <= CHUNK_BYTES, "unbounded object read"
            return super().read(min(size, 997))

    def response(**kwargs):
        result = get_object(**kwargs)
        if kwargs["Key"].rsplit("/", 1)[-1] in ASSETS:
            result["Body"].close()
            body = ShortBody(probe.objects[kwargs["Key"]])
            bodies.append(body)
            result["Body"] = body
        return result

    monkeypatch.setattr(probe, "get_object", response)
    loaded = store.load_snapshot(control)
    assert loaded.tools == original.tools
    assert max(reads) <= CHUNK_BYTES
    assert len(reads) > 20
    assert all(body.closed for body in bodies)


@pytest.mark.parametrize("damage", ["early_eof", "extra_tail", "oversized_chunk", "read_error"])
def test_bad_stream_keeps_old_snapshot_and_closes_all_bodies(tmp_path, monkeypatch, damage):
    store, probe, control, _ = padded_bundle(tmp_path)
    reader = SnapshotReader(store, check_seconds=0)
    resident = reader.current(control)
    checked = reader._checked
    envelope = json.loads(probe.objects[control.manifest.key])
    changed = replace(
        control,
        manifest=probe.put(store.prefix + "next-remote.json", canonical_json(envelope)),
    )
    probe.reset()
    get_object = probe.get_object
    bodies = []
    entered = threading.Event()
    released = threading.Event()
    workers = set()

    class Stream(io.BytesIO):
        def __init__(self, data, name):
            super().__init__(data)
            self.name = name
            self.reads = 0

        def read(self, size=-1):
            workers.add(threading.current_thread())
            self.reads += 1
            if self.name == "catalog.json" and self.reads == 1:
                entered.set()
                assert released.wait(timeout=2)
            if self.name == "bm25.json":
                assert entered.wait(timeout=2)
                released.set()
                if damage == "read_error" and self.reads == 2:
                    raise OSError("offline stream read error")
                if damage == "oversized_chunk":
                    return b"x" * (size + 1)
                if damage == "early_eof":
                    return b""
            return super().read(size)

    def response(**kwargs):
        result = get_object(**kwargs)
        name = kwargs["Key"].rsplit("/", 1)[-1]
        if name in ASSETS:
            result["Body"].close()
            data = probe.objects[kwargs["Key"]]
            if name == "bm25.json" and damage == "extra_tail":
                data += b"x"
            body = Stream(data, name)
            bodies.append(body)
            result["Body"] = body
        return result

    monkeypatch.setattr(probe, "get_object", response)
    with pytest.raises(StorageError):
        reader.current(changed)
    assert reader._snapshot is resident
    assert reader._control is control
    assert reader._checked == checked
    assert all(body.closed for body in bodies)
    assert all(not worker.is_alive() for worker in workers)
    assert "safety.json" not in probe.calls


@pytest.mark.parametrize("damage", ["per_file_size", "total_budget"])
def test_publisher_cannot_upload_a_snapshot_that_readers_would_reject(
    tmp_path, monkeypatch, damage
):
    from semantic_core import snapshot as local

    store, _, _, snapshot, _ = bundle(tmp_path)
    uploads = []

    def saved(_snapshot, directory):
        directory.mkdir()
        for name in ("manifest.json", "catalog.json", "bm25.json"):
            size = 4 * 1024 * 1024 if damage == "total_budget" and name != "manifest.json" else 8
            if damage == "per_file_size" and name == "manifest.json":
                size = 64 * 1024 + 1
            with (directory / name).open("wb") as stream:
                stream.truncate(size)

    def forbidden_upload(*args, **kwargs):
        uploads.append(args)
        raise AssertionError("inadmissible snapshot uploaded")

    monkeypatch.setattr(local, "save_snapshot", saved)
    monkeypatch.setattr(store, "_put", forbidden_upload)
    with pytest.raises(StorageError):
        store._save(snapshot)
    assert uploads == []

"""仅用本地存储替身验证分词配置迁移、增强隔离及并发发布。"""

import io
import json
from dataclasses import replace

import numpy as np
import pytest
from test_service_catalog import GW, Targets, client, context, descriptor, registry
from test_service_storage import DynamoStub, S3Stub, initialize

from semantic_core import tokenization
from semantic_core.bm25 import BM25Index
from semantic_core.catalog import canonical_json, digest
from semantic_core.providers import MockEmbeddingProvider, ModelProfile
from semantic_core.snapshot import IndexSnapshot
from semantic_core.tokenization import ChineseTokenizer
from semantic_core.vectors import VectorIndex
from semantic_service.enhancer import CatalogEnhancer, EnhancementRepository
from semantic_service.indexer import CatalogIndexer
from semantic_service.storage import ObjectRef, PublishConflict, SnapshotStore, StorageError


@pytest.fixture
def store():
    return SnapshotStore(S3Stub(), DynamoStub(), "bucket-test", "table-test", "table-protected", GW)


def _current_config():
    return IndexSnapshot.build(GW, ()).bm25.tokenizer.config


def _tools():
    return registry().materialize([descriptor("orders___find")])


def _with_profile(snapshot, config):
    return replace(
        snapshot,
        bm25=BM25Index.build([tool.fields() for tool in snapshot.tools], ChineseTokenizer(config)),
    )


def _publish_profile(store, config):
    tools = _tools()
    initial = initialize(store)
    snapshot = _with_profile(IndexSnapshot.build(GW, tools), config)
    return store.publish_base(initial, snapshot, tools), snapshot


def _indexer(store):
    page = {"tools": [descriptor("orders___find")]}
    return CatalogIndexer(store, registry(), client([page, page]), Targets())


def _publish_vector_snapshot(store):
    tools = _tools()
    base = IndexSnapshot.build(GW, tools, build_id="recovery-base")
    active = store.publish_base(initialize(store), base, tools)
    snapshot = IndexSnapshot.build(
        GW,
        tools,
        build_id="recovery-enhanced",
        vectors=VectorIndex([[1.0, 0.0]], [0], 1, 2),
        model_profile=ModelProfile("recovery-fixture", "v1", 2),
    )
    active = store.publish_enhanced(active, snapshot)
    assert store.load_snapshot(active).vectors is not None
    return active, snapshot


def _replace_snapshot_asset_with_valid_hashes(store, active, name, changed):
    reference = active.manifest
    assert reference is not None
    remote_version, remote_blob = store.s3.objects[reference.key]
    envelope = json.loads(remote_blob)
    object_ref = envelope["objects"][name]
    store.s3.objects[object_ref["key"]] = (object_ref["version_id"], changed)
    object_ref.update(bytes=len(changed), sha256=digest(changed))
    manifest_ref = envelope["objects"]["manifest.json"]
    _, manifest_blob = store.s3.objects[manifest_ref["key"]]
    manifest = json.loads(manifest_blob)
    manifest["objects"][name].update(bytes=len(changed), sha256=digest(changed))
    manifest_blob = canonical_json(manifest)
    store.s3.objects[manifest_ref["key"]] = (manifest_ref["version_id"], manifest_blob)
    manifest_ref.update(bytes=len(manifest_blob), sha256=digest(manifest_blob))
    remote_blob = canonical_json(envelope)
    store.s3.objects[reference.key] = (remote_version, remote_blob)
    active = replace(
        active,
        manifest=ObjectRef(reference.key, remote_version, len(remote_blob), digest(remote_blob)),
    )
    store.dynamodb.items[(store.table, store.key)] = store._item(active)
    return active


@pytest.mark.parametrize(
    "field",
    ["synonyms_version", "synonyms", "dictionary_version", "dictionary"],
)
def test_same_catalog_rebuilds_for_profile_change_then_only_reconciles(store, field):
    current = _current_config()
    changes = {
        "synonyms_version": current.synonyms_version + "-legacy",
        "synonyms": (*current.synonyms, ("legacyalias", ("legacyterm",))),
        "dictionary_version": current.dictionary_version + "-legacy",
        "dictionary": (*current.dictionary, ("旧词条", 100)),
    }
    legacy = replace(current, **{field: changes[field]})
    active, original = _publish_profile(store, legacy)
    original_data = original.bm25.to_dict()
    original_scores = original.bm25.masked_scores("legacyalias 查找订单", [True])

    assert _indexer(store).run(context()) == "published"

    rebuilt_control = store.read_control()
    rebuilt = store.load_snapshot(rebuilt_control)
    assert rebuilt.bm25.tokenizer.config == current
    assert rebuilt.catalog_revision == active.catalog_revision + 1
    assert rebuilt_control.catalog_digest == active.catalog_digest
    assert rebuilt_control.manifest_version == active.manifest_version + 1
    assert rebuilt.build_id != original.build_id
    assert _indexer(store).run(context()) == "reconciled"
    assert store.read_control().manifest == rebuilt_control.manifest
    assert store.read_control().catalog_revision == rebuilt_control.catalog_revision
    historical = store.load_snapshot(active)
    assert historical.bm25.to_dict() == original_data
    np.testing.assert_array_equal(
        historical.bm25.masked_scores("legacyalias 查找订单", [True]), original_scores
    )


def test_enhancer_preserves_legacy_profile_and_scores_within_catalog_revision(store):
    legacy = replace(
        _current_config(),
        synonyms_version="legacy-profile",
        synonyms=(("find", ("legacyalias",)),),
    )
    active, original = _publish_profile(store, legacy)
    original_scores = original.bm25.masked_scores("legacyalias", [True])
    enhancer = CatalogEnhancer(store, EnhancementRepository(store), MockEmbeddingProvider(4))

    assert enhancer.run(context()) == "published"

    enhanced = store.load_snapshot(store.read_control())
    assert enhanced.bm25.tokenizer.config == legacy
    assert enhanced.catalog_revision == active.catalog_revision
    assert enhanced.vectors is not None
    assert enhanced.bm25.to_dict() == original.bm25.to_dict()
    np.testing.assert_array_equal(
        enhanced.bm25.masked_scores("legacyalias", [True]), original_scores
    )
    assert enhancer.run(context()) == "unchanged"


def test_enhancement_cannot_change_profile_without_new_catalog_revision(store):
    active, original = _publish_profile(store, _current_config())
    changed = replace(_current_config(), synonyms_version="unexpected-profile")
    incompatible = _with_profile(replace(original, build_id="incompatible"), changed)

    with pytest.raises(StorageError, match="tokenizer"):
        store.publish_enhanced(active, incompatible)

    assert store.read_control() == active
    assert store.load_snapshot(active).bm25.to_dict() == original.bm25.to_dict()


def test_old_enhancer_cannot_overwrite_rebuilt_profile(store):
    legacy = replace(_current_config(), synonyms_version="legacy-profile")
    active, original = _publish_profile(store, legacy)
    stale_enhancement = replace(original, build_id="stale-enhancement")
    assert _indexer(store).run(context()) == "published"
    rebuilt = store.read_control()

    with pytest.raises(PublishConflict):
        store.publish_enhanced(active, stale_enhancement)

    assert store.read_control() == rebuilt
    assert store.load_snapshot(rebuilt).bm25.tokenizer.config == _current_config()


def test_rebuild_loses_cas_to_another_complete_publication(store, monkeypatch):
    legacy = replace(_current_config(), synonyms_version="legacy-profile")
    active, _ = _publish_profile(store, legacy)
    publish_base = store.publish_base
    competing = None

    def publish_competitor_then_rebuild(expected, snapshot, safety_tools, **kwargs):
        nonlocal competing
        competing = publish_base(
            active, replace(snapshot, build_id="competing-rebuild"), safety_tools, **kwargs
        )
        return publish_base(expected, snapshot, safety_tools, **kwargs)

    monkeypatch.setattr(store, "publish_base", publish_competitor_then_rebuild)

    with pytest.raises(PublishConflict):
        _indexer(store).run(context())

    assert competing is not None
    assert store.read_control() == competing
    assert store.load_snapshot(competing).build_id == "competing-rebuild"


@pytest.mark.parametrize("upgrade", ["dictionary_digest", "jieba_version"])
def test_unreadable_old_tokenizer_profile_rebuilds_and_subsequent_polls_reconcile(
    store, monkeypatch, upgrade
):
    active, _ = _publish_profile(store, _current_config())
    old_objects = dict(store.s3.objects)
    if upgrade == "dictionary_digest":
        # 仅模拟新运行时的兼容性标记；不修改依赖或既有快照对象。
        monkeypatch.setattr(tokenization, "_base_dictionary_digest", lambda: "f" * 64)
        monkeypatch.setattr(tokenization, "BASE_DICTIONARY_SHA256", "f" * 64)
    else:
        original_version = tokenization.importlib.metadata.version
        monkeypatch.setattr(tokenization, "_JIEBA_VERSION", "future-fixture")
        monkeypatch.setattr(
            tokenization.importlib.metadata,
            "version",
            lambda name: "future-fixture" if name == "jieba" else original_version(name),
        )
    with pytest.raises(StorageError, match="snapshot validation failed"):
        store.load_snapshot(active)

    observed = []
    for _ in range(3):
        try:
            observed.append(_indexer(store).run(context()))
        except StorageError as error:
            observed.append(f"{type(error).__name__}: {error}")
    assert observed == ["published", "reconciled", "reconciled"]

    rebuilt = store.read_control()
    assert rebuilt.catalog_revision == active.catalog_revision + 1
    assert rebuilt.catalog_digest == active.catalog_digest
    assert store.load_snapshot(rebuilt).bm25.tokenizer.config == _current_config()
    assert all(store.s3.objects[key] == value for key, value in old_objects.items())


@pytest.mark.parametrize("error_type", [StorageError, ValueError])
def test_unclassified_snapshot_load_failure_never_triggers_rebuild(store, monkeypatch, error_type):
    active, original = _publish_vector_snapshot(store)
    load_snapshot = store.load_snapshot
    old_objects = dict(store.s3.objects)
    old_items = dict(store.dynamodb.items)

    def reject_old_snapshot(control):
        if control.manifest == active.manifest:
            raise error_type("old snapshot cannot be decoded")
        return load_snapshot(control)

    monkeypatch.setattr(store, "load_snapshot", reject_old_snapshot)
    with pytest.raises(error_type, match="old snapshot cannot be decoded"):
        _indexer(store).run(context())
    assert store.read_control() == active
    assert store.s3.objects == old_objects
    assert store.dynamodb.items == old_items
    np.testing.assert_array_equal(load_snapshot(active).vectors.vectors, original.vectors.vectors)


@pytest.mark.parametrize(
    "failure",
    [
        "equal_length_bm25_corruption",
        "transient_get",
        "remote_manifest_identity",
        "local_manifest_identity",
        "local_identity_and_incompatible_tokenizer",
        "statistics_and_incompatible_tokenizer",
        "vectors_and_incompatible_tokenizer",
        "invalid_config_and_incompatible_tokenizer",
    ],
)
def test_snapshot_validation_or_read_failure_preserves_active_vectors(store, monkeypatch, failure):
    active, original = _publish_vector_snapshot(store)
    reference = active.manifest
    assert reference is not None
    version, blob = store.s3.objects[reference.key]
    envelope = json.loads(blob)
    vector_key = envelope["objects"]["vectors.npy"]["key"]
    bm25_key = envelope["objects"]["bm25.json"]["key"]
    if failure == "equal_length_bm25_corruption":
        bm25_version, bm25_blob = store.s3.objects[bm25_key]
        corrupted = bm25_blob.replace(b"bm25-v1", b"bm25-v9", 1)
        assert corrupted != bm25_blob and len(corrupted) == len(bm25_blob)
        store.s3.objects[bm25_key] = (bm25_version, corrupted)
    elif failure == "transient_get":
        get_object = store.s3.get_object
        failed = False

        def fail_one_get(**kwargs):
            nonlocal failed
            if kwargs["Key"] == bm25_key and not failed:
                failed = True
                raise RuntimeError("temporary object read failure")
            return get_object(**kwargs)

        monkeypatch.setattr(store.s3, "get_object", fail_one_get)
    elif failure in {
        "statistics_and_incompatible_tokenizer",
        "vectors_and_incompatible_tokenizer",
        "invalid_config_and_incompatible_tokenizer",
    }:
        if failure == "vectors_and_incompatible_tokenizer":
            _, vector_blob = store.s3.objects[vector_key]
            values = np.load(io.BytesIO(vector_blob), allow_pickle=False)
            values[0, 0] = 2.0
            output = io.BytesIO()
            np.save(output, values, allow_pickle=False)
            active = _replace_snapshot_asset_with_valid_hashes(
                store, active, "vectors.npy", output.getvalue()
            )
        else:
            _, bm25_blob = store.s3.objects[bm25_key]
            value = json.loads(bm25_blob)
            if failure == "statistics_and_incompatible_tokenizer":
                value["average_length"] += 1.0
            else:
                value["tokenizer"]["config"]["synonyms"] = [["create", ["create"]]]
            active = _replace_snapshot_asset_with_valid_hashes(
                store, active, "bm25.json", canonical_json(value)
            )
        monkeypatch.setattr(tokenization, "_base_dictionary_digest", lambda: "f" * 64)
        monkeypatch.setattr(tokenization, "BASE_DICTIONARY_SHA256", "f" * 64)
    else:
        if failure == "remote_manifest_identity":
            envelope["gateway"]["gateway_id"] = "unexpected-gateway"
        else:
            envelope["build_id"] = "unexpected-build"
        # 绑定新摘要，让本次错误确实来自身份校验，而非对象校验和。
        changed = canonical_json(envelope)
        store.s3.objects[reference.key] = (version, changed)
        active = replace(
            active,
            manifest=ObjectRef(reference.key, version, len(changed), digest(changed)),
        )
        store.dynamodb.items[(store.table, store.key)] = store._item(active)
        if failure == "local_identity_and_incompatible_tokenizer":
            monkeypatch.setattr(tokenization, "_base_dictionary_digest", lambda: "f" * 64)
            monkeypatch.setattr(tokenization, "BASE_DICTIONARY_SHA256", "f" * 64)
    old_objects = dict(store.s3.objects)
    old_items = dict(store.dynamodb.items)
    previous_s3_calls = len(store.s3.calls)
    previous_dynamo_calls = len(store.dynamodb.calls)

    with pytest.raises(StorageError):
        _indexer(store).run(context())

    assert store.read_control() == active
    assert store.s3.objects == old_objects
    assert store.dynamodb.items == old_items
    assert store.s3.objects[vector_key] == old_objects[vector_key]
    assert all(kind == "get" for kind, _ in store.s3.calls[previous_s3_calls:])
    assert all(kind == "get" for kind, _ in store.dynamodb.calls[previous_dynamo_calls:])
    if failure == "transient_get":
        # 一次失败解除后，原有向量快照仍可读取，无须先重建。
        np.testing.assert_array_equal(
            store.load_snapshot(active).vectors.vectors, original.vectors.vectors
        )


@pytest.mark.parametrize("field", ["version", "catalog_revision"])
@pytest.mark.parametrize("invalid_value", [True, 1.0], ids=["boolean", "float"])
def test_remote_identity_types_cannot_request_tokenizer_recovery(
    store, monkeypatch, field, invalid_value
):
    active, _ = _publish_vector_snapshot(store)
    reference = active.manifest
    assert reference is not None
    version, blob = store.s3.objects[reference.key]
    envelope = json.loads(blob)
    vector_key = envelope["objects"]["vectors.npy"]["key"]
    envelope[field] = invalid_value
    changed = canonical_json(envelope)
    store.s3.objects[reference.key] = (version, changed)
    active = replace(
        active,
        manifest=ObjectRef(reference.key, version, len(changed), digest(changed)),
    )
    store.dynamodb.items[(store.table, store.key)] = store._item(active)
    # 有效的旧词典差异不得使不合法的远程身份字段获得迁移资格。
    monkeypatch.setattr(tokenization, "_base_dictionary_digest", lambda: "f" * 64)
    monkeypatch.setattr(tokenization, "BASE_DICTIONARY_SHA256", "f" * 64)
    old_objects = dict(store.s3.objects)
    old_items = dict(store.dynamodb.items)
    previous_s3_calls = len(store.s3.calls)
    previous_dynamo_calls = len(store.dynamodb.calls)

    with pytest.raises(StorageError):
        _indexer(store).run(context())

    assert store.read_control() == active
    assert store.s3.objects == old_objects
    assert store.dynamodb.items == old_items
    assert store.s3.objects[vector_key] == old_objects[vector_key]
    assert all(kind == "get" for kind, _ in store.s3.calls[previous_s3_calls:])
    assert all(kind == "get" for kind, _ in store.dynamodb.calls[previous_dynamo_calls:])


@pytest.mark.parametrize("error_type", [StorageError, ValueError])
@pytest.mark.parametrize(
    "stage", ["read_control", "list_tools", "validate_registry", "reconcile", "publish_base"]
)
def test_snapshot_recovery_does_not_swallow_failures_outside_loading(
    store, monkeypatch, error_type, stage
):
    config = _current_config()
    if stage == "publish_base":
        config = replace(config, synonyms_version="legacy-profile")
    active, _ = _publish_profile(store, config)
    original_read_control = store.read_control
    original_objects = dict(store.s3.objects)
    indexer = _indexer(store)
    owner = (
        indexer.mcp
        if stage == "list_tools"
        else indexer.targets
        if stage == "validate_registry"
        else store
    )

    def fail(*args, **kwargs):
        raise error_type("failure outside snapshot loading")

    monkeypatch.setattr(owner, stage, fail)
    with pytest.raises(error_type, match="failure outside snapshot loading"):
        indexer.run(context())
    assert original_read_control() == active
    assert store.s3.objects == original_objects

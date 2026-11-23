"""Snapshot corruption is a hard failure, separate from model availability."""

import hashlib
import io
import json
from dataclasses import replace

import numpy as np
import pytest

from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord, canonical_json
from semantic_core.providers import ModelProfile
from semantic_core.snapshot import IndexSnapshot, SnapshotError, load_snapshot, save_snapshot
from semantic_core.vectors import VectorIndex

GATEWAY = GatewayIdentity("000000000000", "cn-northwest-1", "fixture")
PROFILE = ModelProfile("fixture-model", "1", 2)


def fixture(vector=True):
    descriptor = {
        "name": "orders___cancelOrder",
        "description": "取消未发货订单",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "订单编号",
                    "default": "not-for-summary",
                }
            },
            "required": ["order_id"],
        },
        "annotations": {"readOnlyHint": False},
        "outputSchema": {"type": "object"},
        "title": "取消订单",
        "_meta": {"reviewed": True},
    }
    tool = ToolRecord.from_descriptor(
        ToolIdentity(GATEWAY, "target", "incarnation1", descriptor["name"]),
        descriptor,
        ("取消我的订单",),
    )
    vectors = VectorIndex([[3, 4], [4, 3]], [0, 0], 1, 2) if vector else None
    return IndexSnapshot.build(
        GATEWAY, [tool], vectors=vectors, model_profile=PROFILE if vector else None
    )


def rewrite_manifest(path, change):
    manifest = json.loads((path / "manifest.json").read_text())
    change(manifest)
    (path / "manifest.json").write_bytes(canonical_json(manifest))


def update_hash(path, name):
    data = (path / name).read_bytes()
    rewrite_manifest(
        path,
        lambda m: m["objects"][name].update(
            sha256=hashlib.sha256(data).hexdigest(), bytes=len(data)
        ),
    )


def test_round_trip_is_lossless_readonly_and_statistics_fixed(tmp_path):
    original = fixture()
    path = tmp_path / "snapshot"
    save_snapshot(original, path)
    loaded = load_snapshot(path, expected_gateway=GATEWAY, expected_model=PROFILE)
    assert loaded.tools == original.tools
    assert loaded.bm25.to_dict() == original.bm25.to_dict()
    np.testing.assert_array_equal(loaded.vectors.vectors, original.vectors.vectors)
    assert not loaded.vectors.vectors.flags.writeable
    assert loaded.model_profile == PROFILE
    assert loaded.catalog_revision == original.catalog_revision
    assert "not-for-summary" not in loaded.tools[0].summary
    assert "not-for-summary" in loaded.tools[0].descriptor_json
    copy = loaded.tools[0].descriptor
    copy["name"] = "mutated"
    assert loaded.tools[0].name == "orders___cancelOrder"


def test_base_and_empty_catalog_are_legal(tmp_path):
    for i, snapshot in enumerate([fixture(False), IndexSnapshot.build(GATEWAY, [])]):
        path = tmp_path / str(i)
        save_snapshot(snapshot, path)
        result = load_snapshot(path, expected_gateway=GATEWAY)
        assert result.vectors is None
        assert result.tools == snapshot.tools


def test_existing_snapshot_never_overwritten(tmp_path):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    with pytest.raises(FileExistsError):
        save_snapshot(fixture(False), path)


@pytest.mark.parametrize("filename", ["catalog.json", "bm25.json", "vectors.npy", "parents.json"])
def test_checksum_corruption_fails_closed(tmp_path, filename):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    with (path / filename).open("ab") as handle:
        handle.write(b"corruption")
    with pytest.raises(SnapshotError):
        load_snapshot(path, expected_gateway=GATEWAY)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda m: m.update(schema_version=2),
        lambda m: m.update(unknown="field"),
        lambda m: m["objects"].update({"../escape": {"bytes": 0, "sha256": "0" * 64}}),
        lambda m: m.update(tool_count=200),
        lambda m: m["model_profile"].update(dimension=3),
    ],
)
def test_manifest_contract_rejects_version_shape_path_and_count(tmp_path, mutation):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    rewrite_manifest(path, mutation)
    with pytest.raises(SnapshotError):
        load_snapshot(path, expected_gateway=GATEWAY)


def test_cross_gateway_and_model_revision_rejected(tmp_path):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    with pytest.raises(SnapshotError):
        load_snapshot(path, expected_gateway=replace(GATEWAY, gateway_id="other"))
    with pytest.raises(SnapshotError):
        load_snapshot(path, expected_gateway=GATEWAY, expected_model=replace(PROFILE, revision="2"))


def test_rehashed_nonfinite_vector_is_still_invalid(tmp_path):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    np.save(
        path / "vectors.npy", np.array([[np.nan, 1], [1, 1]], dtype=np.float32), allow_pickle=False
    )
    update_hash(path, "vectors.npy")
    with pytest.raises(SnapshotError):
        load_snapshot(path, expected_gateway=GATEWAY)


def test_rehashed_duplicate_catalog_is_still_invalid(tmp_path):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    catalog = json.loads((path / "catalog.json").read_text())
    catalog.append(catalog[0])
    (path / "catalog.json").write_bytes(canonical_json(catalog))
    update_hash(path, "catalog.json")
    rewrite_manifest(path, lambda m: m.update(tool_count=2))
    with pytest.raises(SnapshotError):
        load_snapshot(path, expected_gateway=GATEWAY)


def test_symlink_assets_rejected(tmp_path):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    asset = path / "catalog.json"
    outside = tmp_path / "outside.json"
    asset.rename(outside)
    asset.symlink_to(outside)
    with pytest.raises(SnapshotError):
        load_snapshot(path, expected_gateway=GATEWAY)


def test_same_name_recreation_has_new_identity_and_no_inherited_acl():
    original = fixture().tools[0]
    recreated = replace(original, identity=replace(original.identity, incarnation="incarnation2"))
    assert recreated.name == original.name
    assert recreated.tool_id != original.tool_id


def test_descriptor_allowlist_and_required_schema_preserved():
    original = fixture().tools[0]
    assert original.descriptor["inputSchema"]["required"] == ["order_id"]
    descriptor = original.descriptor
    descriptor["unknown"] = "forbidden"
    with pytest.raises(ValueError):
        ToolRecord.from_descriptor(original.identity, descriptor)


@pytest.mark.parametrize(
    "header",
    [
        {"descr": "|O", "fortran_order": False, "shape": (2, 2)},
        {"descr": "<f4", "fortran_order": False, "shape": (10**12, 10**12)},
        {"descr": "<f8", "fortran_order": False, "shape": (2, 2)},
        {"descr": "<f4", "fortran_order": True, "shape": (2, 2)},
        {"descr": "<f4", "fortran_order": False, "shape": (4,)},
    ],
)
def test_rehashed_unsafe_array_headers_are_rejected_without_array_load(
    tmp_path, monkeypatch, header
):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    stream = io.BytesIO()
    np.lib.format.write_array_header_1_0(stream, header)
    (path / "vectors.npy").write_bytes(stream.getvalue())
    update_hash(path, "vectors.npy")

    def forbidden(*args, **kwargs):
        pytest.fail("untrusted arrays must not go through np.load")

    monkeypatch.setattr(np, "load", forbidden)
    with pytest.raises(SnapshotError):
        load_snapshot(path, expected_gateway=GATEWAY)


def test_normalized_vector_bytes_round_trip_without_reencoding(tmp_path):
    base = fixture()
    rng = np.random.default_rng(782)
    profile = replace(PROFILE, dimension=17)
    vectors = VectorIndex(rng.standard_normal((50, 17)), [0] * 50, 1, 17)
    original = replace(base, vectors=vectors, model_profile=profile)
    path = tmp_path / "snapshot"
    save_snapshot(original, path)
    loaded = load_snapshot(path, expected_gateway=GATEWAY, expected_model=profile)
    assert loaded.vectors.vectors.tobytes() == vectors.vectors.tobytes()
    with pytest.raises(ValueError):
        loaded.vectors.vectors.setflags(write=True)


@pytest.mark.parametrize("parents", [[0], [0, 1], [False, 0], [0, 0, 0]])
def test_rehashed_parent_count_range_and_boolean_fail_closed(tmp_path, parents):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    (path / "parents.json").write_bytes(canonical_json(parents))
    update_hash(path, "parents.json")
    with pytest.raises(SnapshotError):
        load_snapshot(path, expected_gateway=GATEWAY)


def test_rehashed_bm25_statistics_cannot_override_full_catalog_math(tmp_path):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    lexical = json.loads((path / "bm25.json").read_bytes())
    lexical["average_length"] += 1
    (path / "bm25.json").write_bytes(canonical_json(lexical))
    update_hash(path, "bm25.json")
    with pytest.raises(SnapshotError):
        load_snapshot(path, expected_gateway=GATEWAY)


@pytest.mark.parametrize("digest_field", ["schema_digest", "descriptor_digest"])
def test_rehashed_catalog_still_requires_correct_descriptor_digests(tmp_path, digest_field):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    catalog = json.loads((path / "catalog.json").read_bytes())
    catalog[0][digest_field] = "0" * 64
    (path / "catalog.json").write_bytes(canonical_json(catalog))
    update_hash(path, "catalog.json")
    with pytest.raises(SnapshotError):
        load_snapshot(path, expected_gateway=GATEWAY)


def test_fully_rehashed_catalog_must_still_match_lexical_documents(tmp_path):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    catalog = json.loads((path / "catalog.json").read_bytes())
    catalog[0]["descriptor"]["description"] = "different tool purpose"
    catalog[0]["descriptor_digest"] = hashlib.sha256(
        canonical_json(catalog[0]["descriptor"])
    ).hexdigest()
    (path / "catalog.json").write_bytes(canonical_json(catalog))
    update_hash(path, "catalog.json")
    with pytest.raises(SnapshotError):
        load_snapshot(path, expected_gateway=GATEWAY)


def test_catalog_and_lexical_fields_mismatch_is_rejected_before_save():
    original = fixture()
    changed = ToolRecord.from_descriptor(
        original.tools[0].identity,
        {**original.tools[0].descriptor, "description": "different content"},
    )
    with pytest.raises(SnapshotError):
        replace(original, tools=(changed,))


def test_missing_manifest_and_directory_symlink_fail_closed(tmp_path):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    link = tmp_path / "link"
    link.symlink_to(path, target_is_directory=True)
    with pytest.raises(SnapshotError):
        load_snapshot(link, expected_gateway=GATEWAY)
    (path / "manifest.json").unlink()
    with pytest.raises(SnapshotError):
        load_snapshot(path, expected_gateway=GATEWAY)


def test_manifest_duplicate_json_keys_are_rejected(tmp_path):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    raw = (path / "manifest.json").read_bytes()
    (path / "manifest.json").write_bytes(b'{"schema_version":1,' + raw[1:])
    with pytest.raises(SnapshotError):
        load_snapshot(path, expected_gateway=GATEWAY)

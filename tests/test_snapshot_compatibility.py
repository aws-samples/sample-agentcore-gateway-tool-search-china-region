"""只有完整验证通过的旧基础分词环境差异可交给索引器重建。"""

import json

import numpy as np
import pytest
from test_snapshot import GATEWAY, fixture, update_hash

from semantic_core import tokenization
from semantic_core.bm25 import BM25Index
from semantic_core.catalog import canonical_json
from semantic_core.snapshot import SnapshotError, load_snapshot, save_snapshot
from semantic_core.tokenization import ChineseTokenizer


def _old_profile(path, field="base_dictionary_sha256"):
    data = json.loads((path / "bm25.json").read_text())
    data["tokenizer"][field] = "0" * 64 if field == "base_dictionary_sha256" else "0.41.0"
    (path / "bm25.json").write_bytes(canonical_json(data))
    update_hash(path, "bm25.json")
    return data


@pytest.mark.parametrize("field", ["base_dictionary_sha256", "jieba_version"])
def test_only_validated_old_environment_reports_compatibility_with_identity(tmp_path, field):
    path = tmp_path / "snapshot"
    original = fixture()
    save_snapshot(original, path)
    data = _old_profile(path, field)
    with pytest.raises(ValueError):
        ChineseTokenizer.from_dict(data["tokenizer"])
    with pytest.raises(ValueError):
        BM25Index.from_dict(data)
    with pytest.raises(SnapshotError) as captured:
        load_snapshot(path, expected_gateway=GATEWAY)
    error = captured.value
    assert type(error).__name__ == "_ValidatedTokenizerMismatch"
    assert error.gateway == GATEWAY
    assert error.catalog_revision == original.catalog_revision
    assert error.build_id == original.build_id


@pytest.mark.parametrize(
    "damage",
    [
        "idf",
        "average_length",
        "document_fields",
        "config",
        "unknown_version",
        "hmm",
        "stopwords",
        "bad_digest",
        "bad_jieba_version",
    ],
)
def test_old_environment_does_not_hide_invalid_lexical_data(tmp_path, damage):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    data = _old_profile(path)
    if damage == "idf":
        data["idf"][next(iter(data["idf"]))] += 1
    elif damage == "average_length":
        data["average_length"] += 1
    elif damage == "document_fields":
        data["documents"][0]["description"] = "different catalog text"
    elif damage == "config":
        data["tokenizer"]["config"]["dictionary"] = [["term", -1]]
    elif damage == "unknown_version":
        data["tokenizer"]["config"]["version"] = "tokenizer-v999"
    elif damage == "hmm":
        data["tokenizer"]["hmm"] = True
    elif damage == "stopwords":
        data["tokenizer"]["stopwords"] = ["not"]
    elif damage == "bad_digest":
        data["tokenizer"]["base_dictionary_sha256"] = "not-a-digest"
    else:
        data["tokenizer"]["jieba_version"] = 123
    (path / "bm25.json").write_bytes(canonical_json(data))
    update_hash(path, "bm25.json")
    with pytest.raises(SnapshotError) as captured:
        load_snapshot(path, expected_gateway=GATEWAY)
    assert type(captured.value) is SnapshotError


@pytest.mark.parametrize("failure", ["package_version", "dictionary_resource"])
def test_runtime_failure_is_not_reported_as_an_old_snapshot_mismatch(
    tmp_path, monkeypatch, failure
):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    _old_profile(path)
    tokenization._shared_cut.cache_clear()
    try:
        if failure == "package_version":
            original_version = tokenization.importlib.metadata.version
            monkeypatch.setattr(
                tokenization.importlib.metadata,
                "version",
                lambda name: "broken-runtime" if name == "jieba" else original_version(name),
            )
        else:

            def broken_dictionary():
                raise ValueError("runtime dictionary is corrupt")

            monkeypatch.setattr(tokenization, "load_dictionary", broken_dictionary)
        with pytest.raises(SnapshotError) as captured:
            load_snapshot(path, expected_gateway=GATEWAY)
        assert type(captured.value) is SnapshotError
    finally:
        tokenization._shared_cut.cache_clear()


@pytest.mark.parametrize("damage", ["vector_shape", "parents", "checksum"])
def test_old_environment_does_not_hide_invalid_vector_data(tmp_path, damage):
    path = tmp_path / "snapshot"
    save_snapshot(fixture(), path)
    _old_profile(path)
    if damage == "vector_shape":
        np.save(path / "vectors.npy", np.zeros((2, 3), dtype=np.float32), allow_pickle=False)
        update_hash(path, "vectors.npy")
    elif damage == "parents":
        (path / "parents.json").write_bytes(canonical_json([1, 1]))
        update_hash(path, "parents.json")
    else:
        content = (path / "vectors.npy").read_bytes()
        (path / "vectors.npy").write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    with pytest.raises(SnapshotError) as captured:
        load_snapshot(path, expected_gateway=GATEWAY)
    assert type(captured.value) is SnapshotError

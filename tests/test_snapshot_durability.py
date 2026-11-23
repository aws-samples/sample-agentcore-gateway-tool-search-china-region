"""A successful local publish must persist its data and both directory levels."""

import os
import stat
from pathlib import Path

import pytest

from semantic_core.catalog import GatewayIdentity, ToolIdentity, ToolRecord
from semantic_core.providers import ModelProfile
from semantic_core.snapshot import IndexSnapshot, SnapshotError, save_snapshot
from semantic_core.vectors import VectorIndex


def snapshot_fixture(with_vectors):
    gateway = GatewayIdentity("fixture-account", "fixture-region", "fixture-gateway")
    tool = ToolRecord.from_descriptor(
        ToolIdentity(gateway, "target", "incarnation", "lookup"),
        {"name": "lookup", "description": "fixture lookup", "inputSchema": {}},
    )
    return IndexSnapshot.build(
        gateway,
        [tool],
        vectors=VectorIndex([[3.0, 4.0]], [0], 1, 2) if with_vectors else None,
        model_profile=ModelProfile("fixture-model", "v1", 2) if with_vectors else None,
    )


def fsync_target(fd, directory):
    details = os.fstat(fd)
    identity = (details.st_dev, details.st_ino)
    if stat.S_ISDIR(details.st_mode):
        for name, path in (
            ("snapshot-directory", directory),
            ("parent-directory", directory.parent),
        ):
            candidate = path.stat()
            if identity == (candidate.st_dev, candidate.st_ino):
                return name
        pytest.fail("Unexpected directory synced during local publication")
    for path in directory.iterdir():
        candidate = path.stat()
        if identity == (candidate.st_dev, candidate.st_ino):
            return path.name
    pytest.fail("Unexpected file synced during local publication")


@pytest.mark.parametrize("with_vectors", [False, True])
def test_data_entries_are_durable_before_manifest_and_parent_entry_last(
    tmp_path, monkeypatch, with_vectors
):
    snapshot = snapshot_fixture(with_vectors)
    directory = tmp_path / "snapshot"
    events = []
    real_fsync = os.fsync

    def record(fd):
        events.append(fsync_target(fd, directory))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", record)
    save_snapshot(snapshot, directory)

    data_files = ["catalog.json", "bm25.json"]
    if with_vectors:
        data_files.extend(["vectors.npy", "parents.json"])
    assert events == data_files + [
        "snapshot-directory",
        "manifest.json",
        "snapshot-directory",
        "parent-directory",
    ]


@pytest.mark.parametrize("failure_stage", ["before-manifest", "after-manifest", "parent"])
def test_directory_sync_failures_never_report_success_and_close_descriptors(
    tmp_path, monkeypatch, failure_stage
):
    snapshot = snapshot_fixture(False)
    directory = tmp_path / "snapshot"
    real_fsync = os.fsync
    directory_fds = set()
    snapshot_syncs = 0

    def fail_at_stage(fd):
        nonlocal snapshot_syncs
        target = fsync_target(fd, directory)
        if target == "snapshot-directory":
            directory_fds.add(fd)
            snapshot_syncs += 1
            stage = "before-manifest" if snapshot_syncs == 1 else "after-manifest"
        elif target == "parent-directory":
            directory_fds.add(fd)
            stage = "parent"
        else:
            stage = "file"
        if stage == failure_stage:
            raise OSError("fixture durability failure details must stay private")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_at_stage)
    with pytest.raises(SnapshotError, match="^snapshot write failed$"):
        save_snapshot(snapshot, directory)

    assert directory_fds
    for fd in directory_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    if failure_stage == "before-manifest":
        assert not Path(directory, "manifest.json").exists()

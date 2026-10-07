from __future__ import annotations

from pathlib import Path

import pytest

from aa_arena.benchmark.snapshot import SnapshotStore


def test_snapshot_is_content_addressed_and_tracks_strategy_diff(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "strategy").mkdir(parents=True)
    source = workspace / "strategy" / "main.py"
    source.write_text("a = 1\n", encoding="utf-8")
    store = SnapshotStore(tmp_path / "store")
    first = store.create(workspace)
    duplicate = store.create(workspace, parent_snapshot_id=first.snapshot_id)
    assert duplicate.snapshot_id != first.snapshot_id
    assert duplicate.content_id == first.content_id
    assert duplicate.changed_files == 0
    assert store.verify(first.snapshot_id) == first
    assert store.verify(duplicate.snapshot_id) == duplicate

    source.write_text("a = 2\nb = 2\n", encoding="utf-8")
    second = store.create(workspace, parent_snapshot_id=first.snapshot_id)
    assert second.snapshot_id != first.snapshot_id
    assert second.changed_files == 1
    assert second.lines_added == 2
    assert second.lines_deleted == 1
    assert second.archive_sha256
    assert second.manifest_sha256
    restored = store.materialize(second.snapshot_id, tmp_path / "restored")
    assert (restored / "strategy" / "main.py").read_text() == "a = 2\nb = 2\n"


def test_snapshot_verification_detects_archive_corruption(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "strategy").mkdir(parents=True)
    (workspace / "strategy" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    store = SnapshotStore(tmp_path / "store")
    snapshot = store.create(workspace)
    snapshot.archive_path.chmod(0o644)
    with snapshot.archive_path.open("ab") as stream:
        stream.write(b"corruption")
    with pytest.raises(ValueError, match="archive hash mismatch"):
        store.verify(snapshot.snapshot_id)


def test_snapshot_refuses_symlinks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "link").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="symbolic links"):
        SnapshotStore(tmp_path / "store").create(workspace)

"""Immutable, content-addressed workspace snapshots."""

from __future__ import annotations

import json
import os
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from pathlib import Path

from aa_arena.io import atomic_write_json, canonical_hash, sha256_file


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    content_id: str
    parent_snapshot_id: str | None
    archive_path: Path
    manifest_path: Path
    strategy_hash: str
    changed_files: int
    lines_added: int
    lines_deleted: int
    source_lines: int
    archive_sha256: str
    manifest_sha256: str


def _is_source(path: Path) -> bool:
    return path.suffix.lower() in {
        ".py",
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".java",
        ".rs",
        ".js",
        ".ts",
    }


def _line_count(path: Path) -> int:
    if not _is_source(path):
        return 0
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.archives = self.root / "archives"
        self.manifests = self.root / "manifests"
        self.archives.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _manifest(
        workspace: Path, include_roots: tuple[str, ...] | None = None
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if include_roots is None:
            paths = workspace.rglob("*")
        else:
            selected: list[Path] = []
            for name in include_roots:
                root = (workspace / name).resolve()
                try:
                    root.relative_to(workspace)
                except ValueError as exc:
                    raise ValueError(f"snapshot include root escapes workspace: {name}") from exc
                if not root.exists():
                    raise ValueError(f"snapshot include root does not exist: {name}")
                selected.extend((root, *root.rglob("*")))
            paths = iter(selected)
        for path in sorted(paths):
            relative = path.relative_to(workspace).as_posix()
            if path.is_symlink():
                raise ValueError(f"workspace snapshots refuse symbolic links: {relative}")
            if not path.is_file():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            rows.append(
                {
                    "path": relative,
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                    "mode": mode,
                    "lines": _line_count(path),
                }
            )
        return rows

    def load_manifest(self, snapshot_id: str | None) -> dict[str, object] | None:
        if not snapshot_id:
            return None
        path = self.manifests / f"{snapshot_id}.json"
        if not path.is_file():
            raise ValueError(f"unknown parent snapshot: {snapshot_id}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"invalid snapshot manifest: {path}")
        return value

    def get(self, snapshot_id: str) -> Snapshot:
        document = self.load_manifest(snapshot_id)
        if document is None:
            raise ValueError(f"unknown snapshot: {snapshot_id}")
        content_id = str(document.get("content_id") or snapshot_id)
        archive_name = str(document.get("archive_name") or f"{content_id}.tar")
        archive_path = self.archives / archive_name
        if not archive_path.is_file():
            raise ValueError(f"snapshot archive is missing: {snapshot_id}")
        manifest_path = self.manifests / f"{snapshot_id}.json"
        return Snapshot(
            snapshot_id=snapshot_id,
            content_id=content_id,
            parent_snapshot_id=(
                str(document["parent_snapshot_id"])
                if document.get("parent_snapshot_id") is not None
                else None
            ),
            archive_path=archive_path,
            manifest_path=manifest_path,
            strategy_hash=str(document["strategy_hash"]),
            changed_files=int(document["changed_files"]),
            lines_added=int(document["lines_added"]),
            lines_deleted=int(document["lines_deleted"]),
            source_lines=int(document["source_lines"]),
            archive_sha256=str(document.get("archive_sha256") or sha256_file(archive_path)),
            manifest_sha256=sha256_file(manifest_path),
        )

    def _read_archived_lines(self, snapshot_id: str, relative: str) -> list[str]:
        snapshot = self.get(snapshot_id)
        document = self.load_manifest(snapshot_id) or {}
        stored = set(str(item) for item in document.get("stored_files", []))
        if stored and relative not in stored and snapshot.parent_snapshot_id:
            return self._read_archived_lines(snapshot.parent_snapshot_id, relative)
        with tarfile.open(snapshot.archive_path, "r") as archive:
            try:
                member = archive.getmember(relative)
            except KeyError:
                return []
            stream = archive.extractfile(member)
            if stream is None:
                return []
            return stream.read().decode("utf-8", errors="replace").splitlines()

    def verify(self, snapshot_id: str) -> Snapshot:
        """Verify immutable manifest identity and every archived file hash."""

        snapshot = self.get(snapshot_id)
        document = self.load_manifest(snapshot_id)
        assert document is not None
        files = document.get("files")
        if not isinstance(files, list):
            raise ValueError(f"invalid snapshot file list: {snapshot_id}")
        content_identity = {"schema_version": 1, "files": files}
        if canonical_hash(content_identity) != snapshot.content_id:
            raise ValueError(f"snapshot content identity mismatch: {snapshot_id}")
        if document.get("content_id") is not None:
            snapshot_identity = {
                "schema_version": 2,
                "content_id": snapshot.content_id,
                "parent_snapshot_id": snapshot.parent_snapshot_id,
            }
            if canonical_hash(snapshot_identity) != snapshot.snapshot_id:
                raise ValueError(f"snapshot lineage identity mismatch: {snapshot_id}")
        expected_archive_hash = document.get("archive_sha256")
        if expected_archive_hash and sha256_file(snapshot.archive_path) != expected_archive_hash:
            raise ValueError(f"snapshot archive hash mismatch: {snapshot_id}")

        expected = {str(row["path"]): row for row in files if isinstance(row, dict)}
        with tarfile.open(snapshot.archive_path, "r") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            stored = set(str(item) for item in document.get("stored_files", names))
            if len(names) != len(set(names)) or set(names) != stored:
                raise ValueError(f"snapshot archive member mismatch: {snapshot_id}")
            for member in members:
                if not member.isfile() or member.issym() or member.islnk():
                    raise ValueError(f"unsupported snapshot member: {member.name}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"unreadable snapshot member: {member.name}")
                digest = sha256()
                size = 0
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
                row = expected[member.name]
                if digest.hexdigest() != row.get("sha256") or size != int(row["size"]):
                    raise ValueError(f"snapshot member hash mismatch: {member.name}")
        if stored != set(expected):
            if not snapshot.parent_snapshot_id:
                raise ValueError(f"snapshot delta has no parent: {snapshot_id}")
            self.verify(snapshot.parent_snapshot_id)
            parent_document = self.load_manifest(snapshot.parent_snapshot_id) or {}
            parent_files = {
                str(row["path"]): row
                for row in parent_document.get("files", [])
                if isinstance(row, dict)
            }
            for path, row in expected.items():
                if path in stored:
                    continue
                if path not in parent_files or parent_files[path].get("sha256") != row.get("sha256"):
                    raise ValueError(f"snapshot delta parent mismatch: {path}")
        return snapshot

    def create(
        self,
        workspace: Path,
        *,
        parent_snapshot_id: str | None = None,
        include_roots: tuple[str, ...] | None = None,
    ) -> Snapshot:
        workspace = Path(workspace).resolve()
        if not workspace.is_dir():
            raise ValueError(f"workspace does not exist: {workspace}")
        files = self._manifest(workspace, include_roots)
        content_identity = {"schema_version": 1, "files": files}
        content_id = canonical_hash(content_identity)
        snapshot_identity = {
            "schema_version": 2,
            "content_id": content_id,
            "parent_snapshot_id": parent_snapshot_id,
        }
        snapshot_id = canonical_hash(snapshot_identity)
        strategy_rows = [row for row in files if str(row["path"]).startswith("strategy/")]
        strategy_hash = canonical_hash(strategy_rows)
        parent = self.load_manifest(parent_snapshot_id)
        old_by_path = {str(row["path"]): row for row in (parent.get("files", []) if parent else [])}
        new_by_path = {str(row["path"]): row for row in files}
        changed = set(old_by_path) ^ set(new_by_path)
        changed.update(
            path
            for path in set(old_by_path) & set(new_by_path)
            if old_by_path[path].get("sha256") != new_by_path[path].get("sha256")
        )
        lines_added = 0
        lines_deleted = 0
        for path in changed:
            if not _is_source(Path(path)):
                continue
            old_lines = (
                self._read_archived_lines(parent_snapshot_id, path)
                if parent_snapshot_id and path in old_by_path
                else []
            )
            new_lines = (
                (workspace / path).read_text(encoding="utf-8", errors="replace").splitlines()
                if path in new_by_path
                else []
            )
            for tag, old_start, old_end, new_start, new_end in SequenceMatcher(
                None, old_lines, new_lines, autojunk=False
            ).get_opcodes():
                if tag in {"delete", "replace"}:
                    lines_deleted += old_end - old_start
                if tag in {"insert", "replace"}:
                    lines_added += new_end - new_start
        document = {
            **content_identity,
            "snapshot_id": snapshot_id,
            "content_id": content_id,
            "parent_snapshot_id": parent_snapshot_id,
            "strategy_hash": strategy_hash,
            "changed_files": len(changed),
            "lines_added": lines_added,
            "lines_deleted": lines_deleted,
            "source_lines": sum(int(row.get("lines") or 0) for row in strategy_rows),
        }
        stored_files = sorted(
            path for path in new_by_path if parent_snapshot_id is None or path in changed
        )
        document["stored_files"] = stored_files
        manifest_path = self.manifests / f"{snapshot_id}.json"
        archive_name = (
            f"{content_id}-{snapshot_id}.tar" if parent_snapshot_id else f"{content_id}.tar"
        )
        document["archive_name"] = archive_name
        archive_path = self.archives / archive_name
        if not archive_path.exists():
            with tempfile.NamedTemporaryFile(
                dir=self.archives, prefix=f".{snapshot_id}.", delete=False
            ) as stream:
                temporary = Path(stream.name)
            try:
                with tarfile.open(temporary, "w", format=tarfile.PAX_FORMAT) as archive:
                    for row in files:
                        if str(row["path"]) not in stored_files:
                            continue
                        source = workspace / str(row["path"])
                        info = archive.gettarinfo(str(source), arcname=str(row["path"]))
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        info.mtime = 0
                        with source.open("rb") as handle:
                            archive.addfile(info, handle)
                os.replace(temporary, archive_path)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        document["archive_sha256"] = sha256_file(archive_path)
        if not manifest_path.exists():
            atomic_write_json(manifest_path, document)
            manifest_path.chmod(0o444)
        elif json.loads(manifest_path.read_text(encoding="utf-8")) != document:
            raise ValueError(f"snapshot manifest collision: {snapshot_id}")
        archive_path.chmod(0o444)
        return self.verify(snapshot_id)

    def materialize(self, snapshot_id: str, destination: Path) -> Path:
        snapshot = self.verify(snapshot_id)
        document = self.load_manifest(snapshot_id) or {}
        archive_path = snapshot.archive_path
        destination = Path(destination).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        if snapshot.parent_snapshot_id:
            self.materialize(snapshot.parent_snapshot_id, destination)
        with tarfile.open(archive_path, "r") as archive:
            for member in archive.getmembers():
                target = (destination / member.name).resolve()
                try:
                    target.relative_to(destination)
                except ValueError as exc:
                    raise ValueError(f"unsafe snapshot member: {member.name}") from exc
                if member.issym() or member.islnk():
                    raise ValueError(f"snapshot links are not supported: {member.name}")
            archive.extractall(destination, filter="data")
        expected = {
            str(row["path"])
            for row in document.get("files", [])
            if isinstance(row, dict)
        }
        stored = set(str(item) for item in document.get("stored_files", expected))
        parent_document = self.load_manifest(snapshot.parent_snapshot_id) if snapshot.parent_snapshot_id else None
        parent_expected = {
            str(row["path"])
            for row in (parent_document or {}).get("files", [])
            if isinstance(row, dict)
        }
        for path in (parent_expected | stored) - expected:
            (destination / path).unlink(missing_ok=True)
        return destination

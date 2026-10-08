"""Operator-selected immutable system runtime for reproducible evaluation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

MARKER = 'aa-arena-runtime.json'


def reference_root() -> Path | None:
    configured = os.environ.get('AA_ARENA_REFERENCE_RUNTIME')
    if not configured:
        return None
    root = Path(configured).resolve(strict=True)
    document = json.loads((root / MARKER).read_text())
    if document.get('schema_version') != 1 or document.get('architecture') != 'x86_64':
        raise RuntimeError('Invalid reference runtime manifest')
    for relative in ('usr/bin/g++-14', 'usr/bin/python3', 'usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2'):
        if not (root / relative).is_file():
            raise RuntimeError(f'Incomplete reference runtime: {relative}')
    return root


def system_path(path: str | Path) -> Path:
    root = reference_root()
    return root / str(path).lstrip('/') if root is not None else Path(path)


def fingerprint() -> str:
    root = reference_root()
    return hashlib.sha256((root / MARKER).read_bytes()).hexdigest() if root else 'host'


def verify_reference_files() -> int:
    root = reference_root()
    if root is None:
        return 0
    marker = json.loads((root / MARKER).read_text())
    manifest = (root / 'runtime-files.json').read_bytes()
    if hashlib.sha256(manifest).hexdigest() != marker['files_sha256']:
        raise RuntimeError('Reference runtime file manifest mismatch')
    rows = json.loads(manifest)
    if len(rows) != marker['file_count']:
        raise RuntimeError('Reference runtime file count mismatch')
    actual = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() or p.is_symlink()}
    expected = {row['path'] for row in rows} | {MARKER, 'runtime-files.json'}
    if actual != expected:
        raise RuntimeError('Reference runtime member set mismatch')
    for row in rows:
        relative = Path(row['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise RuntimeError('Invalid reference runtime member path')
        path = root / relative
        if 'symlink' in row:
            if not path.is_symlink() or os.readlink(path) != row['symlink']:
                raise RuntimeError(f'Reference symlink mismatch: {relative}')
        else:
            if path.is_symlink() or path.stat().st_size != row['size']:
                raise RuntimeError(f'Reference file size mismatch: {relative}')
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != row['sha256']:
                raise RuntimeError(f'Reference file hash mismatch: {relative}')
    return len(rows)


def backend_command(argv: list[str]) -> list[str]:
    """Run a trusted AI9 ELF judge with the same libraries as its players."""
    root = reference_root()
    if root is None:
        return argv
    loader = root / 'usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2'
    libraries = ':'.join(str(root / p) for p in ('usr/lib/x86_64-linux-gnu', 'usr/lib'))
    return [str(loader), '--inhibit-cache', '--library-path', libraries, *argv]

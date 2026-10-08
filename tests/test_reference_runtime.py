import hashlib
import json

import pytest

from aa_arena.core.reference_runtime import backend_command, fingerprint, verify_reference_files


def snapshot(tmp_path, monkeypatch):
    tmp_path = tmp_path / 'system'
    tmp_path.mkdir()
    paths = ['usr/bin/g++-14', 'usr/bin/python3', 'usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2']
    rows = []
    for relative in paths:
        p = tmp_path / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'fixture')
        rows.append({'path': relative, 'size': 7, 'sha256': hashlib.sha256(b'fixture').hexdigest()})
    data = json.dumps(rows).encode()
    (tmp_path / 'runtime-files.json').write_bytes(data)
    (tmp_path / 'aa-arena-runtime.json').write_text(json.dumps({
        'schema_version': 1, 'architecture': 'x86_64', 'file_count': len(rows),
        'files_sha256': hashlib.sha256(data).hexdigest()}))
    monkeypatch.setenv('AA_ARENA_REFERENCE_RUNTIME', str(tmp_path))
    return tmp_path


def test_reference_snapshot_detects_corruption(tmp_path, monkeypatch):
    tmp_path = snapshot(tmp_path, monkeypatch)
    assert verify_reference_files() == 3
    (tmp_path / 'usr/bin/g++-14').write_bytes(b'corrupt')
    with pytest.raises(RuntimeError, match='hash mismatch'):
        verify_reference_files()


def test_backend_uses_reference_loader_and_library_search(tmp_path, monkeypatch):
    tmp_path = snapshot(tmp_path, monkeypatch)
    command = backend_command(['/build/logic', '--ai', '127.0.0.1:12345'])
    assert command[0] == str(tmp_path / 'usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2')
    assert '--inhibit-cache' in command
    assert command[-3:] == ['/build/logic', '--ai', '127.0.0.1:12345']
    assert len(fingerprint()) == 64


def test_reference_snapshot_rejects_unrecorded_files(tmp_path, monkeypatch):
    tmp_path = snapshot(tmp_path, monkeypatch)
    (tmp_path / 'usr/bin/extra-tool').write_bytes(b'extra')
    with pytest.raises(RuntimeError, match='member set mismatch'):
        verify_reference_files()

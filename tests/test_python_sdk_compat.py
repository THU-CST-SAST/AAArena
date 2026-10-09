"""Pinned SDK compatibility preserves policies and concurrent cache integrity."""
import concurrent.futures
import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from aa_arena.core.python_sdk import prepare_antwar2_sdk


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    with tarfile.open(Path(__file__).resolve().parents[1] / 'assets/antwar2.tar.gz') as archive:
        for name in ['state.py', 'engine.py']:
            data = archive.extractfile('games/antwar2/public_sdk/SDK/backend/' + name).read()
            if name == 'state.py':
                data = data.replace(b'def downgrade_tower_income(self, tower_type, tower: Tower | None = None) -> int:', b'def downgrade_tower_income(self, tower_type) -> int:')
                data = data.replace(b'downgrade_tower_income(tower_type, tower)', b'downgrade_tower_income(tower_type)')
                assert hashlib.sha256(data).hexdigest() == '93114a0ff0d29673efd7f6e692103159ede717e46bcb9712b9044c00de2d71d7'
            path = root / 'SDK/backend' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    (root / 'ai.py').write_text('POLICY_BYTES = "must remain unchanged"\n')
    return root


def test_pinned_repair_preserves_source_and_matches_public_sdk(source, tmp_path):
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    runtime = prepare_antwar2_sdk(source, tmp_path / 'cache')
    assert runtime != source
    assert all((source / n).read_bytes() == data for n, data in before.items())
    assert (runtime / 'ai.py').read_bytes() == before['ai.py']
    assert (runtime / 'SDK/backend/engine.py').read_bytes() == before['SDK/backend/engine.py']
    assert hashlib.sha256((runtime / 'SDK/backend/state.py').read_bytes()).hexdigest() == '5fc54d9d137700334ad7cc625d3ba68ad5b7df21510893b9dab3887fbfda58e9'
    manifest = json.loads((runtime / '.aa-sdk-runtime.json').read_text())
    assert manifest['changed_files'] == ['SDK/backend/state.py']


@pytest.mark.parametrize('file', ['SDK/backend/state.py', 'SDK/backend/engine.py'])
def test_unknown_sdk_is_not_overridden(source, tmp_path, file):
    with (source / file).open('a') as stream:
        stream.write('\n# custom SDK\n')
    assert prepare_antwar2_sdk(source, tmp_path / 'cache') == source
    assert not (tmp_path / 'cache').exists()


def test_concurrent_publication_and_cache_integrity(source, tmp_path):
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: prepare_antwar2_sdk(source, tmp_path / 'cache'), range(16)))
    assert len(set(paths)) == 1
    (paths[0] / 'ai.py').write_text('tampered policy')
    with pytest.raises(OSError, match='integrity'):
        prepare_antwar2_sdk(source, tmp_path / 'cache')


def test_new_policy_bytes_get_distinct_runtime(source, tmp_path):
    first = prepare_antwar2_sdk(source, tmp_path / 'cache')
    (source / 'ai.py').write_text('POLICY_BYTES = "different"\n')
    second = prepare_antwar2_sdk(source, tmp_path / 'cache')
    assert first != second
    assert (first / 'ai.py').read_bytes() != (second / 'ai.py').read_bytes()

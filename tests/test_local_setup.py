"""Local setup rejects incompatible environments before advertising success."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest

spec = importlib.util.spec_from_file_location('local_setup', Path(__file__).resolve().parents[1] / 'scripts/setup_local.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def test_local_setup_does_not_inherit_research_environment(monkeypatch, tmp_path):
    names = ('PYTHONPATH', 'PYTHONHOME', 'AA_ARENA_REFERENCE_RUNTIME', 'AA_ARENA_BACKEND_PYTHON', 'AA_ARENA_ADMISSION_ROOT')
    for name in names:
        monkeypatch.setenv(name, '/private/research')
    env = setup.runtime_environment(tmp_path)
    assert all(name not in env for name in names)
    assert env['AA_ARENA_PLAYER_ENV'] == str(tmp_path / '.player-env')
    assert env['OPENBLAS_NUM_THREADS'] == env['OMP_NUM_THREADS'] == '1'


@pytest.mark.parametrize('system,machine', [('Darwin','arm64'), ('Linux','aarch64'), ('Windows','AMD64')])
def test_unsupported_host_fails_before_install(monkeypatch, system, machine):
    monkeypatch.setattr(setup.platform, 'system', lambda: system)
    monkeypatch.setattr(setup.platform, 'machine', lambda: machine)
    monkeypatch.setattr(setup, 'run', lambda *a, **k: pytest.fail('must not execute installation'))
    with pytest.raises(RuntimeError, match='x86-64 Linux'):
        setup.preflight({})


def test_python_patch_version_is_required(monkeypatch, tmp_path):
    monkeypatch.setattr(setup, 'ROOT', tmp_path)
    monkeypatch.setattr(setup, 'run', lambda *a, **k: SimpleNamespace(stdout='[3, 10, 13]'))
    with pytest.raises(RuntimeError, match='3.10.14'):
        setup.verify_player({})


def test_no_activation_after_failed_live_games(monkeypatch, tmp_path):
    stale = tmp_path / 'validation/local-setup/activate.sh'
    stale.parent.mkdir(parents=True)
    stale.write_text('# obsolete acceptance')
    monkeypatch.setattr(setup, 'ROOT', tmp_path)
    monkeypatch.setattr(setup.sys, 'argv', ['setup_local.py', '--verify-only'])
    monkeypatch.setattr(setup, 'preflight', lambda e: None)
    monkeypatch.setattr(setup, 'verify_player', lambda e: None)
    def run(args, env, **kw):
        if any(str(x).endswith('validate_games.py') for x in args):
            raise setup.subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout='')
    monkeypatch.setattr(setup, 'run', run)
    with pytest.raises(setup.subprocess.CalledProcessError):
        setup.main()
    assert not (tmp_path / 'validation/local-setup/activate.sh').exists()


def test_asset_verification_rejects_hardlinked_copy(tmp_path):
    import hashlib
    import os
    spec = importlib.util.spec_from_file_location('asset_installer', Path(__file__).resolve().parents[1] / 'scripts/install_assets.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    source = tmp_path / 'outside.cpp'; source.write_bytes(b'original')
    root = tmp_path / 'release'; root.mkdir()
    os.link(source, root / 'main.cpp')
    manifest = {'files': [{'path': 'main.cpp', 'size': 8, 'sha256': hashlib.sha256(b'original').hexdigest()}]}
    with pytest.raises(ValueError, match='Hard-linked asset'):
        installer.verify_game(root, manifest)
    (root / 'main.cpp').unlink()
    (root / 'main.cpp').write_bytes(b'original')
    assert installer.verify_game(root, manifest) == 1

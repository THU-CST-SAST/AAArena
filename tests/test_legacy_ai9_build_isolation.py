from __future__ import annotations

import hashlib
import importlib
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from aa_arena.legacy import ai9
from games.dorado.evaluator import runtime as dorado

ROOT = Path(__file__).resolve().parents[1]
GAMES = ("dorado", "lota", "monecraft", "pacman")
pytestmark = pytest.mark.skipif(
    shutil.which("bwrap", path="/usr/bin:/bin") is None,
    reason="bubblewrap is unavailable",
)


def _build(game: str, source: Path, cache: Path) -> Path:
    game_root = ROOT / "games" / game
    if game == "dorado":
        layout = dorado.DoradoLayout.from_game_dir(game_root, cache)
        return dorado.build_player(layout, "candidate", source).library_path
    module = importlib.import_module(f"games.{game}.evaluator")
    evaluator = module.make_evaluator(game_root, build_root=cache)
    return ai9.build_player(evaluator._config, "candidate", source, cache).library_path


@pytest.mark.parametrize("game", GAMES)
def test_legacy_sdk_candidate_build_and_cached_elf_checks_are_isolated(
    game: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    original = next(iter(sorted((ROOT / "games" / game / "players/pool").glob("*/ai.cpp"))))
    # A materialized candidate remains untrusted under hidden evaluation paths.
    source = tmp_path / "controller/evaluation/snapshot/player"
    shutil.copytree(original.parent, source)
    before = (source / "ai.cpp").read_bytes()
    calls = []
    actual = ai9.run_isolated_build

    def tracked(command, **kwargs):
        result = actual(command, **kwargs)
        calls.append((tuple(command), kwargs, result.returncode))
        return result

    monkeypatch.setattr(ai9, "run_isolated_build", tracked)
    library = _build(game, source, tmp_path / "cache")
    digest = hashlib.sha256(library.read_bytes()).hexdigest()
    assert _build(game, source, tmp_path / "cache") == library
    assert hashlib.sha256(library.read_bytes()).hexdigest() == digest
    assert (source / "ai.cpp").read_bytes() == before
    assert [command[0] for command, _, _ in calls] == ["g++", "ldd", "nm", "ldd", "nm"]
    for command, options, returncode in calls:
        assert returncode == 0
        if command[0] in {"ldd", "nm"}:
            assert options["readonly_paths"] == (library,)
            assert options["cwd"] != library.parent
        else:
            assert ROOT / "games" / game / "backend" not in options["readonly_paths"]


@pytest.mark.parametrize("game", GAMES)
def test_legacy_compiler_cannot_read_private_host_include(game: str, tmp_path: Path) -> None:
    private = tmp_path / "host-private.hpp"
    private.write_text('#define PRIVATE_SENTINEL "HOST_PRIVATE_SENTINEL"\n')
    source = tmp_path / "controller/evaluation/snapshot/player"
    source.mkdir(parents=True)
    (source / "ai.cpp").write_text(f'#include "{private}"\n')
    with pytest.raises((ai9.Ai9Error, dorado.DoradoRuntimeError)) as error:
        _build(game, source, tmp_path / "cache")
    assert "No such file or directory" in str(error.value)
    assert "HOST_PRIVATE_SENTINEL" not in str(error.value)
    assert not list((tmp_path / "cache").rglob("player.so"))


@pytest.mark.parametrize("game", GAMES)
def test_legacy_rejects_links_before_hashing_or_staging(game: str, tmp_path: Path) -> None:
    private = tmp_path / "private.cpp"
    private.write_text("test sentinel")
    source = tmp_path / "player"
    source.mkdir()
    (source / "ai.cpp").symlink_to(private)
    with pytest.raises((ai9.Ai9Error, dorado.DoradoRuntimeError), match="unsafe build tree entry"):
        _build(game, source, tmp_path / "cache")
    assert private.read_text() == "test sentinel"
    assert not (tmp_path / "cache").exists()


def test_elf_inspection_has_no_host_io_network_environment_or_artifact_writes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private = tmp_path / "host-private.txt"
    private.write_text("test sentinel")
    library = tmp_path / "player.so"
    library.write_text("test artifact")
    monkeypatch.setenv("BUILD_TEST_HOST_ENV", "host-value")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        # Execute a probe through the same inspection boundary as ldd's ELF
        # interpreter. Only the candidate library is mounted, read-only.
        probe = f"""
import os, pathlib, socket
assert os.getuid() == os.getgid() == 65534
assert 'BUILD_TEST_HOST_ENV' not in os.environ
for path, mode in [({str(private)!r}, 'r'), ({str(private)!r}, 'r+'), ({str(library)!r}, 'r+')]:
    try:
        with open(path, mode):
            pass
    except OSError:
        pass
    else:
        raise AssertionError((path, mode))
try:
    socket.create_connection(('127.0.0.1', {port}), timeout=0.2)
except OSError:
    pass
else:
    raise AssertionError('host network reachable')
print('isolated')
"""
        result = ai9._inspect_player_library(["/usr/bin/python3", "-c", probe], library)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "isolated"
    assert private.read_text() == "test sentinel"
    assert library.read_text() == "test artifact"


def test_ldd_cannot_load_a_host_private_dependency(tmp_path: Path) -> None:
    # Construct a harmless ELF whose DT_NEEDED points at a synthetic host-only
    # library. The old host ldd accepted it; isolated ldd must report not found.
    private = tmp_path / "host-private"
    private.mkdir()
    dependency_source = private / "dependency.cpp"
    dependency_source.write_text('extern "C" void dependency() {}\n')
    dependency = private / "dependency.so"
    subprocess.run(
        ["/usr/bin/g++", "-shared", "-fPIC", str(dependency_source), "-o", str(dependency)],
        check=True,
        capture_output=True,
    )
    source = tmp_path / "player.cpp"
    source.write_text(
        'extern "C" void dependency(); extern "C" void player_ai() { dependency(); }\n'
    )
    library = tmp_path / "player.so"
    subprocess.run(
        ["/usr/bin/g++", "-shared", "-fPIC", str(source), str(dependency), "-o", str(library)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ai9.Ai9Error, match="unresolved dependencies"):
        ai9._validate_player_export("lota", library)

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace

import pytest

from aa_arena.core.registry import get_plugin

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.backend]
requires_backend = pytest.mark.skipif(
    os.environ.get("AA_ARENA_RUN_BACKEND_TESTS") != "1",
    reason="set AA_ARENA_RUN_BACKEND_TESTS=1 to prepare bundled backends",
)


def runtime_module(game: str) -> ModuleType:
    evaluator = get_plugin(game, ROOT / "games").evaluator_factory(ROOT / "games" / game)
    package = evaluator.__class__.__module__.rsplit(".", 1)[0]
    return importlib.import_module(f"{package}.runtime")


@requires_backend
def test_antwar_backend_builds(tmp_path: Path) -> None:
    runtime = runtime_module("antwar")
    layout = runtime.AntWarLayout.from_game_dir(ROOT / "games/antwar", tmp_path)
    frozen = runtime.build_backend(layout)
    assert frozen.executable.is_file()


@pytest.mark.parametrize("game", ["lostspace", "miracle", "rollman"])
@requires_backend
def test_python_backend_imports(game: str, tmp_path: Path) -> None:
    backend = ROOT / "games" / game / "backend"
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if game == "lostspace":
        env.update(runtime_module(game).backend_environment(tmp_path))
    probe = "import runpy; runpy.run_path('main.py', run_name='__backend_probe__')"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=backend,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert not tuple(backend.rglob("__pycache__"))


@pytest.mark.parametrize(
    ("game", "sdk_header"),
    [
        ("antwar", "optional.hpp"),
        ("aquawar", "jsoncpp/json/json-forwards.h"),
        ("generals", "constant.hpp"),
        ("lostspace", "json/json-forwards.h"),
        ("miracle", "json.hpp"),
        ("snakego", "adk.hpp"),
    ],
)
def test_six_games_prepare_source_only_players_with_the_common_native_builder(
    game: str,
    sdk_header: str,
    tmp_path: Path,
) -> None:
    runtime = runtime_module(game)
    player = tmp_path / f"{game}-player"
    player.mkdir()
    (player / "main.cpp").write_text(
        f'#include "{sdk_header}"\nint main() {{ return 0; }}\n',
        encoding="utf-8",
    )
    build_root = tmp_path / "build"
    map_file = tmp_path / "mapconf2.map"
    map_file.write_text("test-map\n", encoding="utf-8")

    if game in {"antwar", "aquawar"}:
        executable = runtime.build_player(player, build_root)
        argv = (str(executable),)
        cwd = executable.parent
    elif game == "snakego":
        prepared = runtime.prepare_package(
            "test-player",
            player,
            build_root=build_root,
            language="cpp",
        )
        argv, cwd = prepared.argv, prepared.cwd
    else:
        opponent = SimpleNamespace(
            opponent_id="test-player",
            package_root=player,
            lang="cpp",
            exclusion_diagnostic=None,
        )
        kwargs = {"build_root": build_root}
        if game == "lostspace":
            kwargs["map_file"] = map_file
        prepared = runtime.prepare_player(opponent, **kwargs)
        argv, cwd = prepared.argv, prepared.cwd

    assert Path(argv[0]).is_file()
    assert subprocess.run(argv, cwd=cwd, check=False).returncode == 0
    if game == "lostspace":
        assert (cwd / "mapconf2.map").read_text(encoding="utf-8") == "test-map\n"

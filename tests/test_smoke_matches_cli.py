from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


def _load_smoke_module() -> Any:
    path = Path(__file__).with_name("smoke_matches.py")
    spec = importlib.util.spec_from_file_location("aa_arena_smoke_matches", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_matches_routes_each_game_to_preserved_subdirectory(
    tmp_path: Path,
) -> None:
    module = _load_smoke_module()
    module.MATCH_GAMES = ("game-a", "game-b")
    calls: list[tuple[str, Path | None]] = []
    module.run_match = lambda game, temporary_root=None: calls.append((game, temporary_root))

    module.run_matches(tmp_path / "preserved")

    assert calls == [
        ("game-a", tmp_path / "preserved" / "game-a"),
        ("game-b", tmp_path / "preserved" / "game-b"),
    ]

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aa_arena.core import EvaluationStatus, PlayerRef, evaluate

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.game_smoke,
    pytest.mark.skipif(
        os.environ.get("AA_ARENA_RUN_GAME_SMOKE") != "1",
        reason="set AA_ARENA_RUN_GAME_SMOKE=1 to run real matches",
    ),
]

CASES = [
    ("antwar", ["P0", "P1"], ["games/antwar/public_sdk", "games/antwar/public_sdk"]),
    (
        "lostspace",
        ["P0", "P1", "P2", "P3"],
        ["games/lostspace/public_sdk"] * 4,
    ),
    (
        "miracle",
        ["P0", "P1"],
        ["games/miracle/public_sdk", "games/miracle/public_sdk"],
    ),
    (
        "rollman",
        ["rollman", "ghost"],
        ["games/rollman/public_sdk-rollman", "games/rollman/public_sdk-ghost"],
    ),
]


@pytest.mark.parametrize(("game", "roles", "paths"), CASES, ids=[item[0] for item in CASES])
def test_bundled_game_completes_with_replay(
    game: str, roles: list[str], paths: list[str]
) -> None:
    players = [
        PlayerRef(f"sdk-{game}-{index}", code_path=str((ROOT / path).resolve()))
        for index, path in enumerate(paths)
    ]
    result = evaluate(game, players, roles, seed=7, games_root=ROOT / "games")
    assert result.status is EvaluationStatus.COMPLETE, result.diagnostic
    assert result.winner in set(roles)
    assert set(result.scores) == set(roles)
    assert result.rounds is not None and result.rounds > 0
    assert result.replay_path is not None and Path(result.replay_path).is_file()

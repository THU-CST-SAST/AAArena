from __future__ import annotations

from pathlib import Path


SAIBLO_GAMES = (
    "antwar",
    "antwar2",
    "aquawar",
    "generals",
    "lostspace",
    "miracle",
    "rollman",
    "snakego",
)


def test_saiblo_games_use_one_shared_transport() -> None:
    root = Path(__file__).resolve().parents[1]
    for game in SAIBLO_GAMES:
        source = (root / "games" / game / "evaluator" / "arena.py").read_text(encoding="utf-8")
        assert "aa_arena.saiblo" in source, game
        assert "def _read_exact(" not in source, game
        assert "def _reader" not in source, game

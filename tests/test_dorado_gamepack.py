from __future__ import annotations

from pathlib import Path

import pytest

from aa_arena.core import PlayerRef, available_games
from aa_arena.core.registry import get_plugin


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_dorado_is_discovered_with_two_player_contract() -> None:
    assert "dorado" in available_games()
    plugin = get_plugin("dorado")
    assert plugin.name == "dorado"
    assert plugin.roles == ("P0", "P1")
    assert plugin.roles_symmetric is True


def test_dorado_layout_contains_legacy_backend_and_sample_pool() -> None:
    game_dir = REPOSITORY_ROOT / "games" / "dorado"
    for relative in (
        "game.yaml",
        "plugin.py",
        "backend/logic/src/main.cpp",
        "backend/ailoader/src/main.cc",
        "backend/liblogic/logic.cc",
        "backend/libailoader/ailoader.cc",
        "backend/shared/socket.cc",
        "backend/resources.res",
        "backend/logic/src/mapNEW.txt",
        "players/manifest.tsv",
        "players/publication.json",
    ):
        path = game_dir / relative
        assert path.is_file(), path


@pytest.mark.game_smoke
def test_dorado_sample_file_write_is_reported_as_official_forfeit(tmp_path: Path) -> None:
    if not (REPOSITORY_ROOT/"games/dorado/players/pool/sample_ai").is_dir():
        pytest.skip("Archived sample is outside the published Elo-rank subset")
    plugin = get_plugin("dorado")
    evaluator = plugin.evaluator_factory(
        REPOSITORY_ROOT / "games" / "dorado",
        build_root=tmp_path / "build",
        artifact_root=tmp_path / "artifacts",
        timeout_s=300.0,
        max_rounds=40,
    )
    result = evaluator.evaluate(
        [
            PlayerRef("sample_ai", None),
            PlayerRef("monster_ai", None),
        ],
        ["P0", "P1"],
        20260905,
    )
    # This archived sample writes logai.txt, which the original syscall policy
    # rejects. A backend winner must not hide that candidate execution error.
    assert result.status.value == "game_error"
    assert "ILLEGAL_SYSCALL" in result.diagnostic
    assert result.winner == "P1"
    assert result.scores == {"P0": 0.0, "P1": 1.0}
    assert result.replay_path is not None
    assert Path(result.replay_path).is_file()

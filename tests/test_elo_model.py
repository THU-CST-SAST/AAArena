from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from aa_arena.elo import model


def _repository_with_availability(tmp_path: Path) -> Path:
    repository = tmp_path / "AA-Arena"
    pool = repository / "games" / "antwar2" / "players" / "pool"
    for player_id in ("alpha", "beta"):
        (pool / player_id).mkdir(parents=True)
    snapshot = repository / "results" / "availability" / "antwar2" / "runnable.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "player_id": "beta",
                        "verified": False,
                        "elo": 1300.0,
                        "track": "cpp",
                    },
                    {
                        "player_id": "alpha",
                        "verified": True,
                        "elo": 1700.0,
                        "track": "python",
                    },
                    {
                        "player_id": "missing",
                        "verified": True,
                        "elo": None,
                        "track": None,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return repository


def test_load_verified_players_uses_only_repository_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository_with_availability(tmp_path)
    monkeypatch.setattr(model, "REPOSITORY_ROOT", repository)

    players = model.load_verified_players("antwar2")

    assert [player.player_id for player in players] == ["alpha"]
    assert players[0].package_root == (
        repository / "games" / "antwar2" / "players" / "pool" / "alpha"
    )
    assert players[0].anchor_elo == 1700.0
    assert players[0].track == "python"


def test_load_verified_players_rejects_unsupported_game() -> None:
    with pytest.raises(ValueError, match="unsupported paper game"):
        model.load_verified_players("not-a-paper-game")


def test_loader_has_no_external_root_parameter() -> None:
    assert tuple(inspect.signature(model.load_verified_players).parameters) == ("game",)


def _players(count: int = 4) -> tuple[model.EloPlayer, ...]:
    return tuple(
        model.EloPlayer(f"player-{index}", Path(f"/pool/player-{index}"))
        for index in range(count)
    )


def test_build_cases_is_deterministic_role_balanced_and_has_no_self_play() -> None:
    players = _players()

    first = model.build_cases(players, ("P0", "P1"), False, degree=2, seed=7)
    second = model.build_cases(tuple(reversed(players)), ("P0", "P1"), False, 2, 7)

    assert first == second
    assert len(first) == 8
    assert all(case.player_a != case.player_b for case in first)
    assert [case.player_ids_by_role for case in first[:2]] == [
        ("player-0", "player-1"),
        ("player-1", "player-0"),
    ]
    assert all(case.roles == ("P0", "P1") and case.seed == 7 for case in first)


def test_build_cases_supports_symmetric_lostspace() -> None:
    players = _players()

    cases = model.build_cases(
        players, ("P0", "P1", "P2", "P3"), True, degree=2, seed=11
    )

    assert cases[0].player_ids_by_role == (
        "player-0",
        "player-1",
        "player-1",
        "player-1",
    )
    assert cases[1].player_ids_by_role == (
        "player-1",
        "player-0",
        "player-0",
        "player-0",
    )


def test_build_cases_rejects_non_symmetric_multiplayer_game() -> None:
    with pytest.raises(ValueError, match="non-symmetric multiplayer"):
        model.build_cases(_players(), ("P0", "P1", "P2"), False, degree=2, seed=7)


def test_build_cases_respects_players_constrained_to_asymmetric_roles() -> None:
    players = (
        model.EloPlayer("rollman-a", Path("/pool/rollman-a"), track="rollman"),
        model.EloPlayer("rollman-b", Path("/pool/rollman-b"), track="rollman"),
        model.EloPlayer("ghost-a", Path("/pool/ghost-a"), track="ghost"),
        model.EloPlayer("unclassified", Path("/pool/unclassified")),
    )

    cases = model.build_cases(
        players,
        ("rollman", "ghost"),
        roles_symmetric=False,
        degree=2,
        seed=7,
    )

    assert len(cases) == 4
    assert {
        (
            case.player_a.player_id,
            case.player_b.player_id,
            case.player_ids_by_role,
            case.roles,
        )
        for case in cases
    } == {
        (
            "rollman-a",
            "ghost-a",
            ("rollman-a", "ghost-a"),
            ("rollman", "ghost"),
        ),
        (
            "rollman-b",
            "ghost-a",
            ("rollman-b", "ghost-a"),
            ("rollman", "ghost"),
        ),
        (
            "ghost-a",
            "rollman-a",
            ("ghost-a", "rollman-a"),
            ("ghost", "rollman"),
        ),
        (
            "ghost-a",
            "rollman-b",
            ("ghost-a", "rollman-b"),
            ("ghost", "rollman"),
        ),
    }
    assert all(
        case.player_ids_by_role[0] == case.player_a.player_id
        and case.roles[0] == case.player_a.track
        for case in cases
    )


from __future__ import annotations

from pathlib import Path

import pytest

from aa_arena.elo.model import EloPlayer
from aa_arena.elo.rating import fit_ratings


def _players(*definitions: str | tuple[str, float]) -> tuple[EloPlayer, ...]:
    players: list[EloPlayer] = []
    for definition in definitions:
        if isinstance(definition, tuple):
            player_id, anchor = definition
        else:
            player_id, anchor = definition, None
        players.append(EloPlayer(player_id, Path(f"/pool/{player_id}"), anchor))
    return tuple(players)


def test_fit_ratings_orders_winner_above_loser() -> None:
    ratings = fit_ratings(_players("alpha", "beta"), [("alpha", "beta", 1.0)])
    by_id = {row.player_id: row for row in ratings}

    assert by_id["alpha"].measured_elo > by_id["beta"].measured_elo
    assert by_id["alpha"].matches == by_id["beta"].matches == 1
    assert by_id["alpha"].points == 1.0
    assert by_id["beta"].points == 0.0


def test_anchor_elo_only_shifts_the_common_zero_point() -> None:
    players = _players(("alpha", 1700.0), ("beta", 1500.0))

    ratings = fit_ratings(players, [("alpha", "beta", 1.0)])

    assert sum(
        row.measured_elo - float(row.anchor_elo) for row in ratings
    ) == pytest.approx(0.0)


def test_prior_is_one_virtual_anchor_match_per_player() -> None:
    players = _players(
        ("alpha", 1500.0),
        ("beta", 1500.0),
        ("gamma", 1500.0),
    )
    observations = [
        ("alpha", "beta", 1.0),
        ("alpha", "beta", 1.0),
        ("alpha", "gamma", 1.0),
        ("alpha", "gamma", 1.0),
    ]

    ratings = fit_ratings(players, observations, prior_matches=1.0)
    by_id = {row.player_id: row for row in ratings}

    assert by_id["alpha"].measured_elo == pytest.approx(1767.48, abs=0.01)
    assert by_id["beta"].measured_elo == pytest.approx(1366.26, abs=0.01)
    assert by_id["gamma"].measured_elo == pytest.approx(1366.26, abs=0.01)


def test_fit_ratings_is_deterministic_for_reordered_observations() -> None:
    players = _players("alpha", "beta", "gamma")
    observations = [
        ("alpha", "beta", 1.0),
        ("alpha", "gamma", 0.5),
        ("beta", "gamma", 0.0),
    ]

    assert fit_ratings(players, observations) == fit_ratings(
        players, tuple(reversed(observations))
    )


def test_fit_ratings_rejects_invalid_score() -> None:
    with pytest.raises(ValueError, match="score_a"):
        fit_ratings(_players("alpha", "beta"), [("alpha", "beta", 1.5)])


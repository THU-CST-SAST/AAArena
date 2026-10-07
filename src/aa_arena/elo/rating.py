"""Deterministic Bradley--Terry fitting for completed Elo matches."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import exp, fsum, log, log10
from typing import Iterable, Sequence

from aa_arena.elo.model import EloPlayer


@dataclass(frozen=True)
class PlayerRating:
    """One player's measured rating and unsmoothed match statistics."""

    player_id: str
    measured_elo: float
    matches: int
    points: float
    winrate: float | None
    anchor_elo: float | None
    track: str | None


def fit_ratings(
    players: Sequence[EloPlayer],
    observations: Iterable[tuple[str, str, float]],
    *,
    prior_matches: float = 1.0,
) -> tuple[PlayerRating, ...]:
    """Fit Bradley--Terry strengths and express them on an Elo scale.

    ``score_a`` is 1 for an A win, 0 for a B win, and 0.5 for a draw. A
    one virtual draw against a neutral strength-1 anchor is added per player.
    This prevents perfect records from producing infinite ratings without
    multiplying the prior by the number of distinct opponents. Reported match
    counts and points exclude that prior.
    """

    if prior_matches <= 0:
        raise ValueError("prior_matches must be positive")

    by_id = {player.player_id: player for player in players}
    if len(by_id) != len(players):
        raise ValueError("player IDs must be unique")

    pair_scores: dict[tuple[str, str], list[float]] = defaultdict(list)
    matches = {player_id: 0 for player_id in by_id}
    points = {player_id: 0.0 for player_id in by_id}

    for player_a, player_b, score_a in observations:
        if player_a not in by_id or player_b not in by_id:
            raise ValueError("observation references an unknown player")
        if player_a == player_b:
            raise ValueError("an observation cannot pair a player with itself")
        if not 0.0 <= score_a <= 1.0:
            raise ValueError("score_a must be between 0 and 1")

        low, high = sorted((player_a, player_b))
        score_low = score_a if player_a == low else 1.0 - score_a
        pair_scores[(low, high)].append(score_low)
        matches[player_a] += 1
        matches[player_b] += 1
        points[player_a] += score_a
        points[player_b] += 1.0 - score_a

    ordered_ids = sorted(by_id)
    strengths = {player_id: 1.0 for player_id in ordered_ids}
    games: dict[str, dict[str, float]] = {
        player_id: {} for player_id in ordered_ids
    }

    for (low, high), scores in sorted(pair_scores.items()):
        game_count = float(len(scores))
        games[low][high] = game_count
        games[high][low] = game_count

    active_ids = [player_id for player_id in ordered_ids if games[player_id]]
    if active_ids:
        for _ in range(5_000):
            change = 0.0
            for player_id in active_ids:
                numerator = points[player_id] + prior_matches / 2.0
                denominator = (
                    prior_matches / (strengths[player_id] + 1.0)
                    + fsum(
                        game_count
                        / (strengths[player_id] + strengths[opponent])
                        for opponent, game_count in sorted(games[player_id].items())
                    )
                )
                updated = numerator / denominator
                change = max(
                    change,
                    abs(log(updated) - log(strengths[player_id])),
                )
                strengths[player_id] = updated

            geometric_mean = exp(
                fsum(log(strengths[player_id]) for player_id in active_ids)
                / len(active_ids)
            )
            for player_id in active_ids:
                strengths[player_id] /= geometric_mean
            if change < 1e-10:
                break

    raw_elo = {
        player_id: 400.0 * log10(strengths[player_id])
        for player_id in ordered_ids
    }
    anchored = [player for player in players if player.anchor_elo is not None]
    if anchored:
        shift = fsum(
            float(player.anchor_elo) - raw_elo[player.player_id]
            for player in anchored
        ) / len(anchored)
    elif ordered_ids:
        shift = 1500.0 - fsum(raw_elo.values()) / len(ordered_ids)
    else:
        shift = 1500.0

    rows = [
        PlayerRating(
            player_id=player_id,
            measured_elo=round(raw_elo[player_id] + shift, 6),
            matches=matches[player_id],
            points=points[player_id],
            winrate=(
                points[player_id] / matches[player_id]
                if matches[player_id]
                else None
            ),
            anchor_elo=by_id[player_id].anchor_elo,
            track=by_id[player_id].track,
        )
        for player_id in ordered_ids
    ]
    return tuple(sorted(rows, key=lambda row: (-row.measured_elo, row.player_id)))

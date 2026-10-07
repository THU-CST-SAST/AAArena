"""Immutable Elo inputs loaded only from this AA-Arena checkout."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class EloPlayer:
    """One verified player and its optional historical scale anchor."""

    player_id: str
    package_root: Path
    anchor_elo: float | None = None
    track: str | None = None


@dataclass(frozen=True)
class EloCase:
    """One role assignment for a fixed pair of Elo players."""

    case_id: str
    player_a: EloPlayer
    player_b: EloPlayer
    player_ids_by_role: tuple[str, ...]
    roles: tuple[str, ...]
    seed: int


def repository_root() -> Path:
    """Return the checkout that owns every Elo input and work product."""

    return REPOSITORY_ROOT.resolve()


def load_verified_players(game: str) -> tuple[EloPlayer, ...]:
    """Load verified players whose packages exist in this checkout's pool."""

    if game not in {"antwar", "antwar2", "aquawar", "dorado", "generals", "lostspace", "lota", "miracle", "monecraft", "pacman", "rollman", "snakego"}:
        raise ValueError("unsupported paper game: " + game)
    root = repository_root()
    pool_root = (root / "games" / game / "players" / "pool").resolve()
    snapshot_path = root / "results" / "availability" / game / "runnable.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    rows = snapshot.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"{snapshot_path}: rows must be a list")

    players: list[EloPlayer] = []
    for row in sorted(rows, key=lambda item: str(item.get("player_id", ""))):
        if not isinstance(row, dict) or row.get("verified") is not True:
            continue
        player_id = row.get("player_id")
        if not isinstance(player_id, str) or not player_id:
            continue
        package_root = (pool_root / player_id).resolve()
        if package_root.parent != pool_root or not package_root.is_dir():
            continue
        raw_anchor = row.get("elo")
        anchor_elo = float(raw_anchor) if isinstance(raw_anchor, (int, float)) else None
        raw_track = row.get("track")
        track = str(raw_track) if raw_track is not None else None
        players.append(EloPlayer(player_id, package_root, anchor_elo, track))
    return tuple(players)


def _circulant_edges(player_count: int, degree: int) -> tuple[tuple[int, int], ...]:
    if player_count < 2:
        raise ValueError("Elo requires at least two players")
    if degree < 1:
        raise ValueError("degree must be positive")
    effective_degree = min(degree, player_count - 1)
    if player_count % 2 and effective_degree % 2:
        effective_degree -= 1
    if effective_degree < 1:
        raise ValueError("degree cannot connect this player count")

    edges: set[tuple[int, int]] = set()
    for offset in range(1, effective_degree // 2 + 1):
        for left in range(player_count):
            right = (left + offset) % player_count
            edges.add(tuple(sorted((left, right))))
    if effective_degree % 2:
        opposite = player_count // 2
        for left in range(opposite):
            edges.add((left, left + opposite))
    return tuple(sorted(edges))


def build_cases(
    players: Sequence[EloPlayer],
    roles: Sequence[str],
    roles_symmetric: bool,
    degree: int,
    seed: int,
) -> tuple[EloCase, ...]:
    """Build a deterministic sparse schedule with both role directions."""

    ordered_players = tuple(sorted(players, key=lambda player: player.player_id))
    ordered_roles = tuple(roles)
    if len(ordered_roles) < 2:
        raise ValueError("head-to-head Elo requires at least two roles")
    if len(ordered_roles) > 2 and not roles_symmetric:
        raise ValueError("non-symmetric multiplayer games need an explicit Elo policy")

    role_groups = {
        role: tuple(player for player in ordered_players if player.track == role)
        for role in ordered_roles
    }
    known_tracks = {player.track for player in ordered_players if player.track is not None}
    if (
        len(ordered_roles) == 2
        and not roles_symmetric
        and known_tracks <= set(ordered_roles)
        and all(role_groups.values())
    ):
        if degree < 1:
            raise ValueError("degree must be positive")
        cases: list[EloCase] = []
        for role_index, role in enumerate(ordered_roles):
            opponent_index = 1 - role_index
            opponents = role_groups[ordered_roles[opponent_index]]
            opponent_count = min(degree, len(opponents))
            for player_index, player in enumerate(role_groups[role]):
                for offset in range(opponent_count):
                    opponent = opponents[(player_index + offset) % len(opponents)]
                    cases.append(
                        EloCase(
                            case_id=f"m{len(cases):08d}",
                            player_a=player,
                            player_b=opponent,
                            player_ids_by_role=(player.player_id, opponent.player_id),
                            roles=(role, ordered_roles[opponent_index]),
                            seed=int(seed),
                        )
                    )
        return tuple(cases)

    cases: list[EloCase] = []
    for left, right in _circulant_edges(len(ordered_players), degree):
        player_a = ordered_players[left]
        player_b = ordered_players[right]
        if len(ordered_roles) == 2:
            assignments = (
                (player_a.player_id, player_b.player_id),
                (player_b.player_id, player_a.player_id),
            )
        else:
            assignments = (
                (player_a.player_id, *(player_b.player_id for _ in ordered_roles[1:])),
                (player_b.player_id, *(player_a.player_id for _ in ordered_roles[1:])),
            )
        for player_ids_by_role in assignments:
            cases.append(
                EloCase(
                    case_id=f"m{len(cases):08d}",
                    player_a=player_a,
                    player_b=player_b,
                    player_ids_by_role=tuple(player_ids_by_role),
                    roles=ordered_roles,
                    seed=int(seed),
                )
            )
    return tuple(cases)


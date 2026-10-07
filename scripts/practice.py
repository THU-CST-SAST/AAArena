#!/usr/bin/env python3
"""Local practice against published opponents; no official Elo or budget."""

import argparse
import json
from pathlib import Path
from aa_arena.benchmark.matches import MatchService
from aa_arena.resources import ARENA_GAMES


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--game", choices=ARENA_GAMES, required=True)
    p.add_argument("--strategy", type=Path)
    p.add_argument("--rank", type=int)
    p.add_argument("--list", action="store_true")
    p.add_argument("--output", type=Path, default=Path("runs/practice"))
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--seed", type=int, default=20260831)
    a = p.parse_args()
    m = MatchService(a.game, a.output, workers=a.workers, seed=a.seed, practice=True)
    if a.list:
        print(
            json.dumps(
                [
                    {"rank": o.rank, "opponent_id": o.opponent_id, "track": o.track}
                    for o in m.opponents
                ],
                indent=2,
            )
        )
        return
    if a.strategy is None or a.rank is None:
        p.error("--strategy and --rank are required for practice")
    selected = tuple(o for o in m.opponents if o.rank == a.rank)
    if len(selected) != 1:
        p.error("Choose one published rank from --list")
    m.preflight_candidate(a.strategy)
    rows = m._run(a.strategy, selected, "practice")
    from dataclasses import asdict

    print(
        json.dumps(
            {
                "scope": "local-practice-only",
                "official_score": False,
                "game": a.game,
                "opponent_rank": a.rank,
                **m._aggregate(rows),
                "seats": [asdict(r) for r in rows],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

from pathlib import Path
import json

from aa_arena.benchmark.matches import freeze_opponents
from aa_arena.benchmark.service import BenchmarkService


def test_committed_match_is_backfilled_into_jsonl_after_restart(tmp_path: Path) -> None:
    run = tmp_path / "run"
    with BenchmarkService(
        run,
        game="antwar",
        model_profile="test-model-a",
        small_budget=1,
        large_budget=1,
        workers=1,
    ) as service:
        service.initialize_workspace()
        snapshot = service._snapshot()
        submission_id, _ = service.ledger.reserve("large", 1, snapshot, {})
        service.ledger.complete(
            submission_id,
            {
                "elo": 1000.0,
                "elo_ci_low": 900.0,
                "elo_ci_high": 1100.0,
                "rank": 10,
                "pool_win_rate": 0.5,
                "wins": 1,
                "draws": 0,
                "losses": 1,
                "candidate_errors": 0,
            },
        )
        assert service.trajectory.read() == []

    with BenchmarkService(
        run,
        game="antwar",
        model_profile="test-model-a",
        small_budget=1,
        large_budget=1,
        workers=1,
    ) as resumed:
        resumed.initialize_workspace()
        events = resumed.trajectory.read()
        assert len(events) == 1
        assert events[0]["submission_id"] == submission_id
        assert events[0]["recovered_event"] is True
        assert (run / "trajectory" / "trajectory.png").is_file()


def test_verified_pool_scale_is_frozen_across_controller_restart(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    ratings = repository / "results" / "elo" / "antwar" / "measured_elo.json"
    package = repository / "games" / "antwar" / "players" / "pool" / "player-a"
    package.mkdir(parents=True)
    ratings.parent.mkdir(parents=True)
    ratings.write_text(
        json.dumps({"ratings": [{"player_id": "player-a", "measured_elo": 1234.0}]})
        + "\n",
        encoding="utf-8",
    )
    snapshot = tmp_path / "controller" / "opponent-pool.json"
    first = freeze_opponents("antwar", repository, snapshot)
    ratings.write_text(
        json.dumps({"ratings": [{"player_id": "player-a", "measured_elo": 9999.0}]})
        + "\n",
        encoding="utf-8",
    )
    resumed = freeze_opponents("antwar", repository, snapshot)
    assert first[0].elo == resumed[0].elo == 1234.0
    assert resumed[0].rank == 1

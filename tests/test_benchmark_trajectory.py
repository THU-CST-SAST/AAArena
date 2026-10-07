from __future__ import annotations

import csv
import json
from pathlib import Path

from aa_arena.benchmark.trajectory import TrajectoryLog, rebuild_report


def _event(kind: str, submission: str, elo: float, rank: int, large_used: int):
    return {
        "schema_version": 1,
        "submission_id": submission,
        "kind": kind,
        "status": "complete",
        "snapshot": {
            "snapshot_id": submission,
            "parent_snapshot_id": None,
            "strategy_hash": submission,
            "changed_files": 1,
            "lines_added": 2,
            "lines_deleted": 0,
        },
        "active_seconds": 10 * large_used,
        "wall_seconds": 12 * large_used,
        "token_usage": {"total_tokens": 100 * large_used},
        "budgets": {"small_used": 3 * large_used, "large_used": large_used},
        "result": {
            "elo": elo,
            "elo_ci_low": elo - 20,
            "elo_ci_high": elo + 20,
            "rank": rank,
            "pool_win_rate": 0.5,
            "wins": 2,
            "draws": 0,
            "losses": 2,
            "candidate_errors": 0,
        },
    }


def test_report_uses_only_baseline_and_large_matches_as_performance_points(tmp_path: Path) -> None:
    log = TrajectoryLog(tmp_path / "trajectory")
    log.append(_event("baseline", "base", 900, 90, 0))
    log.append(_event("small", "small", 9999, 1, 0))
    log.append(_event("large", "large-1", 1000, 70, 1))

    summary = rebuild_report(tmp_path)
    points = json.loads((tmp_path / "trajectory" / "points.json").read_text())
    assert summary["performance_points"] == 2
    assert [point["elo"] for point in points] == [900, 1000]
    assert (tmp_path / "trajectory" / "trajectory.png").is_file()
    assert (tmp_path / "trajectory" / "diagnostics.png").is_file()
    with (tmp_path / "trajectory" / "points.csv").open(newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 2

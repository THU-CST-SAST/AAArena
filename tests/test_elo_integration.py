from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from aa_arena.elo import run_elo


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.game_smoke
def test_native_elo_runs_two_new_pool_matches() -> None:
    work = REPOSITORY_ROOT / "runs" / "elo" / "integration-antwar2"
    shutil.rmtree(work, ignore_errors=True)
    try:
        summary = run_elo(
            "antwar2",
            work,
            workers=1,
            degree=2,
            seed=20260830,
            max_matches=2,
        )
        assert summary.valid_matches == 2
        plan = json.loads((work / "plan.json").read_text(encoding="utf-8"))
        serialized_plan = json.dumps(plan)
        assert plan["repository_root"] == str(REPOSITORY_ROOT)
        assert "AgentBench" not in serialized_plan
        assert all(
            str(REPOSITORY_ROOT / "games" / "antwar2" / "players" / "pool")
            in player["package_root"]
            for player in plan["players"]
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

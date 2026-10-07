"""Check actual published pools and every generated local experiment arm."""
import importlib.util
import json
from pathlib import Path
import pytest
from aa_arena.benchmark.matches import load_opponents, freeze_opponents
from aa_arena.benchmark.experiment import ExperimentConfig
from aa_arena.benchmark.distribution import local_subset
from aa_arena.resources import build_bundle
ROOT = Path(__file__).resolve().parents[1]
GAMES = json.loads((ROOT / "configs/paper.json").read_text())["games"]

@pytest.mark.parametrize("game", GAMES)
def test_subset_identity_and_freeze(game, tmp_path):
    assert local_subset(ROOT)
    opponents = load_opponents(game, ROOT)
    rows = json.loads((ROOT / f"results/elo/{game}/measured_elo.json").read_text())
    rows = rows.get("ratings") if isinstance(rows, dict) else rows
    expected = [(i, row["player_id"]) for i,row in enumerate(rows,1) if i>8 and i%2==0]
    assert [(o.reference_rank,o.opponent_id) for o in opponents] == expected
    assert [o.rank for o in opponents] == list(range(1,len(opponents)+1))
    bundle = build_bundle(game, tmp_path / "resources")
    board = json.loads((bundle / "leaderboard.json").read_text())
    assert board["evaluation_scope"] == "published-subset"
    frozen = freeze_opponents(game,ROOT,tmp_path/"pool.json",leaderboard_path=bundle/"leaderboard.json")
    assert frozen == opponents
    assert freeze_opponents(game,ROOT,tmp_path/"pool.json") == opponents

def test_every_formal_ablation_has_full_pool_targets(tmp_path):
    spec = importlib.util.spec_from_file_location("experiments",ROOT/"scripts/run_experiments.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for suite in mod.SUITES:
        plan = mod.plan(suite,["example"],GAMES,[42],tmp_path,"codex")
        assert plan["evaluation_scope"] == "full-pool"
        for job in plan["jobs"]:
            config = ExperimentConfig(**job["config"])
            config.validate_budgets(job["small_budget"],job["large_budget"])
            from aa_arena.resources import _rating_rows
            pool = _rating_rows(job["game"], ROOT)
            if config.opponent_policy == "ladder":
                assert config.initial_rank <= len(pool)
            if config.is_clone:
                assert config.clone_rank <= len(pool)

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from aa_arena.benchmark.profile import load_profile, write_codex_home
from aa_arena.benchmark.runtime import DYNAMIC_TOOLS
from aa_arena.benchmark.service import BenchmarkService
from aa_arena.benchmark.snapshot import SnapshotStore
from aa_arena.benchmark.trajectory import TrajectoryLog, rebuild_report


def _official(kind: str, rank: int, elo: float) -> dict[str, object]:
    return {
        "kind": kind,
        "elo": elo,
        "elo_ci_low": elo - 20,
        "elo_ci_high": elo + 20,
        "rank": rank,
        "pool_win_rate": 0.5,
        "wins": 2,
        "draws": 0,
        "losses": 2,
        "candidate_errors": 0,
        "per_opponent": [
            {
                "opponent_id": f"opponent-{index:03d}",
                "rank": index,
                "elo": 2000 - index,
                "wins": index % 3,
                "draws": 0,
                "losses": 2 - index % 3,
                "candidate_errors": 0,
                "infrastructure_retries": 0,
            }
            for index in range(191)
        ],
    }


def _service(tmp_path: Path, monkeypatch, *, small: int = 64, large: int = 8):
    monkeypatch.setattr("aa_arena.benchmark.service.rebuild_report", lambda _: None)
    service = BenchmarkService(
        tmp_path / "run",
        game="antwar",
        model_profile="test-model-a",
        small_budget=small,
        large_budget=large,
        workers=1,
    )
    service.initialize_workspace()
    monkeypatch.setattr(service, "_preflight", lambda: None)
    return service


def test_codex_config_caps_context_and_output_without_lowering_reasoning(
    tmp_path: Path,
) -> None:
    profile = load_profile("test-model-a")
    config = write_codex_home(tmp_path, profile, workspace_path=str(tmp_path))
    text = config.read_text(encoding="utf-8")
    document = tomllib.loads(text)
    catalog = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))

    assert profile.reasoning_effort == "max"
    assert 'model_reasoning_effort = "max"' in text
    assert "model_auto_compact_token_limit = 200000" in text
    assert 'model_auto_compact_token_limit_scope = "total"' in text
    assert "tool_output_token_limit = 8192" in text
    assert catalog["models"][0]["truncation_policy"] == {
        "mode": "bytes",
        "limit": 32768,
    }
    filesystem = document["permissions"]["arena"]["filesystem"]
    assert filesystem[str(tmp_path)] == "read"
    assert filesystem[str(tmp_path / "strategy")] == "write"
    assert filesystem[str(tmp_path / "skills")] == "write"
    assert filesystem[str(tmp_path / "notes")] == "write"
    assert filesystem[str(tmp_path / "replays")] == "write"
    assert filesystem[str(tmp_path / "artifacts")] == "write"


def test_match_tools_are_narrow_and_small_match_schema_allows_targeted_checks() -> None:
    assert [tool["name"] for tool in DYNAMIC_TOOLS] == [
        "workspace_shell", "workspace_manifest", "list_opponents", "small_match", "large_match", "restore_champion"
    ]
    schema = DYNAMIC_TOOLS[3]["inputSchema"]["properties"]["opponent_ids"]
    assert schema["minItems"] == 1
    assert schema["maxItems"] == 8


def test_agent_match_result_is_compact_while_raw_evidence_remains_addressable(
    tmp_path: Path, monkeypatch
) -> None:
    with _service(tmp_path, monkeypatch) as service:
        raw = {
            **_official("large", 7, 1800.0),
            "match_id": "match-large",
            "snapshot_id": "snapshot-large",
            "budget": {"small_remaining": 40, "large_remaining": 5},
            "champion": {
                "snapshot_id": "champion-snapshot",
                "match_id": "champion-match",
                "kind": "large",
                "rank": 7,
                "elo": 1800.0,
                "strategy_hash": "champion-hash",
            },
            "non_improving_large_streak": 0,
            "stopped_early": False,
        }
        compact = service.agent_result(raw)

        assert len(json.dumps(compact, ensure_ascii=False)) < 4096
        assert "per_opponent" not in compact
        assert compact["champion"]["snapshot_id"] == "champion-snapshot"
        assert compact["stopped_early"] is False
        assert len(compact["priority_opponents"]) <= 12
        raw_path = service.workspace / compact["raw_result_path"]
        assert json.loads(raw_path.read_text(encoding="utf-8")) == raw


def test_small_match_summary_bounds_replay_paths(tmp_path: Path, monkeypatch) -> None:
    with _service(tmp_path, monkeypatch) as service:
        raw = {
            "kind": "small",
            "match_id": "match-small",
            "snapshot_id": "snapshot-small",
            "budget": {"small_remaining": 40, "large_remaining": 5},
            "wins": 8,
            "draws": 0,
            "losses": 8,
            "candidate_errors": 0,
            "opponents": [f"opponent-{index}" for index in range(8)],
            "seats": [
                {
                    "outcome": "loss" if index % 2 else "win",
                    "replay_path": f"replays/{'x' * 300}-{index}.json",
                }
                for index in range(16)
            ],
        }

        compact = service.agent_result(raw)

        assert len(json.dumps(compact, ensure_ascii=False)) < 4096
        assert len(compact["priority_replay_paths"]) <= 8


def test_snapshot_can_include_curated_iteration_evidence_but_not_resources(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    for name in ("strategy", "notes", "replays", "artifacts", "resources"):
        (workspace / name).mkdir(parents=True)
        (workspace / name / "payload.txt").write_text(name * 1000, encoding="utf-8")
    store = SnapshotStore(tmp_path / "snapshots")

    (workspace / "skills").mkdir()
    (workspace / "skills" / "replay-reading.md").write_text("skill", encoding="utf-8")
    snapshot = store.create(
        workspace,
        include_roots=("strategy", "skills", "notes", "replays", "artifacts"),
    )
    manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))

    assert {row["path"] for row in manifest["files"]} == {
        "strategy/payload.txt",
        "skills/replay-reading.md",
        "notes/payload.txt",
        "replays/payload.txt",
        "artifacts/payload.txt",
    }
    assert all(not row["path"].startswith("resources/") for row in manifest["files"])
    assert snapshot.archive_path.stat().st_size < 50_000


def test_snapshot_delta_reuses_unchanged_files_and_materializes_chain(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "strategy").mkdir(parents=True)
    (workspace / "notes").mkdir()
    (workspace / "strategy" / "main.py").write_text("stable\n", encoding="utf-8")
    (workspace / "notes" / "checkpoint.md").write_text("one\n", encoding="utf-8")
    store = SnapshotStore(tmp_path / "snapshots")
    first = store.create(workspace, include_roots=("strategy", "notes"))
    (workspace / "notes" / "checkpoint.md").write_text("two\n", encoding="utf-8")
    second = store.create(
        workspace,
        parent_snapshot_id=first.snapshot_id,
        include_roots=("strategy", "notes"),
    )

    second_manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert second_manifest["stored_files"] == ["notes/checkpoint.md"]
    materialized = store.materialize(second.snapshot_id, tmp_path / "materialized")
    assert (materialized / "strategy" / "main.py").read_text() == "stable\n"
    assert (materialized / "notes" / "checkpoint.md").read_text() == "two\n"


def test_report_uses_only_latest_baseline_generation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("aa_arena.benchmark.trajectory._plot", lambda *_: None)
    monkeypatch.setattr("aa_arena.benchmark.trajectory._plot_diagnostics", lambda *_: None)
    log = TrajectoryLog(tmp_path / "trajectory")

    def event(kind: str, submission: str, rank: int, large_used: int):
        return {
            "schema_version": 1,
            "submission_id": submission,
            "kind": kind,
            "status": "complete",
            "snapshot": {"snapshot_id": submission, "strategy_hash": submission},
            "budgets": {"small_used": 0, "large_used": large_used},
            "token_usage": {"total_tokens": 100 * large_used},
            "result": {
                "elo": 1000 + rank,
                "elo_ci_low": 900,
                "elo_ci_high": 1100,
                "rank": rank,
            },
        }

    log.append(event("baseline", "old-base", 40, 0))
    log.append(event("large", "old-large", 30, 1))
    log.append(event("baseline", "new-base", 20, 0))
    log.append(event("large", "new-large", 10, 1))

    summary = rebuild_report(tmp_path)
    points = json.loads((tmp_path / "trajectory/points.json").read_text())
    assert [point["snapshot_id"] for point in points] == ["new-base", "new-large"]
    assert summary["events"] == 2
    assert summary["archived_events"] == 2


def test_early_stop_reports_saved_champion_as_final(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("aa_arena.benchmark.trajectory._plot", lambda *_: None)
    monkeypatch.setattr("aa_arena.benchmark.trajectory._plot_diagnostics", lambda *_: None)
    log = TrajectoryLog(tmp_path / "trajectory")

    def event(submission: str, rank: int, large_used: int):
        return {
            "schema_version": 1,
            "submission_id": submission,
            "kind": "baseline" if large_used == 0 else "large",
            "status": "complete",
            "snapshot": {"snapshot_id": submission, "strategy_hash": submission},
            "budgets": {"small_used": 0, "large_used": large_used},
            "token_usage": {"total_tokens": 100 * large_used},
            "result": {
                "elo": 1200 - rank,
                "elo_ci_low": 1100,
                "elo_ci_high": 1300,
                "rank": rank,
            },
        }

    log.append(event("baseline", 20, 0))
    log.append(event("champion", 10, 1))
    regression = event("regression", 15, 2)
    regression["result"].update(
        stopped_early=True,
        champion={"snapshot_id": "champion", "rank": 10, "elo": 1190},
    )
    log.append(regression)

    summary = rebuild_report(tmp_path)

    assert summary["last_evaluated"]["snapshot_id"] == "regression"
    assert summary["final"]["snapshot_id"] == "champion"


def test_budget_exhaustion_reports_saved_champion_as_final(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("aa_arena.benchmark.trajectory._plot", lambda *_: None)
    monkeypatch.setattr("aa_arena.benchmark.trajectory._plot_diagnostics", lambda *_: None)
    log = TrajectoryLog(tmp_path / "trajectory")

    def event(submission: str, rank: int, large_used: int):
        return {
            "schema_version": 1,
            "submission_id": submission,
            "kind": "baseline" if large_used == 0 else "large",
            "status": "complete",
            "snapshot": {"snapshot_id": submission, "strategy_hash": submission},
            "budgets": {"small_used": 0, "large_used": large_used},
            "token_usage": {"total_tokens": 100 * large_used},
            "result": {
                "elo": 1200 - rank,
                "elo_ci_low": 1100,
                "elo_ci_high": 1300,
                "rank": rank,
            },
        }

    log.append(event("baseline", 20, 0))
    log.append(event("champion", 10, 1))
    regression = event("regression", 15, 2)
    regression["result"].update(
        stopped_early=False,
        champion={"snapshot_id": "champion", "rank": 10, "elo": 1190},
    )
    log.append(regression)

    summary = rebuild_report(tmp_path)

    assert summary["last_evaluated"]["snapshot_id"] == "regression"
    assert summary["final"]["snapshot_id"] == "champion"


def test_small_matches_allow_one_to_eight_opponents(tmp_path: Path, monkeypatch) -> None:
    with _service(tmp_path, monkeypatch) as service:
        monkeypatch.setattr(
            service,
            "_execute_pending",
            lambda pending, snapshot: {
                "kind": "small",
                "wins": 1,
                "draws": 0,
                "losses": 1,
                "opponents": [],
                "seats": [],
            },
        )
        ids = [item.opponent_id for item in service.matches.opponents[:9]]
        result = service.small_match(ids[:1])
        assert result["budget"]["small_used"] == 1
        with pytest.raises(ValueError, match="between 1 and 8"):
            service.small_match(ids)


def test_small_budget_is_independent_from_large_calibration_phases(
    tmp_path: Path, monkeypatch
) -> None:
    with _service(tmp_path, monkeypatch, small=24) as service:
        monkeypatch.setattr(
            service,
            "_execute_pending",
            lambda pending, snapshot: {
                "kind": "small",
                "wins": 1,
                "draws": 0,
                "losses": 1,
                "opponents": [],
                "seats": [],
            },
        )
        ids = [item.opponent_id for item in service.matches.opponents[:20]]
        for start in range(0, 20, 4):
            service.small_match(ids[start : start + 4])

        assert service.ledger.budgets().small_used == 20
        assert service.ledger.budgets().large_used == 0


def test_regressions_preserve_working_strategy_and_never_plateau_stop(
    tmp_path: Path, monkeypatch
) -> None:
    with _service(tmp_path, monkeypatch, small=0, large=5) as service:
        results = iter(
            [
                _official("large", 10, 1000.0),
                _official("large", 8, 1200.0),
                _official("large", 9, 1100.0),
                _official("large", 9, 1150.0),
                _official("large", 9, 1175.0),
            ]
        )
        monkeypatch.setattr(service, "_execute_pending", lambda *_: next(results))
        strategy = next((service.workspace / "strategy").rglob("main.py"))
        strategy.write_text("baseline\n")
        baseline = service.baseline()
        strategy.write_text("champion\n")
        champion = service.large_match()
        strategy.write_text("regression-one\n")
        first_miss = service.large_match()
        assert strategy.read_text() == "regression-one\n"
        strategy.write_text("regression-two\n")
        second_miss = service.large_match()
        assert second_miss["stopped_early"] is False
        assert service.ledger.state()["status"] == "running"
        strategy.write_text("regression-three\n")
        third_miss = service.large_match()

        state = service.ledger.state()
        assert baseline["champion"]["snapshot_id"] == baseline["snapshot_id"]
        assert champion["champion"]["snapshot_id"] == champion["snapshot_id"]
        assert first_miss["non_improving_large_streak"] == 1
        assert second_miss["non_improving_large_streak"] == 2
        assert third_miss["non_improving_large_streak"] == 3
        assert third_miss["stopped_early"] is False
        assert state["status"] == "running"
        assert state["frozen_snapshot_id"] is None
        assert strategy.read_text() == "regression-three\n"


def test_restore_champion_is_explicit_and_does_not_spend_budget(tmp_path: Path, monkeypatch) -> None:
    with _service(tmp_path, monkeypatch, small=0, large=4) as service:
        results = iter(
            [
                _official("large", 20, 1000.0),
                _official("large", 8, 1200.0),
                _official("large", 9, 1100.0),
                _official("large", 9, 1150.0),
            ]
        )
        monkeypatch.setattr(service, "_execute_pending", lambda *_: next(results))
        strategy = next((service.workspace / "strategy").rglob("main.py"))
        strategy.write_text("baseline\n")
        service.baseline()
        strategy.write_text("champion\n")
        champion = service.large_match()
        strategy.write_text("experimental-branch\n")
        before = service.ledger.budgets().as_dict()

        restored = service.tool_call("restore_champion", {})
        visible = service.agent_result(restored)

        assert restored["kind"] == "restore_champion"
        assert visible == restored
        assert restored["snapshot_id"] == champion["snapshot_id"]
        assert strategy.read_text() == "champion\n"
        assert service.ledger.budgets().as_dict() == before


def test_rank_one_is_the_only_early_stop(tmp_path: Path, monkeypatch) -> None:
    with _service(tmp_path, monkeypatch, small=0, large=5) as service:
        results = iter(
            [
                _official("large", 20, 1000.0),
                _official("large", 1, 2200.0),
            ]
        )
        monkeypatch.setattr(service, "_execute_pending", lambda *_: next(results))
        strategy = next((service.workspace / "strategy").rglob("main.py"))
        strategy.write_text("baseline\n")
        service.baseline()
        strategy.write_text("rank-one\n")
        champion = service.large_match()

        state = service.ledger.state()
        assert champion["stopped_early"] is True
        assert state["status"] == "finalizing"
        assert state["frozen_snapshot_id"] == champion["snapshot_id"]
        assert state["large_used"] == 1
        assert state["large_total"] == 5


def test_last_large_evaluates_challenger_then_freezes_saved_champion(
    tmp_path: Path, monkeypatch
) -> None:
    with _service(tmp_path, monkeypatch, small=0, large=1) as service:
        observed = []

        def execute(pending, snapshot):
            strategy = next((service.workspace / "strategy").rglob("main.py"))
            observed.append(strategy.read_text())
            return _official("large", 10, 1000.0)

        monkeypatch.setattr(service, "_execute_pending", execute)
        strategy = next((service.workspace / "strategy").rglob("main.py"))
        strategy.write_text("champion\n")
        baseline = service.baseline()
        strategy.write_text("untested-final-challenger\n")
        final = service.large_match()

        assert observed == ["champion\n", "untested-final-challenger\n"]
        assert final["snapshot_id"] != baseline["snapshot_id"]
        assert service.ledger.state()["frozen_snapshot_id"] == baseline["snapshot_id"]
        assert strategy.read_text() == "champion\n"

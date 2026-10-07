from __future__ import annotations

from pathlib import Path

import pytest

from aa_arena.benchmark.ledger import BudgetError, RunLedger
from aa_arena.benchmark.snapshot import SnapshotStore


def _snapshot(tmp_path: Path):
    workspace = tmp_path / "workspace"
    (workspace / "strategy").mkdir(parents=True)
    (workspace / "strategy" / "main.py").write_text("print('strategy')\n", encoding="utf-8")
    return SnapshotStore(tmp_path / "snapshots").create(workspace)


def test_budget_is_debited_atomically_when_submission_is_reserved(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    with RunLedger(tmp_path / "ledger.sqlite3") as ledger:
        ledger.initialize(game="antwar", model_profile="test-model-a", small_budget=2, large_budget=1)
        submission_id, _ = ledger.reserve("small", 2, snapshot, {"opponent_ids": ["one", "two"]})
        assert ledger.budgets().small_remaining == 0
        assert ledger.budgets().as_dict() == {
            "small_total": 2,
            "small_used": 2,
            "small_remaining": 0,
            "large_total": 1,
            "large_used": 0,
            "large_remaining": 1,
        }
        with pytest.raises(BudgetError, match="exhausted"):
            ledger.reserve("small", 1, snapshot, {"opponent_ids": ["three"]})
        ledger.complete(submission_id, {"wins": 1, "losses": 1})


def test_last_large_match_freezes_submitted_snapshot(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    with RunLedger(tmp_path / "ledger.sqlite3") as ledger:
        ledger.initialize(game="antwar", model_profile="test-model-a", small_budget=0, large_budget=1)
        submission_id, _ = ledger.reserve("large", 1, snapshot, {})
        ledger.complete(submission_id, {"elo": 1234})
        state = ledger.state()
        assert state["status"] == "finalizing"
        assert state["frozen_snapshot_id"] == snapshot.snapshot_id
        assert ledger.begin_final_review() is True
        assert ledger.begin_final_review() is False
        assert ledger.state()["status"] == "reviewing"


def test_early_finalize_rejects_snapshot_without_completed_official_result(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    with RunLedger(tmp_path / "ledger.sqlite3") as ledger:
        ledger.initialize(game="antwar", model_profile="test-model-a", small_budget=0, large_budget=2)
        with pytest.raises(BudgetError, match="completed official"):
            ledger.finalize_early(snapshot.snapshot_id, reason="test")


def test_incompatible_resume_configuration_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    with RunLedger(path) as ledger:
        ledger.initialize(game="antwar", model_profile="test-model-a", small_budget=128, large_budget=16)
    with RunLedger(path) as ledger:
        with pytest.raises(ValueError, match="does not match"):
            ledger.initialize(
                game="snakego", model_profile="test-model-a", small_budget=128, large_budget=16
            )


def test_pending_submission_survives_restart_without_a_second_debit(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    path = tmp_path / "ledger.sqlite3"
    with RunLedger(path) as ledger:
        ledger.initialize(game="antwar", model_profile="test-model-a", small_budget=2, large_budget=1)
        submission_id, _ = ledger.reserve("small", 1, snapshot, {"opponent_ids": ["rank01"]})
    with RunLedger(path) as ledger:
        ledger.initialize(game="antwar", model_profile="test-model-a", small_budget=2, large_budget=1)
        pending = ledger.pending_submission()
        assert pending is not None
        assert pending["submission_id"] == submission_id
        assert pending["request"] == {"opponent_ids": ["rank01"]}
        assert ledger.budgets().small_used == 1
        with pytest.raises(BudgetError, match="still evaluating"):
            ledger.reserve("large", 1, snapshot, {})


def test_one_extension_preserves_history_usage_thread_and_champion(tmp_path):
    snapshot = _snapshot(tmp_path)
    path = tmp_path / 'ledger.sqlite3'
    with RunLedger(path) as ledger:
        ledger.initialize(game='antwar', model_profile='test-model-a', small_budget=2, large_budget=1)
        ledger.update_runtime(thread_id='same-thread', token_usage={'total_tokens': 123},
                              metadata={'champion': {'snapshot_id': snapshot.snapshot_id, 'rank': 15}})
        small, _ = ledger.reserve('small', 1, snapshot, {})
        ledger.complete(small, {'wins': 1})
        large, _ = ledger.reserve('large', 1, snapshot, {})
        ledger.complete(large, {'rank': 15, 'elo': 1200})
        ledger.begin_final_review()
        ledger.mark_complete()
        history = ledger.submissions()
        ledger.extend_budget_once(small=2, large=1)
        state = ledger.state()
        assert (state['small_total'], state['large_total']) == (4, 2)
        assert (state['small_used'], state['large_used']) == (1, 1)
        assert ledger.budgets().small_remaining == 2
        assert ledger.budgets().as_dict()['small_expired'] == 1
        with pytest.raises(BudgetError):
            ledger.reserve('small', 3, snapshot, {})
        assert state['status'] == 'running' and state['frozen_snapshot_id'] is None
        assert state['thread_id'] == 'same-thread' and state['token_usage']['total_tokens'] == 123
        assert state['metadata']['budget_extension']['first_stage_champion']['rank'] == 15
        assert ledger.submissions() == history
    with RunLedger(path) as ledger:
        ledger.extend_budget_once(small=2, large=1)
        assert ledger.budgets().large_total == 2
        last, _ = ledger.reserve('large', 1, snapshot, {})
        ledger.complete(last, {'rank': 10})
        ledger.begin_final_review()
        ledger.mark_complete()
        ledger.extend_budget_once(small=2, large=1)
        assert ledger.state()['status'] == 'complete'
        assert ledger.budgets().large_total == 2
        with pytest.raises(BudgetError):
            ledger.extend_budget_once(small=4, large=2)


def test_paper_continuation_adds_256_32_to_same_128_16_run(tmp_path):
    snapshot = _snapshot(tmp_path)
    with RunLedger(tmp_path/'ledger.sqlite3') as ledger:
        ledger.initialize(game='miracle',model_profile='default',small_budget=128,large_budget=16)
        ledger.update_runtime(thread_id='original-session',token_usage={'total_tokens':98765})
        small,_=ledger.reserve('small',128,snapshot,{})
        ledger.complete(small,{'wins':64})
        for _ in range(16):
            large,_=ledger.reserve('large',1,snapshot,{})
            ledger.complete(large,{'rank':26,'elo':1436.8})
        ledger.begin_final_review();ledger.mark_complete()
        before=ledger.state();history=ledger.submissions()
        ledger.extend_budget_once(small=256,large=32)
        ledger.extend_budget_once(small=256,large=32)
        after=ledger.state();budget=ledger.budgets()
        assert (budget.small_total,budget.large_total)==(384,48)
        assert (budget.small_remaining,budget.large_remaining)==(256,32)
        assert after['run_id']==before['run_id']
        assert after['thread_id']=='original-session'
        assert after['token_usage']==before['token_usage']
        assert ledger.submissions()==history
        assert after['metadata']['budget_extension']['first_stage']['small_total']==128
        assert after['metadata']['budget_extension']['first_stage']['large_total']==16

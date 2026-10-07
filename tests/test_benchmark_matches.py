from __future__ import annotations

from pathlib import Path

import pytest

from aa_arena.benchmark.matches import MatchService, Opponent, SeatResult
from aa_arena.benchmark.matches import MatchInfrastructureError
from aa_arena.core.contract import EvaluateResult, EvaluationStatus
from types import SimpleNamespace


@pytest.mark.parametrize('status,diagnostic', [
    (EvaluationStatus.GAME_ERROR, 'backend exited without winner'),
    (EvaluationStatus.INFRA_ERROR, 'build failed (make all): backend failure'),
    (EvaluationStatus.INFRA_ERROR, '/sdk/missing.cpp'),
])
def test_unresolved_errors_never_enter_ratings(tmp_path, monkeypatch, status, diagnostic):
    service = MatchService.__new__(MatchService)
    service.game = 'lota'
    service.roles = ('P0', 'P1')
    service.seed = 1
    service.workers = 1
    service.infrastructure_retries = 0
    service.repository = tmp_path
    service.hidden_root = tmp_path / 'matches'
    service.build_root = tmp_path / 'build'
    monkeypatch.setattr('aa_arena.benchmark.matches._configured_evaluator',
        lambda *args: SimpleNamespace(evaluate=lambda *args: EvaluateResult(
            status=status, diagnostic=diagnostic)))
    with pytest.raises(MatchInfrastructureError):
        service._run(tmp_path, (_opponent('other', 1500, 1),), 'submission')


def _opponent(opponent_id: str, elo: float, rank: int) -> Opponent:
    return Opponent(opponent_id, elo, rank, Path(f"/{opponent_id}"))


def test_ai9_preflight_rejects_dummy_makefile_without_callback(tmp_path):
    service = MatchService.__new__(MatchService)
    service.game = 'lota'
    service.roles = ('P0', 'P1')
    service.repository = Path(__file__).resolve().parents[1]
    service.hidden_root = tmp_path / 'matches'
    service.build_root = tmp_path / 'build'
    strategy = tmp_path / 'strategy'
    strategy.mkdir()
    (strategy / 'Makefile').write_text('all:\n\tg++ main.cpp -o main\n')
    (strategy / 'main.cpp').write_text('int main() { return 0; }\n')
    with pytest.raises(ValueError, match='preflight'):
        service.preflight_candidate(strategy)


def _result(opponent_id: str, score: float) -> SeatResult:
    outcome = "win" if score == 1 else "loss" if score == 0 else "draw"
    return SeatResult(
        opponent_id=opponent_id,
        candidate_roles=("player1",),
        status="complete",
        outcome=outcome,
        score=score,
        winner=None,
        rounds=1,
        diagnostic=None,
        replay_path=None,
        attempts=1,
        seed=1,
    )


def test_candidate_elo_uses_complete_pool_neutral_anchor_not_observed_median() -> None:
    opponents = {
        "low-a": _opponent("low-a", 0.0, 2),
        "low-b": _opponent("low-b", 0.0, 3),
        "high": _opponent("high", 300.0, 1),
    }
    _, ci_low, ci_high, prior_anchor = MatchService._fit_fixed_candidate(
        (_result("low-a", 1.0),), opponents
    )
    assert prior_anchor == pytest.approx(100.0)
    assert ci_low < ci_high

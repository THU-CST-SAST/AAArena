from pathlib import Path
from types import SimpleNamespace

import pytest

from games.snakego.evaluator import SnakeGoEvaluator
from games.snakego.evaluator.arena import MatchCase, SnakeGoMatchError, _result_from_end
from aa_arena.benchmark.matches import MatchService, Opponent
from aa_arena.core.contract import EvaluateResult, EvaluationStatus


@pytest.mark.parametrize("failed,role", [(0, "P0"), (0, "P1"), (1, "P0"), (1, "P1")])
def test_official_player_error_preserves_winner_and_replay(tmp_path, monkeypatch, failed, role):
    monkeypatch.setattr("games.snakego.evaluator.replay.load_replay", lambda _: {
        "end_info": {"type": "PLAYER_ERROR", "err": "runError"}, "round_info": [{}, {}]})
    scores = {"0": 0, "1": 0}; scores[str(failed)] = -1
    result = _result_from_end(MatchCase("candidate", "opponent", role, 1),
        {"end_info": scores}, tmp_path / "replay.json", tmp_path / "events.jsonl", (0, 0, 0))
    public = SnakeGoEvaluator.__new__(SnakeGoEvaluator)._to_evaluate_result(result, role)
    assert public.status is EvaluationStatus.GAME_ERROR
    assert public.winner == f"P{1 - failed}"
    assert public.scores[f"P{failed}"] == -1
    assert public.replay_path == str(tmp_path / "replay.json")
    assert public.rounds == 2
    service = MatchService.__new__(MatchService)
    service.game = "snakego"; service.roles = ("P0", "P1"); service.seed = 7
    service.hidden_root = tmp_path; service.build_root = tmp_path / "build"
    service.repository = tmp_path; service.infrastructure_retries = 0
    monkeypatch.setattr("aa_arena.benchmark.matches._configured_evaluator",
        lambda *args: SimpleNamespace(evaluate=lambda *args: public))
    row = service._evaluate_seat(tmp_path, Opponent("b", 1500, 1, tmp_path), (role,), 0, "case")
    assert row.outcome == ("loss" if role == f"P{failed}" else "win")
    assert row.score == (0 if role == f"P{failed}" else 1)


def test_missing_official_winner_is_never_a_draw(tmp_path, monkeypatch):
    public = EvaluateResult(status=EvaluationStatus.GAME_ERROR, diagnostic="broken game")
    monkeypatch.setattr("aa_arena.benchmark.matches._configured_evaluator",
        lambda *args: SimpleNamespace(evaluate=lambda *args: public))
    service = MatchService.__new__(MatchService)
    service.game = "snakego"; service.roles = ("P0", "P1"); service.seed = 7
    service.hidden_root = tmp_path; service.build_root = tmp_path / "build"
    service.repository = tmp_path; service.infrastructure_retries = 0
    row = service._evaluate_seat(tmp_path, Opponent("b", 1500, 1, tmp_path), ("P0",), 0, "case")
    assert row.status == "infra_error"
    assert row.score is None and row.outcome is None


def test_nonfinite_terminal_scores_are_rejected(tmp_path):
    with pytest.raises(SnakeGoMatchError, match="finite"):
        _result_from_end(MatchCase("a", "b", "P0", 1), {"end_info": {"0": float("nan"), "1": 1}},
            tmp_path / "replay.json", tmp_path / "events.jsonl", (0, 0, 0))

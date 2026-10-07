import json
from types import SimpleNamespace

import pytest

from aa_arena.benchmark.matches import MatchService, Opponent
from aa_arena.core.contract import EvaluationStatus
from aa_arena.legacy.ai9 import Ai9GameConfig, _parse_result


@pytest.mark.parametrize("game", ["lota", "dorado", "monecraft", "pacman"])
@pytest.mark.parametrize("candidate_role", ["P0", "P1"])
def test_official_forfeit_is_charged_to_the_failed_player(tmp_path, monkeypatch, game, candidate_role):
    if game in ("lota", "dorado"):
        (tmp_path / "replay.txt").write_text("Round:20\nwinner:1\nAI STATUS:AI(ID:0)Failed,status:TIMED_OUT msg:;\n")
    elif game == "monecraft":
        (tmp_path / "result.txt").write_text(json.dumps({"rounds": 20, "result": {"type": "error", "winner": 1}}))
    else:
        (tmp_path / "result.txt").write_text(json.dumps({"breadcrumbs": [{}], "error": [0], "error_type": ["EXITED"]}))
    result = _parse_result(Ai9GameConfig(game, tmp_path, (), (), ("P0", "P1"), 2), tmp_path, 0)
    assert result.status is EvaluationStatus.GAME_ERROR
    assert result.winner == "P1"
    assert result.payload["failed_roles"] == ["P0"]
    monkeypatch.setattr("aa_arena.benchmark.matches._configured_evaluator",
        lambda *args: SimpleNamespace(evaluate=lambda *args: result))
    service = MatchService.__new__(MatchService)
    service.game = game; service.roles = ("P0", "P1"); service.seed = 7
    service.hidden_root = tmp_path; service.build_root = tmp_path / "build"
    service.repository = tmp_path; service.infrastructure_retries = 0
    seat = service._evaluate_seat(tmp_path, Opponent("other", 1500, 1, tmp_path), (candidate_role,), 0, "case")
    summary = service._aggregate([seat])
    assert summary["candidate_errors"] == int(candidate_role == "P0")
    assert summary["opponent_errors"] == int(candidate_role == "P1")
    assert summary["game_errors"] == 1
    assert seat.score == int(candidate_role == "P1")

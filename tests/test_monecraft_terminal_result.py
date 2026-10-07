import json
from types import SimpleNamespace

import pytest

from aa_arena.benchmark.matches import MatchService, Opponent
from aa_arena.core.contract import EvaluationStatus
from aa_arena.legacy.ai9 import Ai9GameConfig, _parse_result


def parse(tmp_path, terminal, returncode=0, game="monecraft"):
    (tmp_path / "result.txt").write_text(json.dumps({"rounds": 999, "result": terminal}))
    return _parse_result(Ai9GameConfig(game, tmp_path, (), (), ("P0", "P1"), 2), tmp_path, returncode)


def test_normal_draw_is_accepted_without_retry(tmp_path, monkeypatch):
    result = parse(tmp_path, {"type": "normal", "winner": -1})
    assert result.status is EvaluationStatus.COMPLETE
    assert result.winner is None
    assert result.scores == {"P0": .5, "P1": .5}
    assert result.payload["official_draw"] is True
    calls = []

    def evaluate(*args):
        calls.append(args)
        return result

    monkeypatch.setattr("aa_arena.benchmark.matches._configured_evaluator",
        lambda *args: SimpleNamespace(evaluate=evaluate))
    service = MatchService.__new__(MatchService)
    service.game = "monecraft"; service.roles = ("P0", "P1"); service.seed = 7
    service.hidden_root = tmp_path; service.build_root = tmp_path / "build"
    service.repository = tmp_path; service.infrastructure_retries = 3
    row = service._evaluate_seat(tmp_path, Opponent("b", 1500, 1, tmp_path), ("P0",), 0, "case")
    assert row.outcome == "draw" and row.score == .5
    assert len(calls) == 1


@pytest.mark.parametrize("winner", [0, 1])
def test_player_failure_keeps_official_adjudication(tmp_path, winner):
    result = parse(tmp_path, {"type": "error", "winner": winner})
    assert result.status is EvaluationStatus.GAME_ERROR
    assert result.winner == f"P{winner}"
    assert f"ID:{1 - winner}" in result.diagnostic


@pytest.mark.parametrize("terminal", [
    {"type": "error", "winner": -1}, {"type": "normal", "winner": 777},
    {"winner": -1}, {"type": "normal", "winner": True},
])
def test_invalid_terminal_is_rejected(tmp_path, terminal):
    assert parse(tmp_path, terminal).status is EvaluationStatus.INFRA_ERROR


def test_truncated_draw_document_is_rejected(tmp_path):
    (tmp_path / "result.txt").write_text('{"result":{"type":"normal","winner":-1}')
    config = Ai9GameConfig("monecraft", tmp_path, (), (), ("P0", "P1"), 2)
    assert _parse_result(config, tmp_path, 0).status is EvaluationStatus.INFRA_ERROR


def test_signal_still_invalidates_draw(tmp_path):
    assert parse(tmp_path, {"type": "normal", "winner": -1}, -11).status is EvaluationStatus.INFRA_ERROR


@pytest.mark.parametrize("game", ["lota", "dorado"])
def test_minus_one_does_not_become_draw_in_other_games(tmp_path, game):
    assert parse(tmp_path, {"type": "normal", "winner": -1}, game=game).status is EvaluationStatus.INFRA_ERROR

"""The public entry point distinguishes official results from successful players."""
import importlib
import json
from types import SimpleNamespace

import pytest

from aa_arena.core.contract import EvaluateResult, EvaluationStatus, PlayerRef

entry = importlib.import_module("aa_arena.core.evaluator")


def invoke(monkeypatch, tmp_path, events, *, game="lostspace", status=EvaluationStatus.COMPLETE):
    replay = tmp_path / "replay.json"
    replay.write_text("[]")
    (tmp_path / "transport-events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    roles = ["P0", "P1", "P2", "P3"] if game == "lostspace" else ["P0", "P1"]
    original = EvaluateResult(status=status, winner=roles[-1], scores={r: i + 1 for i, r in enumerate(roles)},
                              rounds=1, replay_path=str(replay), payload={"receipt": "retained"})
    fake = SimpleNamespace(evaluate=lambda *args: original)
    monkeypatch.setattr(entry, "get_plugin", lambda *args: SimpleNamespace(evaluator_factory=lambda root: fake))
    result = entry.evaluate(game, [PlayerRef(r) for r in roles], roles, 123, games_root=tmp_path)
    assert (result.winner, result.scores, result.rounds, result.replay_path) == (
        original.winner, original.scores, original.rounds, original.replay_path)
    assert result.payload["receipt"] == "retained"
    return result


def test_all_players_crash_but_referee_still_returns_ranking(monkeypatch, tmp_path):
    events = [{"kind": "ai_error", "player": i, "error_log": "runError"} for i in range(4)]
    result = invoke(monkeypatch, tmp_path, events + [{"kind": "game_over"}])
    assert result.status is EvaluationStatus.GAME_ERROR
    assert result.payload["failed_roles"] == ["P0", "P1", "P2", "P3"]
    assert all(role in result.diagnostic for role in result.payload["failed_roles"])


def test_cleanup_exit_after_game_over_is_not_a_player_failure(monkeypatch, tmp_path):
    result = invoke(monkeypatch, tmp_path, [{"kind": "game_over"}, {"kind": "ai_error", "player": 0}])
    assert result.status is EvaluationStatus.COMPLETE
    assert "failed_roles" not in result.payload


@pytest.mark.parametrize("state", ["RE", "TLE", "OLE", "IA"])
def test_official_antwar2_forfeit_is_attributed(monkeypatch, tmp_path, state):
    result = invoke(monkeypatch, tmp_path, [{"kind": "game_over", "message": {"end_state": [state, "OK"]}}], game="antwar2")
    assert result.status is EvaluationStatus.GAME_ERROR
    assert result.payload["failed_roles"] == ["P0"]


def test_infrastructure_failure_is_not_reclassified(monkeypatch, tmp_path):
    result = invoke(monkeypatch, tmp_path, [{"kind": "ai_error", "player": 0}], status=EvaluationStatus.INFRA_ERROR)
    assert result.status is EvaluationStatus.INFRA_ERROR

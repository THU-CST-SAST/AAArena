import json
from pathlib import Path
from types import SimpleNamespace
from aa_arena.benchmark.matches import MatchService
from aa_arena.core.contract import EvaluateResult, EvaluationStatus


def test_opponent_forfeit_is_not_charged_as_candidate_error(tmp_path,monkeypatch):
    import aa_arena.benchmark.matches as module
    replay=tmp_path/'replay.json';replay.write_text('{}')
    (tmp_path/'transport-events.jsonl').write_text(json.dumps({'kind':'ai_error','player':1,'error_log':'runError'})+'\n')
    evaluator=SimpleNamespace(evaluate=lambda *args:EvaluateResult(status=EvaluationStatus.GAME_ERROR,winner='P0',scores={'P0':1.,'P1':0.},diagnostic='official player forfeit: runError',replay_path=str(replay)))
    monkeypatch.setattr(module,'_configured_evaluator',lambda *args:evaluator)
    monkeypatch.setattr(module,'_candidate_package',lambda path,roles:path)
    service=object.__new__(MatchService)
    service.game='snakego';service.roles=('P0','P1');service.seed=123
    service.hidden_root=tmp_path;service.build_root=tmp_path;service.repository=tmp_path;service.infrastructure_retries=2
    opponent=SimpleNamespace(opponent_id='public-opponent',package_root=tmp_path)
    result=service._evaluate_seat_unadmitted(tmp_path,opponent,('P0',),0,'test')
    assert result.outcome=='win' and result.failed_roles==('P1',)
    aggregate=service._aggregate([result])
    assert aggregate['candidate_errors']==0 and aggregate['opponent_errors']==1

import json
from pathlib import Path
import pytest
from games.aquawar.evaluator.arena import _adjudicated_replay, match_result_from_replay
from games.aquawar.evaluator import AquaWarEvaluator
from aa_arena.core.contract import EvaluationStatus

@pytest.mark.parametrize('raw_score,official,winner',[(0,(0.,100.),1),(1,(0.,100.),1),(-1,(100.,0.),0),(1,(0.,0.),None)])
def test_official_forfeit_and_draw_override_stale_replay_without_destroying_raw(tmp_path,raw_score,official,winner):
    source=tmp_path/'replay.json';original=json.dumps([{'score':raw_score,'rounds':0}]);source.write_text(original)
    corrected=_adjudicated_replay(source,official)
    assert source.read_text()==original and corrected!=source
    result=match_result_from_replay(corrected,candidate_id='c',opponent_id='o',role='P0',seed=7)
    assert result.payload['winner_player']==winner
    assert json.loads(corrected.read_text())[-1]['adjudication_source']=='official_end_info'

def test_agreeing_normal_result_is_unchanged(tmp_path):
    source=tmp_path/'replay.json';source.write_text('[{"score":2,"rounds":3}]')
    assert _adjudicated_replay(source,(100.,0.))==source

def test_transport_crash_is_attributed_after_official_adjudication(tmp_path):
    source=tmp_path/'replay.json';source.write_text('[{"score":0,"rounds":0}]')
    events=tmp_path/'events.jsonl';events.write_text(json.dumps({'kind':'ai_error','player':0,'error_log':'runError'})+'\n')
    result=match_result_from_replay(_adjudicated_replay(source,(0.,100.)),candidate_id='c',opponent_id='o',role='P0',seed=7,events_path=events)
    actual=object.__new__(AquaWarEvaluator)._to_evaluate_result(result,'P0')
    assert actual.status is EvaluationStatus.GAME_ERROR
    assert actual.winner=='P1' and actual.payload['failed_roles']==['P0']

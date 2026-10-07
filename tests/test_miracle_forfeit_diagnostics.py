import json
from pathlib import Path
import pytest
from games.miracle.evaluator import MiracleEvaluator, MatchCase, MatchResult
from aa_arena.core.contract import EvaluationStatus

@pytest.mark.parametrize('failed_seat',[0,1])
@pytest.mark.parametrize('candidate_role',['P0','P1'])
def test_forfeit_preserves_official_score_and_attributes_failed_seat(tmp_path,failed_seat,candidate_role):
    events=tmp_path/'events.jsonl'
    events.write_text(json.dumps({'kind':'ai_error','player':failed_seat,'error_log':'runError'})+'\n')
    scores=(0.,1.) if failed_seat==0 else (1.,0.)
    winner=f'P{1-failed_seat}'
    result=MatchResult(MatchCase('candidate','opponent',candidate_role,7),'complete',
        'win' if winner==candidate_role else 'loss',float(winner==candidate_role),0.,0,
        payload={'terminal_scores':scores},events_path=events)
    evaluator=object.__new__(MiracleEvaluator)
    actual=evaluator._to_evaluate_result(result,candidate_role)
    assert actual.status is EvaluationStatus.GAME_ERROR
    assert actual.winner==winner and actual.scores==dict(zip(['P0','P1'],scores))
    assert actual.payload['failed_roles']==[f'P{failed_seat}']

def test_normal_end_remains_complete_even_after_cleanup_exit(tmp_path):
    events=tmp_path/'events.jsonl'
    events.write_text(json.dumps({'kind':'sandbox_cleanup','player':0})+'\n')
    result=MatchResult(MatchCase('c','o','P0',7),'complete','win',1.,1.,23,
        payload={'terminal_scores':(1.,0.)},events_path=events,process_returncodes=(0,-15,-15))
    actual=object.__new__(MiracleEvaluator)._to_evaluate_result(result,'P0')
    assert actual.status is EvaluationStatus.COMPLETE and not actual.payload['failed_roles']

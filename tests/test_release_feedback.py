import json
from pathlib import Path
import pytest
from aa_arena.replay.narration import _load_narrator, NarrationContext
ROOT=Path(__file__).parents[1]

@pytest.mark.parametrize('winner',['rollman','pacman','ghost'])
@pytest.mark.parametrize('role',['rollman','ghost',None])
def test_rollman_official_winner_alias_preserves_result_and_perspective(winner,role):
    module=_load_narrator('rollman',ROOT/'games')
    frames=[{'score':[10,0],'level':1,'round':1}]
    context=NarrationContext(game='rollman',match_id='synthetic',roles=('rollman','ghost'),perspective=role,official_winner=winner)
    text,actual,_=module._verdict(frames,context)
    assert actual==winner
    if role is None:assert ('吃豆人' if winner!='ghost' else '幽灵方') in text[0]
    else:assert ('你赢了' if ((role=='rollman')==(winner!='ghost')) else '你输了') in text[0]


def test_monecraft_state_feedback_preserves_zero_values_and_missing_fields(tmp_path):
    module=_load_narrator('monecraft',ROOT/'games')
    replay=tmp_path/'synthetic.json'
    replay.write_text(json.dumps({'rounds-info':[{'rounds':0,'ginfo':[{'id':0,'golds':0}],
        'players':[{'id':0,'x':24,'y':3,'blink-cd':0},None],
        'mines':[{'owner':-1}]*30}], 'result':{'winner':-2}}))
    result=module.narrate(replay,NarrationContext(game='monecraft',match_id='synthetic',official_rounds=0))
    assert result.winner is None and result.rounds==0
    for expected in ('金币=0','闪现冷却=0','x=24；y=3','省略 6 条','归属=-1','未记录'):assert expected in result.text
    assert 'None: None' not in result.text

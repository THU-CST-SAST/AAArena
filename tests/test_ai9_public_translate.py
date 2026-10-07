import json
from aa_arena.public_replay_translate import _load_ai9
from aa_arena.replay.public_json import load_ai9_document


def test_monecraft_translation_keeps_round_state(tmp_path):
    path = tmp_path / 'result.txt'
    path.write_text(json.dumps({'rounds-info': [{'rounds': 0, 'players': [1]},
        {'rounds': 1, 'players': [2]}], 'result': {'winner': 1}}))
    result = _load_ai9(path)
    assert result['rounds'] == 2
    assert result['winner'] == 1
    assert result['timeline'][-1]['state']['players'] == [2]


def test_normalized_pacman_keeps_final_round_beyond_512(tmp_path):
    path = tmp_path / 'result.txt'
    path.write_text(json.dumps({'breadcrumbs': [{'scores': [i]} for i in range(600)], 'result': -8}))
    document = load_ai9_document(path)
    assert len(document['rounds']) == 600
    assert document['rounds'][-1]['state_changes']['scores'] == [599]


def test_pacman_translation_keeps_terminal_frame(tmp_path):
    path = tmp_path / 'result.txt'
    path.write_text(json.dumps({'breadcrumbs': [{'scores': [i]} for i in range(600)], 'result': -8}))
    result = _load_ai9(path)
    assert result['rounds'] == 600
    assert result['timeline'][-1]['state']['scores'] == [599]
    assert result['winner'] == 1


def test_lota_translation_keeps_actions(tmp_path):
    path = tmp_path / 'replay.txt'
    path.write_text('Round:0\nAction:ChooseArcher\nRound:1\nAction:Attack\nwinner:0\n')
    result = _load_ai9(path)
    assert 'Action:Attack' in result['timeline'][-1]['state']

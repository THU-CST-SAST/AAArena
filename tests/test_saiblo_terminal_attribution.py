"""Official rule forfeits must survive removal of spurious terminal EOF errors."""
import json
import pytest
from aa_arena.saiblo.player_errors import transport_player_errors


@pytest.mark.parametrize('status', ['RE', 'TLE', 'OLE', 'IA'])
@pytest.mark.parametrize('encoded', [False, True])
def test_official_forfeit_attributes_only_the_failed_seat(tmp_path, status, encoded):
    states = ['OK', status]
    events = [
        {'kind': 'ai_exit_deferred', 'player': 0, 'detail': 'returncode=0'},
        {'kind': 'game_over', 'message': {'end_state': json.dumps(states) if encoded else states}},
        {'kind': 'ai_error', 'player': 0, 'error_log': 'runError'},
    ]
    path = tmp_path/'events.jsonl'
    path.write_text(''.join(json.dumps(e)+'\n' for e in events))
    failed, diagnostic = transport_player_errors(path, terminal_failure_states=frozenset({'RE','TLE','OLE','IA'}))
    assert failed == ['P1'] and diagnostic == f'P1: official_{status}'
    # Other game adapters must opt into their own terminal status vocabulary.
    assert transport_player_errors(path) == ([], None)


def test_transport_and_terminal_forfeits_keep_both_causes(tmp_path):
    events = [
        {'kind': 'ai_error', 'player': 0, 'error_log': 'timeOutError'},
        {'kind': 'game_over', 'message': {'end_state': '["TLE", "IA"]'}},
    ]
    path = tmp_path/'events.jsonl'
    path.write_text(''.join(json.dumps(e)+'\n' for e in events))
    roles, diagnostic = transport_player_errors(path, terminal_failure_states=frozenset({'TLE','IA'}))
    assert roles == ['P0', 'P1']
    assert 'timeOutError' in diagnostic and 'official_IA' in diagnostic

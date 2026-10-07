from pathlib import Path
from aa_arena.legacy.ai9 import Ai9GameConfig, _parse_result
from aa_arena.core.contract import EvaluationStatus
import os
import json
import signal
import subprocess
import sys
import time
import pytest
from aa_arena.legacy.ai9 import _kill_group


@pytest.mark.parametrize('signed,raw,winner', [(1, 0, 'P0'), (-1, 1, 'P1'), (0, -2, None)])
@pytest.mark.parametrize('with_replay', [True, False])
def test_lota_json_uses_signed_outcomes(tmp_path, signed, raw, winner, with_replay):
    config = Ai9GameConfig('lota', tmp_path, (), (), ('P0', 'P1'), 3)
    if with_replay:
        (tmp_path / 'replay.txt').write_text(f'Round:519\nwinner:{raw}\nAI STATUS:\n')
    (tmp_path / 'result.txt').write_text(json.dumps({'winner': signed, 'replay': ''}))
    result = _parse_result(config, tmp_path, 0)
    assert result.status is EvaluationStatus.COMPLETE
    assert result.winner == winner
    assert result.scores == ({'P0': .5, 'P1': .5} if winner is None else
                             {role: float(role == winner) for role in config.roles})


@pytest.mark.parametrize('signed,raw', [(-1, 0), (1, 1), (0, -1), (2, 0), (True, 0), ('1', 0)])
def test_lota_rejects_inconsistent_or_invalid_terminal(tmp_path, signed, raw):
    config = Ai9GameConfig('lota', tmp_path, (), (), ('P0', 'P1'), 3)
    (tmp_path / 'replay.txt').write_text(f'winner:{raw}\n')
    (tmp_path / 'result.txt').write_text(json.dumps({'winner': signed}))
    assert _parse_result(config, tmp_path, 0).status is EvaluationStatus.INFRA_ERROR


@pytest.mark.parametrize('winner', [-3, -1, 2, 99])
def test_invalid_official_winner_is_not_a_draw(tmp_path, winner):
    config = Ai9GameConfig('lota', tmp_path, (), (), ('P0', 'P1'), 3)
    (tmp_path / 'replay.txt').write_text(f'Round:10\nwinner:{winner}\n')
    result = _parse_result(config, tmp_path, 0)
    assert result.status is EvaluationStatus.INFRA_ERROR
    assert result.winner is None
    assert not result.scores


def test_backend_signal_cannot_be_hidden_by_an_early_winner(tmp_path):
    config = Ai9GameConfig('dorado', tmp_path, (), (), ('P0', 'P1'), 2)
    (tmp_path / 'replay.txt').write_text('Round:136\nwinner:0\n')
    result = _parse_result(config, tmp_path, -11)
    assert result.status is EvaluationStatus.INFRA_ERROR
    assert result.winner is None
    assert 'signal 11' in result.diagnostic


def test_cleanup_kills_descendant_after_parent_exits(tmp_path):
    ready = tmp_path / 'ready'
    child = subprocess.Popen([sys.executable, '-c',
        'import os,signal,time,sys; from pathlib import Path; '
        'pid=os.fork(); '
        'signal.signal(signal.SIGTERM,signal.SIG_IGN) if pid==0 else None; '
        'Path(sys.argv[1]).write_text(str(os.getpid())) if pid==0 else None; '
        'time.sleep(60)', str(ready)], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while (not ready.exists() or not ready.read_text()) and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        descendant = int(ready.read_text())
        _kill_group(child)
        deadline = time.monotonic() + 2
        path = Path(f'/proc/{descendant}/stat')
        while path.exists() and path.read_text().split()[2] != 'Z' and time.monotonic() < deadline:
            time.sleep(.01)
        assert not path.exists() or path.read_text().split()[2] == 'Z'
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)


def test_missing_winner_is_infrastructure_failure(tmp_path):
    config = Ai9GameConfig('lota', tmp_path, (), (), ('P0', 'P1'), 2)
    (tmp_path / 'backend.stderr').write_text('Connection reset by peer')
    result = _parse_result(config, tmp_path, 1)
    assert result.status is EvaluationStatus.INFRA_ERROR
    assert result.winner is None


def test_lota_player_failure_keeps_official_winner_but_reports_game_error(tmp_path):
    config = Ai9GameConfig('lota', tmp_path, (), (), ('P0', 'P1'), 3)
    (tmp_path/'replay.txt').write_text('Round:10\nwinner:1\nAI STATUS:AI(ID:0)Failed,status:TIMED_OUT msg:;\n')
    result = _parse_result(config, tmp_path, 0)
    assert result.status is EvaluationStatus.GAME_ERROR
    assert result.winner == 'P1'


def test_neutral_controller_failure_is_infrastructure(tmp_path):
    config = Ai9GameConfig('lota', tmp_path, (), (), ('P0', 'P1'), 3)
    (tmp_path/'replay.txt').write_text('Round:10\nwinner:1\nAI STATUS:AI(ID:2)Failed,status:SEGFAULT msg:;\n')
    assert _parse_result(config, tmp_path, 0).status is EvaluationStatus.INFRA_ERROR

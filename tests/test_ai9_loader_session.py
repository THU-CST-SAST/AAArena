from pathlib import Path
import os
import signal
import socket
import subprocess

import pytest

from aa_arena.benchmark.matches import _configured_evaluator
from aa_arena.legacy.ai9 import build_backend
from aa_arena.benchmark.matches import load_opponents
from aa_arena.core.contract import PlayerRef, EvaluationStatus


def test_lota_plays_a_competitive_match(tmp_path):
    root = Path(__file__).resolve().parents[1]
    evaluator = _configured_evaluator('lota', tmp_path / 'matches', tmp_path / 'build', root)
    opponent = load_opponents('lota', root)[39]
    result = evaluator.evaluate([
        PlayerRef('starter', str(root / 'games/lota/public_sdk')),
        PlayerRef(opponent.opponent_id, str(opponent.package_root)),
    ], ['P0', 'P1'], 20260909)
    assert result.status is EvaluationStatus.COMPLETE, result.diagnostic
    assert result.rounds is not None and result.rounds > 0


@pytest.mark.parametrize('game', ['lota', 'monecraft', 'pacman', 'dorado'])
def test_loader_accepts_parent_created_session(tmp_path, game):
    root = Path(__file__).resolve().parents[1]
    evaluator = _configured_evaluator(game, tmp_path / 'matches', tmp_path / 'build', root)
    if game == 'dorado':
        namespace = evaluator.evaluate.__func__.__globals__
        layout = namespace['DoradoLayout'].from_game_dir(root / 'games/dorado', tmp_path / 'build')
        backend = namespace['build_backend'](layout)
    else:
        backend = build_backend(evaluator._config, tmp_path / 'build')
    with (tmp_path / 'stderr').open('w+') as errors:
        process = subprocess.Popen([str(backend.ailoader), '/nonexistent-player.so'],
            stdout=subprocess.PIPE, stderr=errors, text=True, start_new_session=True)
        try:
            port = int(process.stdout.readline())
            with socket.create_connection(('127.0.0.1', port), timeout=5) as connection:
                connection.settimeout(5)
                try:
                    connection.recv(1024)
                except (ConnectionResetError, socket.timeout):
                    pass
            errors.seek(0)
            assert 'failed to setsid' not in errors.read()
            assert process.poll() is None, 'loader must await the game protocol'
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)

from pathlib import Path
import subprocess

import pytest

from aa_arena.legacy.ai9 import (
    Ai9Error, Ai9GameConfig, _parse_result, _runtime_environment, _validate_player_export,
)
from aa_arena.core.contract import EvaluationStatus


@pytest.mark.parametrize("game,source,accepted", [
    ("monecraft", "void player_ai(int) {}", False),
    ("dorado", "int main() { return 0; }", False),
    ("lota", 'extern "C" void player_ai() {}', True),
    ("pacman", 'extern "C" int decide() { return 0; }', True),
])
def test_export_gate_rejects_wrong_abi_and_unrelated_programs(tmp_path, game, source, accepted):
    path = tmp_path / 'player.cpp'; path.write_text(source)
    library = tmp_path / 'player.so'
    subprocess.run(['g++', '-shared', '-fPIC', str(path), '-o', str(library)], check=True, capture_output=True)
    if accepted:
        _validate_player_export(game, library)
    else:
        with pytest.raises(Ai9Error, match='required callback'):
            _validate_player_export(game, library)


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_loader_failure_never_becomes_a_win_or_draw(tmp_path, stream):
    (tmp_path / 'replay.txt').write_text('Round:0\nwinner:1\n')
    (tmp_path / f'ailoader-0.{stream}').write_text('failed to lookup AI func: undefined symbol: player_ai')
    config = Ai9GameConfig('lota', tmp_path, (), (), ('P0', 'P1'), 2)
    result = _parse_result(config, tmp_path, 0)
    assert result.status is EvaluationStatus.INFRA_ERROR
    assert result.winner is None and not result.scores


def test_match_and_compiler_environment_excludes_host_credentials(monkeypatch):
    monkeypatch.setenv('ABHL_KEY_TEST', 'test-secret')
    monkeypatch.setenv('OPENAI_API_KEY', 'other-test-secret')
    monkeypatch.setenv('AI9_AILOADER_NOSUPERVISOR', '1')
    environment = _runtime_environment()
    assert not {'ABHL_KEY_TEST', 'OPENAI_API_KEY', 'AI9_AILOADER_NOSUPERVISOR'} & environment.keys()
    assert 'PATH' in environment


def test_exported_callback_with_missing_function_is_rejected(tmp_path):
    source=tmp_path/'broken.cpp'
    source.write_text('extern void missing_function(); extern "C" void player_ai() { missing_function(); }')
    library=tmp_path/'broken.so'
    subprocess.run(['g++','-shared','-fPIC',str(source),'-o',str(library)],check=True,capture_output=True)
    with pytest.raises(Ai9Error, match='unresolved dependencies'):
        _validate_player_export('dorado',library)


@pytest.mark.parametrize('game,symbol,accepted', [
    ('pacman','log_printf',True), ('pacman','unknown_logger',False),
    ('dorado','log_printf',False),
])
def test_only_documented_pacman_host_symbol_is_allowed(tmp_path,game,symbol,accepted):
    callback='decide' if game=='pacman' else 'player_ai'
    source=tmp_path/'plugin.cpp'
    source.write_text(f'extern "C" void {symbol}(const char*, ...); extern "C" void {callback}() {{ {symbol}("hello"); }}')
    library=tmp_path/'plugin.so'
    subprocess.run(['g++','-shared','-fPIC',str(source),'-o',str(library)],check=True,capture_output=True)
    if accepted:_validate_player_export(game,library)
    else:
        with pytest.raises(Ai9Error,match='unresolved dependencies'):_validate_player_export(game,library)

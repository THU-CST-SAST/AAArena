from pathlib import Path
import sys
import pytest
from games.dorado.evaluator.runtime import (
    DoradoLayout, DoradoRuntimeError, build_player, _run_build,
)


def test_dorado_compiler_child_does_not_inherit_host_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('ABHL_KEY_TEST', 'test-placeholder')
    monkeypatch.setenv('AI9_AILOADER_NOSUPERVISOR', '1')
    _run_build([sys.executable, '-c',
        "import os; assert 'ABHL_KEY_TEST' not in os.environ; "
        "assert 'AI9_AILOADER_NOSUPERVISOR' not in os.environ"], cwd=tmp_path)


def test_dorado_export_gate_checks_cold_and_cached_player(tmp_path):
    package=tmp_path/'player';package.mkdir()
    (package/'ai.cpp').write_text('int main() { return 0; }')
    backend=tmp_path/'backend';backend.mkdir()
    layout=DoradoLayout(tmp_path,tmp_path/'build',backend,tmp_path/'manifest.tsv',
        tmp_path,backend,())
    for attempt in range(2):
        with pytest.raises(DoradoRuntimeError, match='required callback player_ai'):
            build_player(layout, 'player', package)
    assert len(list((tmp_path/'build').rglob('player.so'))) == 1

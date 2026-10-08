import os
import sys
from pathlib import Path

import pytest

from aa_arena.saiblo.judger import backend_process_spec
from aa_arena.sandbox import ProcessSpec, SandboxInfrastructureError


def test_selected_backend_python_preserves_arguments_directory_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('AA_ARENA_BACKEND_PYTHON', sys.executable)
    monkeypatch.delenv('AA_ARENA_REFERENCE_RUNTIME', raising=False)
    spec = ProcessSpec(('/service/python3.12', '-u', 'main.py'), tmp_path, {'GAME_SEED': '17'})
    actual = backend_process_spec(spec)
    assert actual.argv == (sys.executable, '-u', 'main.py')
    assert actual.cwd == spec.cwd and actual.env == spec.env


def test_unconfigured_or_native_backend_is_preserved(tmp_path, monkeypatch):
    monkeypatch.delenv('AA_ARENA_BACKEND_PYTHON', raising=False)
    spec = ProcessSpec((sys.executable, 'main.py'), tmp_path)
    assert backend_process_spec(spec) is spec
    monkeypatch.setenv('AA_ARENA_BACKEND_PYTHON', '/missing/python')
    native = ProcessSpec(('/judge/logic',), tmp_path)
    assert backend_process_spec(native) is native
    with pytest.raises(SandboxInfrastructureError):
        backend_process_spec(spec)


def test_python_backend_uses_reference_runtime_loader(tmp_path, monkeypatch):
    monkeypatch.setenv('AA_ARENA_BACKEND_PYTHON', sys.executable)
    monkeypatch.setattr('aa_arena.core.reference_runtime.backend_command',
        lambda argv: ['/reference/ld.so', '--library-path', '/reference/lib', *argv])
    actual = backend_process_spec(ProcessSpec((sys.executable, 'main.py'), tmp_path))
    assert actual.argv == ('/reference/ld.so', '--library-path', '/reference/lib', sys.executable, 'main.py')

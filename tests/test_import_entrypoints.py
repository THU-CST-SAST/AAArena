"""Public entry points must work independently in a fresh interpreter."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", [
    "aa_arena.sandbox",
    "aa_arena.sandbox.systemd",
    "aa_arena.core.reference_runtime",
    "aa_arena.saiblo.player_errors",
])
def test_fresh_import(module):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}; from aa_arena.core import evaluate; assert callable(evaluate)"],
        cwd=root, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

"""Test-process safeguards for immutable bundled game assets."""

from __future__ import annotations

import os
import sys


# Some backend smoke tests import modules directly from games/*/backend. Keep
# those versioned asset trees byte-for-byte clean throughout the full suite.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import json
import pytest

@pytest.fixture(autouse=True)
def isolated_test_profiles(tmp_path, monkeypatch):
    """Synthetic profiles: tests never read a developer's endpoints or credentials."""
    directory=tmp_path/'model-fixtures'
    directory.mkdir()
    for name in ('default','test-model-a','test-model-b','test-model-c','test-model-d'):
        profile=dict(name=name,model=name,model_provider='fixture-provider',
                     base_url='https://provider.invalid/v1',api_key_env='ARENA_TEST_KEY',
                     reasoning_effort='max',wire_api='responses',context_window=800000,
                     effective_context_window_percent=95)
        (directory/(name+'.json')).write_text(json.dumps(profile))
    monkeypatch.setenv('AA_ARENA_PROFILE_DIR',str(directory))

@pytest.fixture(autouse=True)
def isolate_fake_systemd_environment(request,monkeypatch):
    if request.node.get_closest_marker('sandbox_integration'):
        return
    if request.path.name in {'test_rootless_policy.py','test_systemd_scope_launcher.py'}:
        monkeypatch.delenv('AA_ARENA_SYSTEMD_MODE',raising=False)
        monkeypatch.delenv('AA_ARENA_CPU_POLICY',raising=False)

@pytest.fixture(autouse=True)
def isolated_evaluation_transport(request, monkeypatch):
    # Legacy controller/unit tests exercise the local service with synthetic match
    # results. Remote tests explicitly exercise an authenticated loopback server.
    if request.path.name not in {"test_remote_evaluation.py", "test_remote_benchmark.py", "test_local_subset.py", "test_release_tools.py"}:
        monkeypatch.setattr("aa_arena.benchmark.remote.public_distribution", lambda *a: False)

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from aa_arena.benchmark.runtime import CodexArenaRuntime


@pytest.mark.skipif(os.geteuid() != 0, reason='production runtime requires root')
def test_prepare_restores_mapped_owner_and_leaves_link_target_private(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    resources = workspace / 'resources'
    resources.mkdir()
    rules = resources / 'rules.md'
    rules.write_text('immutable')
    rules.chmod(0o444)
    home = tmp_path / 'controller/codex-home'
    arg = home / 'tmp/arg0'
    arg.mkdir(parents=True)
    lock = arg / 'lock'
    lock.write_text('preserve')
    for p in (home, arg, lock):
        os.chown(p, 1000, 1000)
    private = tmp_path / 'private'
    private.write_text('not mounted')
    private.chmod(0o600)
    (home / 'external-link').symlink_to(private)
    runtime = object.__new__(CodexArenaRuntime)
    runtime.service = SimpleNamespace(workspace=workspace, controller=home.parent,
                                      initialize_workspace=lambda: None)
    runtime.codex_home = home
    runtime.netns_proxy_path = tmp_path / 'proxy.py'
    runtime._prepare_filesystem()
    assert all(p.stat().st_uid == 0 for p in (home, arg, lock))
    assert lock.read_text() == 'preserve'
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(rules.stat().st_mode) == 0o444

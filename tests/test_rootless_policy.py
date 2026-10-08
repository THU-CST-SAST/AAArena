import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aa_arena.sandbox import ProcessSpec, SandboxInfrastructureError
from aa_arena.sandbox.systemd import SystemdScopeLauncher
from aa_arena.benchmark.runtime import CodexArenaRuntime
from test_systemd_scope_launcher import FakeSystemdHost


def policy_launcher(host, **kwargs):
    return SystemdScopeLauncher(
        cgroup_root=host.cgroup_root, temporary_root=host.cwd / 'gates',
        popen_factory=host.popen, control_runner=host.run, which=host.which,
        monotonic=host.monotonic, sleep=host.sleep,
        registration_gate=host.registration_gate, **kwargs)


def test_unlimited_policy_records_true_configuration(tmp_path):
    host = FakeSystemdHost(tmp_path, cpu_max='max 100000\n')
    proc = policy_launcher(host, cpu_policy='unlimited').start(
        ProcessSpec(('/bin/true',), host.cwd), match_id='unlimited', player_index=0)
    assert proc.metadata.requested_cpu_quota_percent is None
    assert proc.metadata.requested_period_usec is None
    assert proc.metadata.observed_cpu_max == 'max 100000'
    assert proc.metadata.cpu_policy == 'unlimited'
    assert not any('CPUQuota' in arg for arg in host.popen_calls[0][0])
    assert proc.cleanup().clean


def test_unlimited_policy_rejects_effective_per_player_quota(tmp_path):
    host = FakeSystemdHost(tmp_path)
    with pytest.raises(SandboxInfrastructureError, match='limited'):
        policy_launcher(host, cpu_policy='unlimited').start(
            ProcessSpec(('/bin/true',), host.cwd), match_id='limited', player_index=0)


def test_unlimited_missing_cpu_controller_preserves_ancestor_evidence(tmp_path):
    host = FakeSystemdHost(tmp_path)
    scope = host.cgroup_root / host.control_group.lstrip('/')
    (scope / 'cpu.max').unlink()
    (scope.parent / 'cgroup.subtree_control').write_text('memory pids\n')
    (scope.parent / 'cpu.max').write_text('max 100000\n')
    proc = policy_launcher(host, cpu_policy='unlimited').start(
        ProcessSpec(('/bin/true',), host.cwd), match_id='missing', player_index=0)
    assert proc.metadata.observed_cpu_max is None
    assert proc.metadata.ancestor_cpu_max == {'system.slice': 'max 100000'}
    assert proc.cleanup().clean


def test_missing_cpu_max_fails_when_controller_is_enabled(tmp_path):
    host = FakeSystemdHost(tmp_path)
    scope = host.cgroup_root / host.control_group.lstrip('/')
    (scope / 'cpu.max').unlink()
    (scope.parent / 'cgroup.subtree_control').write_text('cpu memory pids\n')
    with pytest.raises(SandboxInfrastructureError, match='missing despite'):
        policy_launcher(host, cpu_policy='unlimited').start(
            ProcessSpec(('/bin/true',), host.cwd), match_id='missing', player_index=0)


def test_user_mode_routes_control_calls_and_preserves_only_bus_environment(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '', '')
    launcher = SystemdScopeLauncher(user_mode=True, control_runner=run)
    launcher._run_control(('/usr/bin/systemctl', 'show', 'example.scope'))
    assert calls == [('/usr/bin/systemctl', '--user', 'show', 'example.scope')]
    monkeypatch.setenv('XDG_RUNTIME_DIR', '/run/user/20018')
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/run/user/20018/bus')
    monkeypatch.setenv('PROVIDER_API_KEY', 'not-for-player')
    env = launcher._launch_environment({})
    assert env['XDG_RUNTIME_DIR'] == '/run/user/20018'
    assert 'PROVIDER_API_KEY' not in env


def test_runtime_keeps_current_owner_and_does_not_follow_private_symlinks(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    controller = tmp_path / 'controller'
    controller.mkdir()
    home = controller / 'codex-home'
    home.mkdir()
    private = tmp_path / 'private'
    private.write_text('private')
    private.chmod(0o600)
    (workspace / 'external').symlink_to(private)
    runtime = object.__new__(CodexArenaRuntime)
    runtime.service = SimpleNamespace(workspace=workspace, controller=controller,
                                      initialize_workspace=lambda: None)
    runtime.codex_home = home
    runtime.netns_proxy_path = tmp_path / 'proxy.py'
    runtime._prepare_filesystem()
    assert home.stat().st_uid == os.geteuid()
    runtime._freeze_workspace()
    assert private.stat().st_mode & 0o777 == 0o600
    assert controller.stat().st_mode & 0o777 == 0o700
    workspace.chmod(0o700)


@pytest.mark.sandbox_integration
def test_real_rootless_scope_blocks_private_files_and_network_then_cleans(tmp_path):
    if os.environ.get('AA_ARENA_RUN_ROOTLESS_INTEGRATION') != '1':
        pytest.skip('requires target Linux user manager')
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    secret = tmp_path / 'private'
    secret.write_text('must stay hidden')
    (candidate / 'probe.py').write_text(
        'import json,socket,time,subprocess,sys,signal\n'
        'from pathlib import Path\n'
        f'visible=Path({str(secret)!r}).exists()\n'
        'net=True\n'
        'try:\n socket.socket().connect(("1.1.1.1",53))\n'
        'except OSError:\n net=False\n'
        'subprocess.Popen([sys.executable,"-c","import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(120)"])\n'
        'print(json.dumps({"visible":visible,"network":net}),flush=True)\n'
        'time.sleep(120)\n')
    launcher = SystemdScopeLauncher(user_mode=True, cpu_policy='unlimited', startup_timeout_s=10)
    proc = launcher.start(ProcessSpec(('/usr/bin/python3','probe.py'), candidate),
                          match_id='rootless-integration', player_index=0)
    try:
        assert json.loads(proc.stdout.readline()) == {'visible':False,'network':False}
        assert proc.metadata.cpu_policy == 'unlimited'
    finally:
        outcome = proc.cleanup()
    assert outcome.clean, outcome.detail
    result = subprocess.run(['systemctl','--user','is-active',proc.metadata.unit_name],capture_output=True,text=True)
    assert result.stdout.strip() != 'active'


def test_numeric_player_imports_use_bounded_thread_pools(monkeypatch):
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "64")
    monkeypatch.setenv("PRIVATE_API_TOKEN", "not-for-player")
    launcher = SystemdScopeLauncher(user_mode=False)
    env = launcher._launch_environment({"OMP_NUM_THREADS": "128"})
    for key in (
        "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS", "TF_NUM_INTRAOP_THREADS", "TF_NUM_INTEROP_THREADS",
    ):
        assert env[key] == "1"
    assert "PRIVATE_API_TOKEN" not in env

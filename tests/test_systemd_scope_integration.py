from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from aa_arena.sandbox import DirectLauncher, ProcessSpec, SystemdScopeLauncher
from aa_arena.sandbox.systemd import read_cpu_usage_usec, verify_cpu_max


@pytest.fixture(scope="module", autouse=True)
def require_systemd_cgroup_v2() -> None:
    try:
        SystemdScopeLauncher().preflight()
    except Exception as exc:  # pragma: no cover - developer-host capability gate
        pytest.skip(f"systemd cgroup v2 unavailable: {exc}")


def _write_script(tmp_path: Path, name: str, source: str) -> ProcessSpec:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return ProcessSpec((sys.executable, str(path)), tmp_path)


def _process_cgroup(pid: int) -> str:
    entries = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines()
    unified = [line.split(":", 2)[2] for line in entries if line.startswith("0::")]
    assert len(unified) == 1
    return unified[0]


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.sandbox_integration
def test_real_scope_enforces_one_aggregate_cpu(tmp_path: Path) -> None:
    if os.environ.get("AA_ARENA_CPU_POLICY", "one_cpu") != "one_cpu":
        pytest.skip("requires the one_cpu policy and a delegated cpu controller")
    spec = _write_script(
        tmp_path,
        "burn.py",
        """
import multiprocessing, time

def burn():
    value = 1
    while True:
        value = (value * 48271) % 2147483647

if __name__ == "__main__":
    workers = [multiprocessing.Process(target=burn) for _ in range(2)]
    for worker in workers:
        worker.start()
    print("started", flush=True)
    time.sleep(60)
""",
    )
    process = SystemdScopeLauncher().start(spec, match_id="cpu-real", player_index=0)
    try:
        started = process.stdout.readline()
        if started != b"started\n":
            pytest.fail(
                f"burner exited before readiness: returncode={process.poll()} "
                f"stdout={started!r} stderr={process.stderr.read()!r}"
            )
        assert process.metadata.observed_cpu_max is not None
        assert verify_cpu_max(process.metadata.observed_cpu_max) == (100_000, 100_000)
        assert process.metadata.control_group is not None
        before = read_cpu_usage_usec(process.metadata.control_group)
        time.sleep(2.0)
        after = read_cpu_usage_usec(process.metadata.control_group)
        assert 300_000 <= after - before <= 2_800_000
    finally:
        outcome = process.cleanup()
    assert outcome.clean is True, outcome.detail


@pytest.mark.sandbox_integration
def test_players_get_distinct_scopes_and_backend_is_outside(tmp_path: Path) -> None:
    spec = _write_script(tmp_path, "wait.py", "import time; time.sleep(60)\n")
    launcher = SystemdScopeLauncher()
    player0 = launcher.start(spec, match_id="pair-real", player_index=0)
    player1 = launcher.start(spec, match_id="pair-real", player_index=1)
    backend = DirectLauncher().start(spec, match_id="pair-real", player_index=-1)
    try:
        assert player0.metadata.control_group != player1.metadata.control_group
        assert player0.metadata.control_group == _process_cgroup(player0.pid)
        assert player1.metadata.control_group == _process_cgroup(player1.pid)
        assert _process_cgroup(backend.pid) not in {
            player0.metadata.control_group,
            player1.metadata.control_group,
        }
    finally:
        outcomes = (backend.cleanup(), player0.cleanup(), player1.cleanup())
    assert all(outcome.clean for outcome in outcomes), outcomes


@pytest.mark.sandbox_integration
def test_cleanup_kills_descendant_that_ignores_sigterm(tmp_path: Path) -> None:
    spec = _write_script(
        tmp_path,
        "stubborn.py",
        """
import signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
])
print(child.pid, flush=True)
time.sleep(60)
""",
    )
    process = SystemdScopeLauncher().start(spec, match_id="stubborn-real", player_index=0)
    child_namespace_pid = int(process.stdout.readline())
    parent_pid = process.pid
    unit_name = process.metadata.unit_name
    assert unit_name is not None

    outcome = process.cleanup()

    assert outcome.clean is True, outcome.detail
    assert not _pid_exists(parent_pid)
    # The player is in a private PID namespace, so the reported child PID is
    # namespace-local and must not be looked up in the host /proc tree.
    assert child_namespace_pid > 0
    status = subprocess.run(
        ("systemctl", "is-active", unit_name),
        capture_output=True,
        text=True,
        check=False,
    )
    assert status.stdout.strip() != "active"

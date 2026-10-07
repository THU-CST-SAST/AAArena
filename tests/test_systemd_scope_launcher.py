from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aa_arena.sandbox import ProcessSpec, SandboxInfrastructureError
from aa_arena.sandbox.systemd import (
    SystemdScopeLauncher,
    _pace_scope_registration,
    parse_cpu_max,
    verify_cpu_max,
)


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.pid = 4242
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(("systemd-run",), timeout)
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class FakeSystemdHost:
    def __init__(self, tmp_path: Path, *, cpu_max: str = "100000 100000\n") -> None:
        self.cwd = tmp_path / "cwd"
        self.cwd.mkdir()
        self.cgroup_root = tmp_path / "cgroup"
        self.cgroup_root.mkdir()
        (self.cgroup_root / "cgroup.controllers").write_text("cpu memory pids\n")
        (self.cgroup_root / "cgroup.type").write_text("domain\n")
        self.control_group = "/system.slice/fake.scope"
        scope = self.cgroup_root / self.control_group.lstrip("/")
        scope.mkdir(parents=True)
        (scope / "cpu.max").write_text(cpu_max)
        self.popen_calls: list[tuple[list[str], dict[str, Any]]] = []
        self.control_calls: list[tuple[str, ...]] = []
        self.processes: list[FakeProcess] = []
        self.registration_gate_calls = 0
        self.clock = 0.0
        self.show_value = self.control_group
        self.active_states = ["inactive"]

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}"

    def monotonic(self) -> float:
        self.clock += 0.01
        return self.clock

    def sleep(self, duration: float) -> None:
        self.clock += duration

    def popen(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        assert self.registration_gate_calls == len(self.popen_calls) + 1
        self.popen_calls.append((list(argv), kwargs))
        process = FakeProcess()
        self.processes.append(process)
        return process

    def run(self, argv: tuple[str, ...], **_: Any) -> subprocess.CompletedProcess[str]:
        self.control_calls.append(tuple(argv))
        action = argv[1]
        if action == "show":
            return subprocess.CompletedProcess(argv, 0, f"{self.show_value}\n", "")
        if action == "kill":
            for process in self.processes:
                if process.poll() is None:
                    process.returncode = -15 if "--signal=TERM" in argv else -9
            return subprocess.CompletedProcess(argv, 0, "", "")
        if action == "stop":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if action == "is-active":
            state = self.active_states.pop(0) if len(self.active_states) > 1 else self.active_states[0]
            return subprocess.CompletedProcess(
                argv,
                0 if state == "active" else 3,
                f"{state}\n",
                "",
            )
        raise AssertionError(f"unexpected control call: {argv}")

    @property
    def launcher(self) -> SystemdScopeLauncher:
        return SystemdScopeLauncher(
            cgroup_root=self.cgroup_root,
            temporary_root=self.cwd / "gates",
            popen_factory=self.popen,
            control_runner=self.run,
            which=self.which,
            monotonic=self.monotonic,
            sleep=self.sleep,
            registration_gate=self.registration_gate,
        )

    def registration_gate(self) -> None:
        self.registration_gate_calls += 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("100000 100000\n", (100000, 100000)), ("50000 100000", (50000, 100000))],
)
def test_parse_cpu_max_accepts_finite_quota(
    raw: str,
    expected: tuple[int, int],
) -> None:
    assert parse_cpu_max(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["max 100000", "100001 100000", "0 100000", "100000 0", "broken"],
)
def test_verify_cpu_max_rejects_unfair_or_malformed_values(raw: str) -> None:
    with pytest.raises(SandboxInfrastructureError):
        verify_cpu_max(raw)


def test_scope_command_has_exact_fairness_properties(tmp_path: Path) -> None:
    host = FakeSystemdHost(tmp_path)
    launcher = host.launcher
    launcher.preflight()

    process = launcher.start(
        ProcessSpec(("/opt/player", "--seed", "9"), host.cwd, {"LANG": "C"}),
        match_id="round 7/blue",
        player_index=1,
    )

    argv, kwargs = host.popen_calls[0]
    assert "--scope" in argv
    assert "--quiet" in argv
    assert "--property=CPUQuota=100%" in argv
    assert "--property=CPUQuotaPeriodSec=100ms" in argv
    assert "--property=KillMode=control-group" in argv
    separator = argv.index("--")
    wrapped = argv[separator + 1 :]
    assert wrapped[0] == sys.executable
    assert Path(wrapped[1]).name == "scope_entry.py"
    assert "/usr/bin/bwrap" in wrapped
    assert "--unshare-all" in wrapped
    assert "--ro-bind" in wrapped
    assert str(host.cwd) not in wrapped
    plan_index = wrapped.index("--propagation") + 4
    plan = json.loads(Path(wrapped[plan_index]).read_text(encoding="utf-8"))
    assert any(row["source"] == str(host.cwd) for row in plan)
    assert wrapped[-3:] == ["/program/player", "--seed", "9"]
    assert kwargs["cwd"] == host.cwd
    assert process.metadata.launcher == "systemd_scope"
    assert process.metadata.observed_cpu_max == "100000 100000"
    assert process.metadata.control_group == host.control_group
    assert process.metadata.unit_name is not None
    assert process.metadata.unit_name.endswith(".scope")
    assert host.registration_gate_calls == 1

    gate = Path(wrapped[2])
    assert gate.is_file()
    outcome = process.cleanup()
    assert outcome.clean is True
    assert not gate.parent.exists()


def test_filesystem_sandbox_supports_venv_with_external_interpreter_symlink(
    tmp_path: Path,
) -> None:
    host = FakeSystemdHost(tmp_path)
    runtime = tmp_path / "venv"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "pyvenv.cfg").write_text("home = external\n", encoding="utf-8")
    executable = runtime / "bin" / "python"
    executable.symlink_to(Path(sys.executable).resolve())
    script = host.cwd / "player.py"
    script.write_text("print('ok')\n", encoding="utf-8")

    sandboxed = host.launcher.isolate_filesystem(
        ProcessSpec((str(executable), str(script)), host.cwd)
    )

    argv = list(sandboxed.argv)
    assert "/runtime/bin/python" in argv
    external_runtime = Path(sys.executable).resolve().parent.parent
    assert str(external_runtime) in argv
    assert "/workspace/player.py" in argv


def test_scope_registration_gate_spaces_processes_across_shared_lock(
    tmp_path: Path,
) -> None:
    clock = [10.0]
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock[0]

    def sleep(duration: float) -> None:
        sleeps.append(duration)
        clock[0] += duration

    lock_path = tmp_path / "scope-registration.lock"

    _pace_scope_registration(
        lock_path=lock_path,
        interval_s=0.08,
        monotonic=monotonic,
        sleep=sleep,
    )
    _pace_scope_registration(
        lock_path=lock_path,
        interval_s=0.08,
        monotonic=monotonic,
        sleep=sleep,
    )

    assert sleeps == [pytest.approx(0.08)]


def test_scope_names_are_collision_resistant_and_systemd_safe(tmp_path: Path) -> None:
    host = FakeSystemdHost(tmp_path)
    first = host.launcher.start(
        ProcessSpec(("/bin/true",), host.cwd),
        match_id="spaces/and:punctuation",
        player_index=0,
    )
    second = host.launcher.start(
        ProcessSpec(("/bin/true",), host.cwd),
        match_id="spaces/and:punctuation",
        player_index=0,
    )
    try:
        assert first.metadata.unit_name != second.metadata.unit_name
        assert first.metadata.unit_name is not None
        assert set(first.metadata.unit_name) <= set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
        )
    finally:
        first.cleanup()
        second.cleanup()


def test_preflight_rejects_missing_cpu_controller(tmp_path: Path) -> None:
    host = FakeSystemdHost(tmp_path)
    (host.cgroup_root / "cgroup.controllers").write_text("memory pids\n")

    with pytest.raises(SandboxInfrastructureError, match="cpu controller"):
        host.launcher.preflight()


def test_preflight_accepts_cgroup_v2_root_without_cgroup_type(tmp_path: Path) -> None:
    host = FakeSystemdHost(tmp_path)
    (host.cgroup_root / "cgroup.type").unlink()

    host.launcher.preflight()


def test_start_rejects_control_group_outside_cgroup_root(tmp_path: Path) -> None:
    host = FakeSystemdHost(tmp_path)
    host.show_value = "/../../etc"

    with pytest.raises(SandboxInfrastructureError, match="escapes cgroup root"):
        host.launcher.start(
            ProcessSpec(("/bin/true",), host.cwd),
            match_id="escape",
            player_index=0,
        )

    assert host.processes[0].poll() is not None


def test_start_rejects_unverified_quota_and_cleans_wrapper(tmp_path: Path) -> None:
    host = FakeSystemdHost(tmp_path, cpu_max="max 100000\n")

    with pytest.raises(SandboxInfrastructureError, match="cpu.max"):
        host.launcher.start(
            ProcessSpec(("/bin/true",), host.cwd),
            match_id="unbounded",
            player_index=0,
        )

    assert host.processes[0].poll() is not None


def test_cleanup_waits_for_scope_to_become_inactive(tmp_path: Path) -> None:
    host = FakeSystemdHost(tmp_path)
    host.active_states = ["active", "active", "inactive"]
    process = host.launcher.start(
        ProcessSpec(("/bin/true",), host.cwd),
        match_id="teardown-race",
        player_index=0,
    )

    outcome = process.cleanup()

    assert outcome.clean is True
    assert sum(call[1] == "is-active" for call in host.control_calls) == 3

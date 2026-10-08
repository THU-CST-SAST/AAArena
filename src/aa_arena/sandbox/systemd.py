"""Strict cgroup v2 player launcher backed by systemd transient scopes."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from aa_arena.core.reference_runtime import reference_root, system_path
from collections.abc import Callable
from pathlib import Path

from .model import (
    CleanupOutcome,
    LaunchMetadata,
    ManagedProcess,
    ProcessSpec,
    SandboxInfrastructureError,
    safe_environment,
)

CPU_QUOTA_PERCENT = 100
CPU_PERIOD_USEC = 100_000
SCOPE_REGISTRATION_INTERVAL_S = 0.08


def _pace_scope_registration(
    *,
    lock_path: Path | None = None,
    interval_s: float = SCOPE_REGISTRATION_INTERVAL_S,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Space systemd-run starts across evaluator worker processes."""

    path = lock_path or (
        Path(tempfile.gettempdir())
        / f"aa-arena-{os.getuid()}-scope-registration.lock"
    )
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        raw_previous = handle.read().strip()
        try:
            previous = float(raw_previous) if raw_previous else None
        except ValueError:
            previous = None
        now = monotonic()
        if previous is not None:
            delay = interval_s - (now - previous)
            if delay > 0:
                sleep(delay)
                now = monotonic()
        handle.seek(0)
        handle.truncate()
        handle.write(f"{now:.9f}\n")
        handle.flush()


def parse_cpu_max(value: str) -> tuple[int, int]:
    fields = value.split()
    if len(fields) != 2 or fields[0] == "max":
        raise SandboxInfrastructureError(f"unbounded or malformed cpu.max: {value!r}")
    try:
        quota, period = (int(field) for field in fields)
    except ValueError as exc:
        raise SandboxInfrastructureError(f"malformed cpu.max: {value!r}") from exc
    if quota <= 0 or period <= 0:
        raise SandboxInfrastructureError(f"non-positive cpu.max: {value!r}")
    return quota, period


def verify_cpu_max(value: str) -> tuple[int, int]:
    quota, period = parse_cpu_max(value)
    if quota > period:
        raise SandboxInfrastructureError(
            f"player quota exceeds 1.0 CPU: cpu.max={value.strip()}"
        )
    return quota, period


def _resolve_cgroup_file(
    cgroup_root: Path,
    control_group: str,
    filename: str,
) -> Path:
    root = Path(cgroup_root).resolve()
    candidate = (root / control_group.lstrip("/") / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SandboxInfrastructureError(
            f"systemd ControlGroup escapes cgroup root: {control_group!r}"
        ) from exc
    return candidate


def read_cpu_usage_usec(
    control_group: str,
    *,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> int:
    try:
        statistics = _resolve_cgroup_file(
            cgroup_root,
            control_group,
            "cpu.stat",
        ).read_text(encoding="utf-8")
        values = dict(
            line.split(maxsplit=1) for line in statistics.splitlines() if line.strip()
        )
        return int(values["usage_usec"])
    except (KeyError, OSError, ValueError) as exc:
        raise SandboxInfrastructureError(
            f"invalid cpu.stat for {control_group!r}"
        ) from exc


class SystemdScopeLauncher:
    """Launch each player tree in one separately verified systemd scope."""

    def __init__(
        self,
        *,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        user_mode: bool | None = None,
        cpu_policy: str | None = None,
        temporary_root: Path | None = None,
        startup_timeout_s: float = 2.0,
        cleanup_timeout_s: float = 10.0,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        control_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        registration_gate: Callable[[], None] | None = None,
    ) -> None:
        self._user_mode = (os.environ.get("AA_ARENA_SYSTEMD_MODE", "system") == "user"
                           if user_mode is None else user_mode)
        self._cpu_policy = cpu_policy or os.environ.get("AA_ARENA_CPU_POLICY", "one_cpu")
        if self._cpu_policy not in {"one_cpu", "unlimited"}:
            raise ValueError(f"unknown CPU policy: {self._cpu_policy}")
        self._cgroup_root = Path(cgroup_root).resolve()
        self._temporary_root = (
            Path(temporary_root).resolve() if temporary_root is not None else None
        )
        self._startup_timeout_s = float(startup_timeout_s)
        self._cleanup_timeout_s = float(cleanup_timeout_s)
        self._popen_factory = popen_factory
        self._control_runner = control_runner
        self._which = which
        self._monotonic = monotonic
        self._sleep = sleep
        self._registration_gate = registration_gate or _pace_scope_registration
        self._systemd_run: str | None = None
        self._systemctl: str | None = None
        self._bubblewrap: str | None = None
        self._mount: str | None = None
        self._unshare: str | None = None

    def preflight(self) -> None:
        systemd_run = self._which("systemd-run")
        systemctl = self._which("systemctl")
        bubblewrap = self._which("bwrap")
        mount = self._which("mount")
        unshare = self._which("unshare")
        if None in (systemd_run, systemctl, bubblewrap, mount, unshare):
            missing = [
                name
                for name, value in (
                    ("systemd-run", systemd_run),
                    ("systemctl", systemctl),
                    ("bwrap", bubblewrap),
                    ("mount", mount),
                    ("unshare", unshare),
                )
                if value is None
            ]
            raise SandboxInfrastructureError(
                f"required sandbox commands are unavailable: {', '.join(missing)}"
            )
        try:
            controllers = (self._cgroup_root / "cgroup.controllers").read_text().split()
        except OSError as exc:
            raise SandboxInfrastructureError(
                f"cgroup v2 is unavailable at {self._cgroup_root}: {exc}"
            ) from exc
        if "cpu" not in controllers and self._cpu_policy != "unlimited":
            raise SandboxInfrastructureError(
                f"cgroup v2 cpu controller is unavailable at {self._cgroup_root}"
            )
        self._systemd_run = systemd_run
        self._systemctl = systemctl
        self._bubblewrap = bubblewrap
        self._mount = mount
        self._unshare = unshare

    @staticmethod
    def _under(path: Path, root: Path) -> Path | None:
        try:
            return path.resolve().relative_to(root.resolve())
        except ValueError:
            return None

    @staticmethod
    def _plan_mirror(
        source: Path, target: Path, mirrors: list[tuple[Path, Path]]
    ) -> Path:
        target.mkdir(parents=True, exist_ok=False)
        mirrors.append((source.resolve(), target.resolve()))
        return target

    def _filesystem_sandbox(
        self,
        spec: ProcessSpec,
        *,
        mirror_root: Path | None = None,
        mirrors: list[tuple[Path, Path]] | None = None,
    ) -> ProcessSpec:
        """Expose only this package, its executable/runtime, and public imports.

        Saiblo remains the protocol/judging authority. This wrapper is strictly
        the hostile-player visibility boundary and is shared by all games and
        implementation languages.
        """

        assert self._bubblewrap is not None
        mounted = mirrors if mirrors is not None else []

        def visible_source(source: Path, label: str) -> Path:
            if mirror_root is None:
                return source
            return self._plan_mirror(source, mirror_root / label, mounted)

        workspace_source = visible_source(spec.cwd, "workspace")
        command = [
            self._bubblewrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/workspace",
            "--ro-bind",
            str(workspace_source),
            "/workspace",
        ]
        for target in ("/usr", "/etc"):
            source = system_path(target) if target == '/usr' or reference_root() else Path(target)
            if source.exists():
                command += ["--ro-bind", str(source), target]
        for target, source in (("/bin", "usr/bin"), ("/lib", "usr/lib"), ("/lib64", "usr/lib64")):
            path = system_path(target)
            if path.is_symlink() or not path.exists():
                command += ["--symlink", source, target]
            else:
                command += ["--ro-bind", str(path), target]

        argv = list(spec.argv)
        executable = Path(argv[0])
        runtime_root: Path | None = None
        program_root: Path | None = None
        if executable.is_absolute():
            relative = self._under(executable, spec.cwd)
            if relative is not None:
                argv[0] = str(Path("/workspace") / relative)
            elif executable.parent.name in {"bin", "Scripts"} and (
                (executable.parent.parent / "pyvenv.cfg").is_file()
                or (executable.parent.parent / "conda-meta").is_dir()
            ):
                lexical_runtime_root = executable.parent.parent
                runtime_root = lexical_runtime_root.resolve()
                runtime_source = visible_source(runtime_root, "runtime")
                command += ["--ro-bind", str(runtime_source), "/runtime"]
                argv[0] = str(Path("/runtime") / executable.relative_to(lexical_runtime_root))
                resolved_executable = executable.resolve()
                if self._under(resolved_executable, runtime_root) is None:
                    # Some venvs use an absolute interpreter symlink outside
                    # the environment (for example /root/miniconda3/bin/python).
                    # Keep the venv invocation path so pyvenv.cfg/site-packages
                    # semantics remain intact, while mounting only that base
                    # runtime at the symlink's original location.
                    external_runtime = resolved_executable.parent.parent.resolve()
                    external_source = visible_source(external_runtime, "runtime-base")
                    for parent in reversed(external_runtime.parents):
                        if parent != Path("/"):
                            command += ["--dir", str(parent)]
                    command += [
                        "--ro-bind",
                        str(external_source),
                        str(external_runtime),
                    ]
            elif not any(self._under(executable, root) is not None for root in (Path("/usr"), Path("/bin"))):
                program_root = executable.parent.resolve()
                program_source = visible_source(program_root, "program")
                command += ["--ro-bind", str(program_source), "/program"]
                argv[0] = str(Path("/program") / executable.name)

        for index, argument in enumerate(argv[1:], start=1):
            path = Path(argument)
            if not path.is_absolute():
                continue
            relative = self._under(path, spec.cwd)
            if relative is not None:
                argv[index] = str(Path("/workspace") / relative)
            elif runtime_root is not None and (relative := self._under(path, runtime_root)) is not None:
                argv[index] = str(Path("/runtime") / relative)
            elif program_root is not None and (relative := self._under(path, program_root)) is not None:
                argv[index] = str(Path("/program") / relative)

        environment = dict(spec.env)
        pythonpath = environment.get("PYTHONPATH")
        if pythonpath:
            translated: list[str] = []
            for index, item in enumerate(pythonpath.split(os.pathsep)):
                source = Path(item).resolve()
                if not source.exists():
                    continue
                target = f"/public-python/{index}"
                public_source = visible_source(source, f"public-python-{index}")
                command += ["--dir", "/public-python", "--ro-bind", str(public_source), target]
                translated.append(target)
            environment["PYTHONPATH"] = os.pathsep.join(translated)
        environment["HOME"] = "/tmp/home"
        command += ["--dir", "/tmp/home", "--chdir", "/workspace", "--", *argv]
        return ProcessSpec(tuple(command), spec.cwd, environment)

    def isolate_filesystem(self, spec: ProcessSpec) -> ProcessSpec:
        """Return the exact player visibility wrapper for certification probes."""

        if self._bubblewrap is None:
            self._bubblewrap = self._which("bwrap")
        if self._bubblewrap is None:
            raise SandboxInfrastructureError("bubblewrap is required for player isolation")
        return self._filesystem_sandbox(spec)

    def start(
        self,
        spec: ProcessSpec,
        *,
        match_id: str,
        player_index: int,
    ) -> ManagedProcess:
        self.preflight()
        assert self._systemd_run is not None
        assert self._unshare is not None
        unit_name = self._unit_name(match_id, player_index)
        gate_directory = self._make_gate_directory(unit_name)
        mirrors: list[tuple[Path, Path]] = []
        gate_path = gate_directory / "verified"
        entry_path = Path(__file__).with_name("scope_entry.py").resolve()
        private_mount_path = Path(__file__).with_name("private_mount.py").resolve()
        command = [
            self._systemd_run,
            *(["--user"] if self._user_mode else []),
            "--scope",
            "--quiet",
            f"--unit={unit_name}",
            *(["--property=CPUQuota=100%", "--property=CPUQuotaPeriodSec=100ms"]
              if self._cpu_policy == "one_cpu" else []),
            "--property=KillMode=control-group",
            "--",
            sys.executable,
            str(entry_path),
            str(gate_path),
            "--",
        ]
        process: subprocess.Popen[bytes] | None = None
        handle: ManagedProcess | None = None
        try:
            sandboxed = self._filesystem_sandbox(
                spec,
                mirror_root=gate_directory / "mounts",
                mirrors=mirrors,
            )
            mirror_plan = gate_directory / "mount-plan.json"
            mirror_plan.write_text(
                json.dumps(
                    [
                        {"source": str(source), "target": str(target)}
                        for source, target in mirrors
                    ]
                ),
                encoding="utf-8",
            )
            mirror_plan.chmod(0o600)
            command += [
                self._unshare,
                *(["--user", "--map-root-user"] if self._user_mode else []),
                "--mount",
                "--propagation",
                "private",
                sys.executable,
                str(private_mount_path),
                str(mirror_plan),
                "--",
                *sandboxed.argv,
            ]
            self._registration_gate()
            process = self._popen_factory(
                command,
                cwd=spec.cwd,
                env=self._launch_environment(sandboxed.env),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            control_group = self._wait_for_control_group(unit_name, process)
            cpu_path = self._resolve_cgroup_file(control_group, "cpu.max")
            observed = cpu_path.read_text(encoding="utf-8").strip() if cpu_path.exists() else None
            if self._cpu_policy == "one_cpu":
                if observed is None:
                    raise SandboxInfrastructureError("cpu.max is missing for finite CPU policy")
                verify_cpu_max(observed)
            elif observed is not None:
                fields = observed.split()
                if len(fields) != 2 or fields[0] != "max" or not fields[1].isdigit() or int(fields[1]) <= 0:
                    raise SandboxInfrastructureError(f"unexpected limited or malformed cpu.max: {observed!r}")
            else:
                # Missing cpu.max is valid only when this cgroup was not
                # delegated the CPU controller. Do not hide unrelated errors.
                enabled = (cpu_path.parent.parent / "cgroup.subtree_control").read_text().split()
                if "cpu" in enabled:
                    raise SandboxInfrastructureError("cpu.max missing despite enabled CPU controller")
            ancestors = {}
            parent = cpu_path.parent.parent
            while parent != self._cgroup_root and parent.is_relative_to(self._cgroup_root):
                limit = parent / "cpu.max"
                if limit.exists():
                    ancestors[str(parent.relative_to(self._cgroup_root))] = limit.read_text().strip()
                parent = parent.parent
            metadata = LaunchMetadata(
                launcher="systemd_scope",
                match_id=str(match_id),
                player_index=int(player_index),
                unit_name=unit_name,
                control_group=control_group,
                requested_cpu_quota_percent=CPU_QUOTA_PERCENT if self._cpu_policy == "one_cpu" else None,
                requested_period_usec=CPU_PERIOD_USEC if self._cpu_policy == "one_cpu" else None,
                observed_cpu_max=observed,
                cpu_policy=self._cpu_policy,
                ancestor_cpu_max=ancestors,
            )
            handle = ManagedProcess(
                process,
                metadata,
                terminate_callback=lambda: self._terminate_scope(unit_name),
                kill_callback=lambda: self._kill_scope(unit_name),
                finalize_callback=lambda: self._finalize_scope(
                    unit_name, gate_directory, mirrors
                ),
            )
            gate_path.touch(exist_ok=False)
            return handle
        except BaseException as exc:
            cleanup_detail = ""
            if handle is not None:
                outcome = handle.cleanup()
                cleanup_detail = outcome.detail
            elif process is not None:
                cleanup_detail = self._cleanup_partial_process(
                    process,
                    unit_name,
                    gate_directory,
                    mirrors,
                )
            else:
                shutil.rmtree(gate_directory, ignore_errors=True)
            if isinstance(exc, SandboxInfrastructureError):
                message = str(exc)
            else:
                message = f"could not create or verify player scope {unit_name}: {exc}"
            if cleanup_detail:
                message = f"{message}; cleanup: {cleanup_detail}"
            raise SandboxInfrastructureError(message) from exc

    def _unit_name(self, match_id: str, player_index: int) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(match_id)).strip(".-")
        safe = (safe or "match")[:40]
        return f"aa-arena-{safe}-p{int(player_index)}-{uuid.uuid4().hex[:10]}.scope"

    def _make_gate_directory(self, unit_name: str) -> Path:
        if self._temporary_root is not None:
            self._temporary_root.mkdir(parents=True, exist_ok=True)
        directory = Path(
            tempfile.mkdtemp(
                prefix=f"{unit_name[:-6]}-",
                dir=self._temporary_root,
            )
        ).resolve()
        directory.chmod(0o700)
        return directory

    def _launch_environment(self, extra: dict[str, str]) -> dict[str, str]:
        environment = safe_environment(extra)
        # BLAS/ML imports otherwise create a host-sized thread pool in each
        # player, which can exhaust its sandbox task allowance before gameplay.
        for name in (
            "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS", "TF_NUM_INTRAOP_THREADS", "TF_NUM_INTEROP_THREADS",
        ):
            environment[name] = "1"
        if self._user_mode:
            for name in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
                if name in os.environ:
                    environment[name] = os.environ[name]
        return environment

    def _run_control(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        if self._user_mode:
            argv = (argv[0], "--user", *argv[1:])
        return self._control_runner(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=self._startup_timeout_s,
        )

    def _wait_for_control_group(
        self,
        unit_name: str,
        process: subprocess.Popen[bytes],
    ) -> str:
        assert self._systemctl is not None
        deadline = self._monotonic() + self._startup_timeout_s
        last_error = ""
        while True:
            if process.poll() is not None:
                raise SandboxInfrastructureError(
                    f"systemd scope wrapper exited before verification: unit={unit_name} "
                    f"returncode={process.returncode}"
                )
            result = self._run_control(
                (self._systemctl, "show", "--property=ControlGroup", "--value", unit_name)
            )
            value = result.stdout.strip()
            if result.returncode == 0 and value:
                return value
            last_error = (result.stderr or result.stdout).strip()
            if self._monotonic() >= deadline:
                raise SandboxInfrastructureError(
                    f"systemd scope registration timed out: unit={unit_name} detail={last_error!r}"
                )
            self._sleep(0.01)

    def _resolve_cgroup_file(self, control_group: str, filename: str) -> Path:
        return _resolve_cgroup_file(self._cgroup_root, control_group, filename)

    def _terminate_scope(self, unit_name: str) -> None:
        assert self._systemctl is not None
        self._run_control(
            (
                self._systemctl,
                "kill",
                "--kill-who=all",
                "--signal=TERM",
                unit_name,
            )
        )

    def _kill_scope(self, unit_name: str) -> None:
        assert self._systemctl is not None
        self._run_control(
            (
                self._systemctl,
                "kill",
                "--kill-who=all",
                "--signal=KILL",
                unit_name,
            )
        )
        self._run_control((self._systemctl, "stop", unit_name))

    def _finalize_scope(
        self, unit_name: str, gate_directory: Path, mirrors: list[tuple[Path, Path]]
    ) -> CleanupOutcome:
        assert self._systemctl is not None
        # The systemd-run wrapper can exit after TERM while a PID-namespace
        # child is still alive in the scope. Always issue a cgroup-wide KILL
        # before declaring teardown complete; this is the hard descendant
        # cleanup boundary, not a best-effort parent-process check.
        self._kill_scope(unit_name)
        deadline = self._monotonic() + self._cleanup_timeout_s
        state = "active"
        clean = False
        while True:
            result = self._run_control((self._systemctl, "is-active", unit_name))
            state = result.stdout.strip().lower()
            clean = (
                state in {"inactive", "failed", "unknown", ""}
                or result.returncode in {3, 4}
            )
            if clean or self._monotonic() >= deadline:
                break
            self._sleep(0.01)
        details: list[str] = []
        if not clean:
            details.append(f"scope {unit_name} remains {state or 'active'}")
        try:
            shutil.rmtree(gate_directory)
        except FileNotFoundError:
            pass
        except OSError as exc:
            details.append(f"gate cleanup failed: {exc}")
        return CleanupOutcome(not details, "; ".join(details))

    def _cleanup_partial_process(
        self,
        process: subprocess.Popen[bytes],
        unit_name: str,
        gate_directory: Path,
        mirrors: list[tuple[Path, Path]],
    ) -> str:
        metadata = LaunchMetadata(
            launcher="systemd_scope",
            match_id="startup-failure",
            player_index=-1,
            unit_name=unit_name,
            requested_cpu_quota_percent=CPU_QUOTA_PERCENT if self._cpu_policy == "one_cpu" else None,
            requested_period_usec=CPU_PERIOD_USEC if self._cpu_policy == "one_cpu" else None,
        )
        handle = ManagedProcess(
            process,
            metadata,
            terminate_callback=lambda: self._terminate_scope(unit_name),
            kill_callback=lambda: self._kill_scope(unit_name),
            finalize_callback=lambda: self._finalize_scope(unit_name, gate_directory, mirrors),
        )
        return handle.cleanup().detail

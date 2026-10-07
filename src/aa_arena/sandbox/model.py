"""Game-independent process launch contracts."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol


class SandboxInfrastructureError(RuntimeError):
    """The requested player isolation could not be guaranteed."""


@dataclass(frozen=True)
class ProcessSpec:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.argv or not self.argv[0]:
            raise ValueError("process argv cannot be empty")
        object.__setattr__(self, "argv", tuple(str(item) for item in self.argv))
        object.__setattr__(self, "cwd", Path(self.cwd).resolve())
        object.__setattr__(
            self,
            "env",
            {str(key): str(value) for key, value in self.env.items()},
        )


@dataclass(frozen=True)
class LaunchMetadata:
    launcher: str
    match_id: str
    player_index: int
    unit_name: str | None = None
    control_group: str | None = None
    requested_cpu_quota_percent: int | None = None
    requested_period_usec: int | None = None
    observed_cpu_max: str | None = None
    cpu_policy: str = "one_cpu"
    ancestor_cpu_max: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CleanupOutcome:
    clean: bool
    detail: str = ""


def safe_environment(extra: Mapping[str, str]) -> dict[str, str]:
    """Build the deliberately small environment inherited by managed processes."""

    allowed = (
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SYSTEMROOT",
        "TMPDIR",
        "HOME",
    )
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment.update({str(key): str(value) for key, value in extra.items()})
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


class ManagedProcess:
    """A narrow Popen-compatible facade with idempotent bounded cleanup."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        metadata: LaunchMetadata,
        *,
        terminate_callback: Callable[[], None] | None = None,
        kill_callback: Callable[[], None] | None = None,
        finalize_callback: Callable[[], CleanupOutcome] | None = None,
    ) -> None:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.terminate()
            raise RuntimeError("failed to open process pipes")
        self._process = process
        self.metadata = metadata
        self.stdin: BinaryIO = process.stdin
        self.stdout: BinaryIO = process.stdout
        self.stderr: BinaryIO = process.stderr
        self._terminate_callback = terminate_callback or process.terminate
        self._kill_callback = kill_callback or process.kill
        self._finalize_callback = finalize_callback
        self._cleanup_outcome: CleanupOutcome | None = None

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def poll(self) -> int | None:
        return self._process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self._process.wait(timeout=timeout)

    def terminate(self) -> None:
        self._terminate_callback()

    def kill(self) -> None:
        self._kill_callback()

    def cleanup(self) -> CleanupOutcome:
        if self._cleanup_outcome is not None:
            return self._cleanup_outcome

        details: list[str] = []
        if self.poll() is None:
            try:
                self.terminate()
                self.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    self.kill()
                    self.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    details.append("process remained alive after SIGKILL")
                except OSError as exc:
                    details.append(f"kill failed: {exc}")
            except OSError as exc:
                details.append(f"terminate failed: {exc}")

        if self._finalize_callback is not None:
            try:
                finalized = self._finalize_callback()
            except OSError as exc:
                details.append(f"finalization failed: {exc}")
            else:
                if not finalized.clean:
                    details.append(finalized.detail or "finalization did not complete")

        for stream in (self.stdin, self.stdout, self.stderr):
            try:
                stream.close()
            except OSError as exc:
                details.append(f"pipe close failed: {exc}")

        if self.poll() is None:
            details.append("process is still alive")
        self._cleanup_outcome = CleanupOutcome(not details, "; ".join(details))
        return self._cleanup_outcome


class ProcessLauncher(Protocol):
    def preflight(self) -> None:
        """Raise before launch when the requested policy cannot be attempted."""

    def start(
        self,
        spec: ProcessSpec,
        *,
        match_id: str,
        player_index: int,
    ) -> ManagedProcess:
        """Start one process and return a managed handle."""

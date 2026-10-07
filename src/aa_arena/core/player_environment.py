"""Shared, repository-managed Python environment for submitted players."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any

from aa_arena.sandbox import SandboxInfrastructureError

PLAYER_ENV_VARIABLE = "AA_ARENA_PLAYER_ENV"
OWNER_RECORD = ".aa-arena-player-environment.json"
_PROBE_LOCK = Lock()


class PlayerEnvironmentError(SandboxInfrastructureError):
    """The isolated Python environment required by a player is unavailable."""


def player_environment_root(
    *, environment: Mapping[str, str] | None = None
) -> Path:
    """Return the configured player environment without consulting active envs."""

    values = os.environ if environment is None else environment
    configured = values.get(PLAYER_ENV_VARIABLE)
    if configured:
        return Path(configured).expanduser().resolve()
    repository_root = Path(__file__).resolve().parents[3]
    return (repository_root / ".player-env").resolve()


def _signature(path: Path) -> tuple[int, int, int, int, int]:
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


@lru_cache(maxsize=32)
def _probe_python(
    interpreter: str,
    interpreter_signature: tuple[int, int, int, int, int],
    marker_signature: tuple[int, int, int, int, int],
    owner_signature: tuple[int, int, int, int, int] | None,
) -> dict[str, Any]:
    del interpreter_signature, marker_signature, owner_signature
    try:
        completed = subprocess.run(
            (
                interpreter,
                "-I",
                "-c",
                (
                    "import json, sys; print(json.dumps({"
                    "'version': f'{sys.version_info.major}.{sys.version_info.minor}',"
                    "'prefix': sys.prefix, 'base_prefix': sys.base_prefix,"
                    "'executable': sys.executable}))"
                ),
            ),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PlayerEnvironmentError(
            f"cannot inspect shared player Python {interpreter}: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise PlayerEnvironmentError(
            f"cannot inspect shared player Python {interpreter}: {detail or completed.returncode}"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PlayerEnvironmentError(
            f"shared player Python returned invalid identity: {completed.stdout.strip()!r}"
        ) from exc
    if not isinstance(value, dict):
        raise PlayerEnvironmentError("shared player Python returned a non-object identity")
    return value


def _owned_conda_marker(root: Path) -> Path:
    owner = root / OWNER_RECORD
    if not owner.is_file():
        raise PlayerEnvironmentError(
            f"Conda player environment is not owned by AA-Arena: {root}; "
            f"install environment/player-environment-owner.json as {owner.name}"
        )
    try:
        value = json.loads(owner.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlayerEnvironmentError(f"invalid AA-Arena player environment record: {owner}") from exc
    if value != {
        "schema_version": 1,
        "managed_by": "AA-Arena",
        "purpose": "shared-player-python",
        "python": "3.10",
    }:
        raise PlayerEnvironmentError(f"invalid AA-Arena player environment record: {owner}")
    return owner


def _validated_python(root: Path) -> Path:
    if not root.is_dir():
        raise PlayerEnvironmentError(
            f"shared player environment does not exist: {root}; "
            "run conda env create --prefix .player-env --file "
            "environment/player-environment.yml"
        )
    venv_marker = root / "pyvenv.cfg"
    conda_marker = root / "conda-meta" / "history"
    if venv_marker.is_file():
        marker = venv_marker
        owner: Path | None = None
    elif conda_marker.is_file():
        marker = conda_marker
        owner = _owned_conda_marker(root)
    else:
        raise PlayerEnvironmentError(f"shared player path is not a virtual environment: {root}")
    interpreter = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise PlayerEnvironmentError(
            f"shared player environment has no executable Python: {interpreter}"
        )
    try:
        fingerprint = (
            _signature(interpreter),
            _signature(marker),
            _signature(owner) if owner is not None else None,
        )
    except OSError as exc:
        raise PlayerEnvironmentError(f"cannot stat shared player environment: {exc}") from exc
    with _PROBE_LOCK:
        identity = _probe_python(str(interpreter), *fingerprint)
    version = identity.get("version")
    if version != "3.10":
        raise PlayerEnvironmentError(
            f"shared player environment requires Python 3.10, got {version or 'unknown'}"
        )
    try:
        observed_prefix = Path(str(identity.get("prefix"))).resolve()
        observed_executable = Path(str(identity.get("executable"))).resolve()
    except (OSError, TypeError, ValueError) as exc:
        raise PlayerEnvironmentError("shared player Python returned invalid paths") from exc
    if observed_prefix != root or observed_executable != interpreter.resolve():
        raise PlayerEnvironmentError(
            "shared player Python identity does not match its configured environment"
        )
    if owner is None and identity.get("base_prefix") == identity.get("prefix"):
        raise PlayerEnvironmentError(f"shared player path is not a virtual environment: {root}")
    return interpreter


def player_python(*, environment: Mapping[str, str] | None = None) -> Path:
    """Return the shared venv interpreter, failing closed when it is invalid."""

    return _validated_python(player_environment_root(environment=environment))


def player_command(
    script: str = "main.py", *, environment: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Build a Python player command without falling back to system Python."""

    interpreter = player_python(environment=environment)
    # Filesystem/network isolation is applied once, for every language, by
    # SystemdScopeLauncher. Keeping this command plain avoids nested bwrap and
    # guarantees C++ and Python players receive the same visibility policy.
    return (str(interpreter), script)

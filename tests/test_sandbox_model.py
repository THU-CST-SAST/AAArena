from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from aa_arena.sandbox import DirectLauncher, ProcessSpec, safe_environment


def test_process_spec_normalizes_values(tmp_path: Path) -> None:
    spec = ProcessSpec((Path("/bin/echo"), 7), tmp_path, {"NUMBER": 9})

    assert spec.argv == ("/bin/echo", "7")
    assert spec.cwd == tmp_path.resolve()
    assert spec.env == {"NUMBER": "9"}


def test_process_spec_rejects_empty_argv(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="argv cannot be empty"):
        ProcessSpec((), tmp_path)


def test_safe_environment_does_not_copy_arbitrary_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AA_ARENA_SECRET_PROBE", "do-not-copy")

    environment = safe_environment({"PLAYER_SETTING": "yes"})

    assert "AA_ARENA_SECRET_PROBE" not in environment
    assert environment["PLAYER_SETTING"] == "yes"
    assert environment["PYTHONUNBUFFERED"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"


def test_safe_environment_drops_ambient_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch evaluator-only modules leaking into submitted Python players."""

    monkeypatch.setenv("PYTHONPATH", "/evaluator/private/modules")

    environment = safe_environment({})

    assert "PYTHONPATH" not in environment


def test_direct_launcher_is_explicit_and_popen_compatible(tmp_path: Path) -> None:
    launcher = DirectLauncher()
    launcher.preflight()

    process = launcher.start(
        ProcessSpec((sys.executable, "-c", "print('ready')"), tmp_path),
        match_id="unit-test",
        player_index=0,
    )

    assert process.stdout.read() == b"ready\n"
    assert process.wait(timeout=2) == 0
    assert process.returncode == 0
    assert process.metadata.launcher == "direct"
    assert process.metadata.match_id == "unit-test"
    assert process.metadata.player_index == 0
    assert process.cleanup().clean is True


def test_managed_process_cleanup_terminates_and_is_idempotent(tmp_path: Path) -> None:
    process = DirectLauncher().start(
        ProcessSpec((sys.executable, "-c", "import time; time.sleep(60)"), tmp_path),
        match_id="cleanup-test",
        player_index=1,
    )

    first = process.cleanup()
    second = process.cleanup()

    assert first.clean is True
    assert second == first
    assert process.poll() is not None


def test_managed_process_wait_preserves_timeout_expired(tmp_path: Path) -> None:
    process = DirectLauncher().start(
        ProcessSpec((sys.executable, "-c", "import time; time.sleep(60)"), tmp_path),
        match_id="timeout-test",
        player_index=2,
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.01)
    finally:
        process.cleanup()

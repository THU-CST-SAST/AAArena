from __future__ import annotations

import select
import subprocess
import sys
from pathlib import Path

import pytest

from aa_arena.sandbox import scope_entry


def test_scope_entry_waits_for_gate_before_execing_player(tmp_path: Path) -> None:
    gate = tmp_path / "verified"
    process = subprocess.Popen(
        (
            sys.executable,
            str(Path(scope_entry.__file__).resolve()),
            str(gate),
            "--",
            sys.executable,
            "-c",
            "print('player-started')",
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    try:
        readable, _, _ = select.select([process.stdout], [], [], 0.1)
        assert readable == []

        gate.touch()
        stdout, stderr = process.communicate(timeout=2)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)

    assert process.returncode == 0
    assert stdout == b"player-started\n"
    assert stderr == b""


def test_scope_entry_rejects_empty_player_argv(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="player argv cannot be empty"):
        scope_entry.run(tmp_path / "verified", (), timeout_s=0.01)


def test_scope_entry_times_out_when_verification_never_arrives(tmp_path: Path) -> None:
    with pytest.raises(TimeoutError, match="verification gate was not released"):
        scope_entry.run(tmp_path / "missing", ("/bin/true",), timeout_s=0.02)

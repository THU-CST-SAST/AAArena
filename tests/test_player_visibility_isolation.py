from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from aa_arena.sandbox import ProcessSpec
from aa_arena.sandbox.systemd import SystemdScopeLauncher


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is unavailable")
def test_player_namespace_cannot_read_sibling_secret_or_use_network(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    secret = tmp_path / "opponent-source-sentinel"
    secret.write_text("must-not-be-readable", encoding="utf-8")
    (workspace / "probe.py").write_text(
        "import json, socket\n"
        "from pathlib import Path\n"
        f"visible = Path({str(secret)!r}).exists()\n"
        "network = True\n"
        "try:\n"
        "    socket.socket().connect(('1.1.1.1', 53))\n"
        "except OSError:\n"
        "    network = False\n"
        "print(json.dumps({'secret_visible': visible, 'network': network}))\n",
        encoding="utf-8",
    )
    launcher = SystemdScopeLauncher(which=shutil.which)
    spec = launcher.isolate_filesystem(
        ProcessSpec((shutil.which("python3") or "/usr/bin/python3", "probe.py"), workspace)
    )
    completed = subprocess.run(
        spec.argv,
        cwd=spec.cwd,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(completed.stdout) == {"secret_visible": False, "network": False}

"""Create player bind mirrors in a private mount namespace, then exec bwrap."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    arguments = sys.argv[1:]
    if len(arguments) < 3 or arguments[1] != "--":
        raise ValueError("usage: private_mount.py PLAN.json -- COMMAND [ARG ...]")
    plan_path = Path(arguments[0])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    mount = shutil.which("mount")
    if mount is None:
        raise FileNotFoundError("mount is required inside the private mount namespace")
    for row in plan:
        subprocess.run(
            (mount, "--bind", str(row["source"]), str(row["target"])),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    command = arguments[2:]
    os.execvpe(command[0], command, os.environ)
    raise AssertionError("exec returned")


if __name__ == "__main__":
    raise SystemExit(main())

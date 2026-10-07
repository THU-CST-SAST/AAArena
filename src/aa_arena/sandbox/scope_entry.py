"""Trusted gate between transient-scope creation and player execution."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path


def run(gate: Path, argv: Sequence[str], *, timeout_s: float = 30.0) -> int:
    """Wait for verified policy, then replace this helper with the player."""

    gate = Path(gate)
    arguments = tuple(str(item) for item in argv)
    if not arguments or not arguments[0]:
        raise ValueError("player argv cannot be empty")
    deadline = time.monotonic() + float(timeout_s)
    while not gate.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"sandbox verification gate was not released: {gate}")
        time.sleep(0.01)
    os.execvpe(arguments[0], list(arguments), os.environ)
    raise AssertionError("os.execvpe returned")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 3 or arguments[1] != "--":
        raise ValueError("usage: scope_entry.py GATE -- PLAYER [ARG ...]")
    return run(Path(arguments[0]), arguments[2:])


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


GEOMETRY_ROOT = (
    Path(__file__).resolve().parents[1] / "games" / "miracle" / "backend" / "Geometry"
)


def test_unreachable_path_search_stays_inside_the_finite_map() -> None:
    code = """
from calculator import cube_neighbor, search_path

destination = (2, -2, 0)
obstacles = [cube_neighbor(destination, direction) for direction in range(6)]
print(search_path((0, 0, 0), destination, obstacles, []))
"""

    completed = subprocess.run(
        (sys.executable, "-c", code),
        cwd=GEOMETRY_ROOT,
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )

    assert completed.stdout.strip() == "False"

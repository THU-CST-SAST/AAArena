from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

EXPECTED_BACKEND_SOURCES = {
    "antwar": ".",
    "antwar2": "logic/gamecode_logic",
    "aquawar": "logic/gamecode_logic",
    "generals": "logic/gamecode_logic",
    "lostspace": ".",
    "miracle": ".",
    "rollman": ".",
    "snakego": "logic/gamecode_logic",
}
LAYOUT_CLASS_NAMES = {
    "antwar": "AntWarLayout",
    "antwar2": "AntWar2Layout",
    "aquawar": "AquaWarLayout",
    "generals": "GeneralsLayout",
    "lostspace": "LostSpaceLayout",
    "miracle": "MiracleLayout",
    "rollman": "RollmanLayout",
    "snakego": "SnakeGoLayout",
}


def load_runtime(game: str):
    runtime_path = REPOSITORY_ROOT / "games" / game / "evaluator" / "runtime.py"
    module_name = f"aa_arena_backend_layout_test_{game}"
    spec = importlib.util.spec_from_file_location(module_name, runtime_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {runtime_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class BackendLayoutTest(unittest.TestCase):
    def test_project_declares_python_backend_dependencies(self) -> None:
        project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        dependency_names = {
            re.split(r"[<>=!~\[]", dependency, maxsplit=1)[0].lower()
            for dependency in project["project"]["dependencies"]
        }
        self.assertEqual(
            dependency_names,
            {
                "cmake",
                "cloudpickle",
                "gym",
                "gym-notices",
                "numpy",
                "python-dotenv",
                "requests",
                "tqdm",
                "websockets",
            },
        )

    def test_all_backends_resolve_inside_their_game_directories(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aa-arena-layout-") as temporary_directory:
            build_root = Path(temporary_directory) / "build"
            for game, relative_source in EXPECTED_BACKEND_SOURCES.items():
                runtime = load_runtime(game)
                layout_type = getattr(runtime, LAYOUT_CLASS_NAMES[game])
                parameters = inspect.signature(layout_type.from_root).parameters
                arguments = [REPOSITORY_ROOT]
                if "build_root" in parameters:
                    arguments.append(build_root / game)
                layout = layout_type.from_root(*arguments)
                expected = REPOSITORY_ROOT / "games" / game / "backend" / relative_source

                self.assertEqual(layout.backend_source_root, expected, game)
                layout.validate()


if __name__ == "__main__":
    unittest.main()

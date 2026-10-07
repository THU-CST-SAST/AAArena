from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_GAMES = (
    "antwar",
    "antwar2",
    "aquawar",
    "dorado",
    "generals",
    "lostspace",
    "lota",
    "miracle",
    "monecraft",
    "pacman",
    "rollman",
    "snakego",
)


class StandaloneRuntimeTest(unittest.TestCase):
    def test_all_game_evaluators_load_without_external_agentbench_installation(self) -> None:
        """Catch missing framework files or evaluator imports hidden by another checkout."""

        probe = textwrap.dedent(
            """
            import sys
            from pathlib import Path

            repository = Path(sys.argv[1])
            expected_games = tuple(sys.argv[2].split(","))
            sys.path.insert(0, str(repository / "src"))

            assert (repository / "src" / "aa_arena").is_dir()
            assert not (repository / "src" / "agentbench").exists()

            from aa_arena.core import Evaluator, available_games
            from aa_arena.core.registry import get_plugin
            from aa_arena.replay import available_narrators

            games_root = repository / "games"
            assert tuple(available_games(games_root)) == expected_games
            for game in expected_games:
                plugin = get_plugin(game, games_root)
                evaluator = plugin.evaluator_factory(games_root / game)
                assert isinstance(evaluator, Evaluator), game

            assert set(available_narrators(games_root)) == set(expected_games)
            """
        )
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                probe,
                str(REPOSITORY_ROOT),
                ",".join(EXPECTED_GAMES),
            ],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()

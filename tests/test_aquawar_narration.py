from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aa_arena.replay import narrate  # noqa: E402


class AquaWarNarrationTest(unittest.TestCase):
    def test_digest_folds_repeated_identical_actions(self) -> None:
        repeated = {
            "winner": -1,
            "rounds": 1,
            "gamestate": 4,
            "cur_turn": 0,
            "players": [{"fight_fish": []}, {"fight_fish": []}],
            "operation": [
                {"Action": "Action", "Type": 0, "MyPos": 0, "EnemyPos": 1, "Player": 0}
            ],
        }
        final = {
            **repeated,
            "winner": 0,
            "gamestate": 5,
            "operation": [{"Action": "Finish", "Player": 0}],
        }
        with tempfile.TemporaryDirectory() as directory:
            replay = Path(directory) / "replay.json"
            replay.write_text(json.dumps([repeated] * 500 + [final]), encoding="utf-8")
            result = narrate(
                "aquawar",
                replay,
                detail="digest",
                games_root=REPOSITORY_ROOT / "games",
            )

        self.assertIn("已折叠", result.text)
        self.assertLessEqual(len(result.text.splitlines()), 80)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PLAYER_COUNTS = {
    "antwar": 499,
    "antwar2": 500,
    "aquawar": 386,
    "dorado": 332,
    "generals": 509,
    "lostspace": 150,
    "lota": 212,
    "miracle": 355,
    "monecraft": 119,
    "pacman": 51,
    "rollman": 234,
    "snakego": 436,
}
GENERATED_RESULT_NAMES = {
    "runnable.json",
    "measured_elo.json",
    "measured_ranking.tsv",
}
EXPECTED_AVAILABILITY_GAMES = {
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
}
EXPECTED_ELO_GAMES = EXPECTED_AVAILABILITY_GAMES


class AssetLayoutTest(unittest.TestCase):
    def test_manifests_match_pool_directories_and_expected_counts(self) -> None:
        total = 0
        for game in EXPECTED_PLAYER_COUNTS:
            ratings=json.loads((REPOSITORY_ROOT / "results/elo" / game / "measured_elo.json").read_text())
            ratings=ratings.get("ratings") if isinstance(ratings,dict) else ratings
            expected_count=sum(i>8 and i%2==0 for i in range(1,len(ratings)+1))
            players_dir = REPOSITORY_ROOT / "games" / game / "players"
            with (players_dir / "manifest.tsv").open(encoding="utf-8", newline="") as stream:
                records = list(csv.DictReader(stream, delimiter="\t"))
            directories = sorted(
                path.name for path in (players_dir / "pool").iterdir() if path.is_dir()
            )

            self.assertEqual(len(records), expected_count, game)
            self.assertEqual(len(directories), expected_count, game)
            manifest_directories = sorted(
                record["dir"].removeprefix("pool/") for record in records
            )
            self.assertEqual(manifest_directories, directories, game)
            total += len(records)

        self.assertEqual(total, 909)

    def test_player_pools_do_not_contain_generated_results(self) -> None:
        found = sorted(
            str(path.relative_to(REPOSITORY_ROOT))
            for path in (REPOSITORY_ROOT / "games").glob("*/players/*")
            if path.name in GENERATED_RESULT_NAMES
        )
        self.assertEqual(found, [])

    def test_results_are_separated_with_exact_game_coverage(self) -> None:
        availability_root = REPOSITORY_ROOT / "results" / "availability"
        elo_root = REPOSITORY_ROOT / "results" / "elo"
        self.assertTrue(availability_root.is_dir())
        self.assertTrue(elo_root.is_dir())

        availability_games = {
            path.name for path in availability_root.iterdir() if path.is_dir()
        }
        elo_games = {path.name for path in elo_root.iterdir() if path.is_dir()}
        self.assertEqual(availability_games, EXPECTED_AVAILABILITY_GAMES)
        self.assertEqual(elo_games, EXPECTED_ELO_GAMES)

        for game in EXPECTED_AVAILABILITY_GAMES:
            self.assertTrue((availability_root / game / "runnable.json").is_file(), game)
        for game in EXPECTED_ELO_GAMES:
            self.assertTrue((elo_root / game / "measured_elo.json").is_file(), game)
            self.assertTrue((elo_root / game / "measured_ranking.tsv").is_file(), game)



if __name__ == "__main__":
    unittest.main()

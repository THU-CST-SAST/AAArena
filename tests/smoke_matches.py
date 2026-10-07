"""Run one representative standalone match for each head-to-head game."""

from __future__ import annotations

import argparse
import inspect
import json
import sys
import tempfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aa_arena.core import EvaluateResult, EvaluationStatus, Evaluator, PlayerRef  # noqa: E402
from aa_arena.core.registry import get_plugin  # noqa: E402


MATCH_GAMES = (
    "antwar",
    "antwar2",
    "aquawar",
    "generals",
    "lostspace",
    "miracle",
    "rollman",
    "snakego",
)


def verified_player(game: str) -> PlayerRef:
    snapshot_path = REPOSITORY_ROOT / "results" / "availability" / game / "runnable.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    for row in snapshot["rows"]:
        if not row["verified"]:
            continue
        player_id = row["player_id"]
        package = REPOSITORY_ROOT / "games" / game / "players" / "pool" / player_id
        if package.is_dir():
            return PlayerRef(player_id=player_id, code_path=str(package))
    raise RuntimeError(f"{game}: availability snapshot has no existing verified player")


def isolated_evaluator(game: str, temporary_root: Path) -> tuple[Evaluator, tuple[str, ...]]:
    games_root = REPOSITORY_ROOT / "games"
    game_dir = games_root / game
    plugin = get_plugin(game, games_root)
    evaluator_type = type(plugin.evaluator_factory(game_dir))
    parameters = inspect.signature(evaluator_type).parameters
    keyword_arguments: dict[str, Path] = {}
    if "build_root" in parameters:
        keyword_arguments["build_root"] = temporary_root / "build"
    if "artifact_root" in parameters:
        keyword_arguments["artifact_root"] = temporary_root / "matches"
    evaluator = evaluator_type(game_dir, **keyword_arguments)
    if not isinstance(evaluator, Evaluator):
        raise TypeError(f"{game}: evaluator does not satisfy the public protocol")
    return evaluator, plugin.roles


def _run_match_at(game: str, temporary_root: Path) -> None:
    player = verified_player(game)
    temporary_root.mkdir(parents=True, exist_ok=True)
    evaluator, roles = isolated_evaluator(game, temporary_root)
    result = evaluator.evaluate([player] * len(roles), list(roles), seed=20260828)
    if not isinstance(result, EvaluateResult):
        raise TypeError(f"{game}: evaluator returned {type(result).__name__}")
    if result.status is EvaluationStatus.INFRA_ERROR:
        raise RuntimeError(f"{game}: infrastructure error: {result.diagnostic}")
    print(
        json.dumps(
            {
                "game": game,
                "player_id": player.player_id,
                "status": result.status.value,
                "winner": result.winner,
                "scores": result.scores,
                "rounds": result.rounds,
                "diagnostic": result.diagnostic,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )


def run_match(game: str, temporary_root: Path | None = None) -> None:
    if temporary_root is not None:
        _run_match_at(game, Path(temporary_root))
        return
    with tempfile.TemporaryDirectory(prefix=f"aa-arena-{game}-") as temporary_directory:
        _run_match_at(game, Path(temporary_directory))


def run_matches(artifact_root: Path | None = None) -> None:
    if artifact_root is not None:
        artifact_root = Path(artifact_root)
        artifact_root.mkdir(parents=True, exist_ok=True)
    for game in MATCH_GAMES:
        temporary_root = None if artifact_root is None else artifact_root / game
        run_match(game, temporary_root=temporary_root)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path)
    arguments = parser.parse_args(argv)
    run_matches(arguments.artifact_root)



if __name__ == "__main__":
    main()

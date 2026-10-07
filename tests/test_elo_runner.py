from __future__ import annotations

import inspect
import json
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from aa_arena.core.contract import EvaluateResult, EvaluationStatus
from aa_arena.elo import runner
from aa_arena.elo.model import EloCase, EloPlayer
from aa_arena.elo.runner import MatchRecord, run_elo


class InlineExecutor:
    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers

    def __enter__(self) -> InlineExecutor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def submit(self, function, *args):
        future: Future = Future()
        try:
            future.set_result(function(*args))
        except BaseException as exc:  # pragma: no cover - mirrors Future behavior
            future.set_exception(exc)
        return future


@pytest.fixture
def configured_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "AA-Arena"
    (root / "runs" / "elo").mkdir(parents=True)
    players = tuple(
        EloPlayer(player_id, root / "games" / "demo" / "players" / "pool" / player_id)
        for player_id in ("alpha", "beta")
    )
    for player in players:
        player.package_root.mkdir(parents=True)
    monkeypatch.setattr(runner, "repository_root", lambda: root)
    monkeypatch.setattr(runner, "load_verified_players", lambda game: players)
    monkeypatch.setattr(
        runner,
        "get_plugin",
        lambda game: SimpleNamespace(roles=("P0", "P1"), roles_symmetric=False),
    )
    monkeypatch.setattr(runner, "_git_commit", lambda root: "deadbeef")
    monkeypatch.setattr(runner, "ProcessPoolExecutor", InlineExecutor)
    return root, players


def test_run_elo_rejects_work_directory_outside_repository(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="runs/elo"):
        run_elo("antwar2", tmp_path)


def test_run_elo_refuses_to_mix_changed_plan(
    configured_repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = configured_repository
    work = root / "runs" / "elo" / "test"
    run_elo("demo", work, workers=1, degree=1, max_matches=0)
    changed = (
        EloPlayer("alpha", root / "games/demo/players/pool/alpha"),
        EloPlayer("gamma", root / "games/demo/players/pool/gamma"),
    )
    monkeypatch.setattr(runner, "load_verified_players", lambda game: changed)

    with pytest.raises(ValueError, match="plan does not match"):
        run_elo("demo", work, workers=1, degree=1, max_matches=0)


def test_run_elo_can_resume_same_plan_after_repository_commit_changes(
    configured_repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = configured_repository
    work = root / "runs" / "elo" / "commit-change"
    run_elo("demo", work, workers=1, degree=1, max_matches=0)

    monkeypatch.setattr(runner, "_git_commit", lambda root: "fixed-judger")
    resumed = run_elo("demo", work, workers=1, degree=1, max_matches=0)

    assert resumed.planned_matches == 2
    plan = json.loads((work / "plan.json").read_text(encoding="utf-8"))
    assert plan["git_commit"] == "deadbeef"


def test_public_signature_has_no_external_repository_parameter() -> None:
    assert tuple(inspect.signature(run_elo).parameters) == (
        "game",
        "work_dir",
        "workers",
        "degree",
        "seed",
        "max_matches",
    )


def test_run_elo_excludes_verified_players_that_have_no_schedulable_role(
    configured_repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = configured_repository
    players = (
        EloPlayer("rollman-a", root / "games/demo/players/pool/rollman-a", track="rollman"),
        EloPlayer("ghost-a", root / "games/demo/players/pool/ghost-a", track="ghost"),
        EloPlayer("unclassified", root / "games/demo/players/pool/unclassified"),
    )
    monkeypatch.setattr(runner, "load_verified_players", lambda game: players)
    monkeypatch.setattr(
        runner,
        "get_plugin",
        lambda game: SimpleNamespace(
            roles=("rollman", "ghost"), roles_symmetric=False
        ),
    )
    work = root / "runs" / "elo" / "role-constrained"

    run_elo("demo", work, workers=1, degree=24, max_matches=0)

    plan = json.loads((work / "plan.json").read_text(encoding="utf-8"))
    assert [player["player_id"] for player in plan["players"]] == [
        "ghost-a",
        "rollman-a",
    ]
    ratings = json.loads((work / "measured_elo.json").read_text(encoding="utf-8"))
    assert {row["player_id"] for row in ratings} == {"ghost-a", "rollman-a"}


def test_valid_cases_are_persisted_and_skipped_on_resume(
    configured_repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = configured_repository
    calls: list[str] = []

    def evaluate(game, case, build_root, artifact_root):
        calls.append(case.case_id)
        return MatchRecord(case.case_id, "complete", case.player_a.player_id, 1.0)

    monkeypatch.setattr(runner, "_evaluate_case", evaluate)
    work = root / "runs" / "elo" / "resume"

    first = run_elo("demo", work, workers=1, degree=1)
    second = run_elo("demo", work, workers=1, degree=1)

    assert first.valid_matches == second.valid_matches == 2
    assert calls == ["m00000000", "m00000001"]
    assert json.loads((work / "matches" / "m00000000.json").read_text())["status"] == "complete"


def test_infra_error_is_not_rated_and_is_retried(
    configured_repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = configured_repository
    results = [
        MatchRecord("", "infra_error", None, None, diagnostic="host unavailable"),
        MatchRecord("", "complete", "alpha", 1.0),
        MatchRecord("", "complete", "alpha", 1.0),
    ]

    def evaluate(game, case, build_root, artifact_root):
        return results.pop(0).__class__(case_id=case.case_id, **{
            key: value for key, value in results_template.pop(0).items()
        })

    results_template = [
        {"status": "infra_error", "winner_player_id": None, "score_a": None,
         "diagnostic": "host unavailable"},
        {"status": "complete", "winner_player_id": "alpha", "score_a": 1.0},
        {"status": "complete", "winner_player_id": "alpha", "score_a": 1.0},
    ]
    monkeypatch.setattr(runner, "_evaluate_case", evaluate)
    work = root / "runs" / "elo" / "retry"

    first = run_elo("demo", work, workers=1, degree=1)
    second = run_elo("demo", work, workers=1, degree=1)

    assert first.infra_errors == 1
    assert first.valid_matches == 1
    assert second.infra_errors == 0
    assert second.valid_matches == 2


def test_game_error_with_official_winner_is_rated(
    configured_repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = configured_repository
    records = [
        {"status": "game_error", "winner_player_id": "alpha", "score_a": 1.0},
        {"status": "complete", "winner_player_id": None, "score_a": 0.5},
    ]

    def evaluate(game, case, build_root, artifact_root):
        return MatchRecord(case_id=case.case_id, **records.pop(0))

    monkeypatch.setattr(runner, "_evaluate_case", evaluate)
    summary = run_elo("demo", root / "runs" / "elo" / "game-error", workers=1, degree=1)

    assert summary.game_errors == 1
    assert summary.valid_matches == 2


def test_evaluate_case_maps_the_official_winner_role(
    configured_repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, players = configured_repository

    class FakeEvaluator:
        def __init__(
            self,
            game_dir: Path,
            *,
            build_root: Path | None = None,
            artifact_root: Path | None = None,
            timeout_s: float = 1.0,
        ) -> None:
            self.timeout_s = timeout_s

        def evaluate(self, player_refs, roles, seed):
            assert self.timeout_s == 1800.0
            return EvaluateResult(EvaluationStatus.GAME_ERROR, winner="P0")

    plugin = SimpleNamespace(
        evaluator_factory=lambda game_dir: FakeEvaluator(game_dir),
        roles=("P0", "P1"),
        roles_symmetric=False,
    )
    monkeypatch.setattr(runner, "get_plugin", lambda game: plugin)
    case = EloCase(
        "m00000000",
        players[0],
        players[1],
        ("beta", "alpha"),
        ("P0", "P1"),
        7,
    )

    record = runner._evaluate_case(
        "demo", case, root / "runs/elo/x/build", root / "runs/elo/x/artifacts"
    )

    assert record.status == "game_error"
    assert record.winner_player_id == "beta"
    assert record.score_a == 0.0


def test_evaluate_case_retries_game_error_without_official_winner(
    configured_repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, players = configured_repository

    class FakeEvaluator:
        def evaluate(self, player_refs, roles, seed):
            return EvaluateResult(
                EvaluationStatus.GAME_ERROR,
                diagnostic="match stalled",
            )

    plugin = SimpleNamespace(
        evaluator_factory=lambda game_dir: FakeEvaluator(),
        roles=("P0", "P1"),
        roles_symmetric=False,
    )
    monkeypatch.setattr(runner, "get_plugin", lambda game: plugin)
    case = EloCase(
        "m00000000",
        players[0],
        players[1],
        ("alpha", "beta"),
        ("P0", "P1"),
        7,
    )

    record = runner._evaluate_case(
        "demo", case, root / "runs/elo/x/build", root / "runs/elo/x/artifacts"
    )

    assert record.status == "infra_error"
    assert record.winner_player_id is None
    assert record.score_a is None
    assert record.diagnostic == "game_error missing official winner: match stalled"

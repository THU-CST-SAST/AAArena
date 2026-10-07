"""Repository-bound, resumable execution of native Elo matches."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aa_arena.core.contract import EvaluationStatus, PlayerRef
from aa_arena.core.registry import get_plugin
from aa_arena.elo.model import (
    EloCase,
    build_cases,
    load_verified_players,
    repository_root,
)
from aa_arena.elo.rating import PlayerRating, fit_ratings


@dataclass(frozen=True)
class MatchRecord:
    """Durable result for one planned case."""

    case_id: str
    status: str
    winner_player_id: str | None
    score_a: float | None
    diagnostic: str | None = None
    replay_path: str | None = None
    scores: dict[str, float] | None = None
    rounds: int | None = None


@dataclass(frozen=True)
class EloRunSummary:
    """Current state of one resumable Elo work directory."""

    game: str
    planned_matches: int
    valid_matches: int
    game_errors: int
    infra_errors: int
    pending_matches: int
    complete: bool
    work_dir: Path


def _git_commit(root: Path) -> str | None:
    if not (root / ".git").exists():
        return None
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _source_hash(root: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    paths = sorted((root / "src" / "aa_arena").rglob("*.py"))
    manifest = root / "assets" / "manifest.json"
    if manifest.exists():
        paths.append(manifest)
    for path in paths:
        relative = path.relative_to(root).as_posix().encode()
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(8,"big")); digest.update(relative)
        digest.update(len(data).to_bytes(8,"big")); digest.update(data)
    return digest.hexdigest()


def _atomic_write_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, payload)


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary_path = Path(handle.name)
        try:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
    os.replace(temporary_path, path)


def _plan_value(
    game: str,
    root: Path,
    players: tuple,
    cases: tuple[EloCase, ...],
    roles: tuple[str, ...],
    roles_symmetric: bool,
    degree: int,
    seed: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "game": game,
        "repository_root": str(root),
        "git_commit": _git_commit(root),
        "source_sha256": _source_hash(root),
        "degree": degree,
        "seed": seed,
        "roles": list(roles),
        "roles_symmetric": roles_symmetric,
        "players": [
            {
                "player_id": player.player_id,
                "package_root": str(player.package_root.resolve()),
                "anchor_elo": player.anchor_elo,
                "track": player.track,
            }
            for player in players
        ],
        "cases": [
            {
                "case_id": case.case_id,
                "player_a": case.player_a.player_id,
                "player_b": case.player_b.player_id,
                "player_ids_by_role": list(case.player_ids_by_role),
                "roles": list(case.roles),
                "seed": case.seed,
            }
            for case in cases
        ],
    }


def _plan_inputs(plan: dict[str, Any]) -> dict[str, Any]:
    """Return fields that determine the immutable Elo schedule."""

    return {key: value for key, value in plan.items() if key != "git_commit"}


def _configured_evaluator(game: str, build_root: Path, artifact_root: Path):
    root = repository_root()
    plugin = get_plugin(game)
    game_dir = root / "games" / game
    default_evaluator = plugin.evaluator_factory(game_dir)
    evaluator_type = type(default_evaluator)
    parameters = inspect.signature(evaluator_type).parameters
    supported = {
        "build_root": build_root,
        "artifact_root": artifact_root,
        "timeout_s": 1800.0,
    }
    options = {name: value for name, value in supported.items() if name in parameters}
    if options:
        return evaluator_type(game_dir, **options)
    return default_evaluator


def _evaluate_case(
    game: str,
    case: EloCase,
    build_root: Path,
    artifact_root: Path,
) -> MatchRecord:
    """Evaluate one case; every worker-side exception is infrastructure failure."""

    try:
        evaluator = _configured_evaluator(game, build_root, artifact_root)
        packages = {
            case.player_a.player_id: case.player_a.package_root,
            case.player_b.player_id: case.player_b.package_root,
        }
        player_refs = [
            PlayerRef(player_id, str(packages[player_id]))
            for player_id in case.player_ids_by_role
        ]
        result = evaluator.evaluate(player_refs, list(case.roles), case.seed)
        status = result.status.value
        if status not in {
            EvaluationStatus.COMPLETE.value,
            EvaluationStatus.GAME_ERROR.value,
            EvaluationStatus.INFRA_ERROR.value,
        }:
            raise ValueError(f"unsupported evaluator status: {status!r}")
        if status == EvaluationStatus.INFRA_ERROR.value:
            return MatchRecord(
                case.case_id,
                status,
                None,
                None,
                result.diagnostic,
                result.replay_path,
                result.scores,
                result.rounds,
            )
        official_draw = (result.winner is None
            and result.payload.get('official_draw') is True
            and all(result.scores.get(role) == 0.5 for role in case.roles))
        if status == EvaluationStatus.GAME_ERROR.value and result.winner is None and not official_draw:
            detail = result.diagnostic or "evaluator returned no diagnostic"
            return MatchRecord(
                case.case_id,
                EvaluationStatus.INFRA_ERROR.value,
                None,
                None,
                f"game_error missing official winner: {detail}",
                result.replay_path,
                result.scores,
                result.rounds,
            )

        winner_player_id: str | None = None
        score_a = 0.5
        if result.winner is not None:
            try:
                winner_index = case.roles.index(result.winner)
            except ValueError as exc:
                raise ValueError(f"winner is not a planned role: {result.winner!r}") from exc
            winner_player_id = case.player_ids_by_role[winner_index]
            score_a = 1.0 if winner_player_id == case.player_a.player_id else 0.0
        return MatchRecord(
            case.case_id,
            status,
            winner_player_id,
            score_a,
            result.diagnostic,
            result.replay_path,
            result.scores,
            result.rounds,
        )
    except Exception as exc:
        return MatchRecord(
            case.case_id,
            EvaluationStatus.INFRA_ERROR.value,
            None,
            None,
            diagnostic=f"{type(exc).__name__}: {exc}",
        )


def _read_record(path: Path, expected_case_id: str) -> MatchRecord:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        record = MatchRecord(**value)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"invalid Elo match record: {path}") from exc
    if record.case_id != expected_case_id:
        raise ValueError(f"Elo match record case mismatch: {path}")
    if record.status not in {
        EvaluationStatus.COMPLETE.value,
        EvaluationStatus.GAME_ERROR.value,
        EvaluationStatus.INFRA_ERROR.value,
    }:
        raise ValueError(f"invalid Elo match status in {path}: {record.status!r}")
    return record


def _write_reports(
    *,
    game: str,
    work_dir: Path,
    degree: int,
    players: tuple,
    cases: tuple[EloCase, ...],
    records: dict[str, MatchRecord],
) -> EloRunSummary:
    valid_statuses = {
        EvaluationStatus.COMPLETE.value,
        EvaluationStatus.GAME_ERROR.value,
    }
    valid_records = [
        records[case.case_id]
        for case in cases
        if case.case_id in records and records[case.case_id].status in valid_statuses
    ]
    case_by_id = {case.case_id: case for case in cases}
    observations = [
        (
            case_by_id[record.case_id].player_a.player_id,
            case_by_id[record.case_id].player_b.player_id,
            float(record.score_a),
        )
        for record in valid_records
        if record.score_a is not None
    ]
    ratings = fit_ratings(players, observations)
    game_errors = sum(
        record.status == EvaluationStatus.GAME_ERROR.value for record in valid_records
    )
    infra_errors = sum(
        record.status == EvaluationStatus.INFRA_ERROR.value for record in records.values()
    )
    summary = EloRunSummary(
        game=game,
        planned_matches=len(cases),
        valid_matches=len(valid_records),
        game_errors=game_errors,
        infra_errors=infra_errors,
        pending_matches=len(cases) - len(valid_records),
        complete=len(valid_records) == len(cases),
        work_dir=work_dir,
    )
    generated_at = datetime.now(UTC).isoformat()
    rating_values = [asdict(rating) for rating in ratings]
    _atomic_write_json(work_dir / "measured_elo.json", rating_values)
    _atomic_write_json(
        work_dir / "summary.json",
        {
            "game": game,
            "method": "Bradley-Terry MLE with one neutral-anchor prior per player",
            "degree": degree,
            "players": len(players),
            "planned_matches": summary.planned_matches,
            "valid_matches": summary.valid_matches,
            "game_errors": summary.game_errors,
            "infra_errors": summary.infra_errors,
            "pending_matches": summary.pending_matches,
            "complete": summary.complete,
            "ratings": rating_values,
            "generated_at": generated_at,
        },
    )
    _write_ranking_tsv(work_dir / "measured_ranking.tsv", ratings)
    return summary


def _write_ranking_tsv(path: Path, ratings: tuple[PlayerRating, ...]) -> None:
    columns = (
        "rank",
        "player_id",
        "measured_elo",
        "matches",
        "points",
        "winrate",
        "anchor_elo",
        "track",
    )
    lines = ["\t".join(columns)]
    for rank, rating in enumerate(ratings, start=1):
        lines.append(
            "\t".join(
                (
                    str(rank),
                    rating.player_id,
                    str(rating.measured_elo),
                    str(rating.matches),
                    str(rating.points),
                    "" if rating.winrate is None else str(rating.winrate),
                    "" if rating.anchor_elo is None else str(rating.anchor_elo),
                    "" if rating.track is None else rating.track,
                )
            )
        )
    _atomic_write_text(path, "\n".join(lines) + "\n")


def run_elo(
    game: str,
    work_dir: Path,
    *,
    workers: int | None = None,
    degree: int = 24,
    seed: int = 7,
    max_matches: int | None = None,
) -> EloRunSummary:
    """Run or resume a native Elo schedule inside this checkout's ``runs/elo``."""

    root = repository_root()
    allowed_root = (root / "runs" / "elo").resolve()
    resolved_work = Path(work_dir).resolve()
    try:
        relative_work = resolved_work.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError(f"work_dir must be inside {allowed_root}") from exc
    if relative_work == Path("."):
        raise ValueError(f"work_dir must be a child of {allowed_root}")
    if max_matches is not None and max_matches < 0:
        raise ValueError("max_matches cannot be negative")
    worker_count = workers
    if worker_count is None:
        worker_count = max(1, ((os.cpu_count() or 1) - 2) // 2)
    if worker_count < 1:
        raise ValueError("workers must be positive")

    plugin = get_plugin(game)
    roles = tuple(plugin.roles)
    loaded_players = load_verified_players(game)
    cases = build_cases(loaded_players, roles, plugin.roles_symmetric, degree, seed)
    scheduled_player_ids = {
        player_id for case in cases for player_id in case.player_ids_by_role
    }
    players = tuple(
        sorted(
            (
                player
                for player in loaded_players
                if player.player_id in scheduled_player_ids
            ),
            key=lambda player: player.player_id,
        )
    )
    plan = _plan_value(
        game,
        root,
        players,
        cases,
        roles,
        plugin.roles_symmetric,
        degree,
        seed,
    )

    resolved_work.mkdir(parents=True, exist_ok=True)
    plan_path = resolved_work / "plan.json"
    if plan_path.exists():
        try:
            existing_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid existing Elo plan: {plan_path}") from exc
        if _plan_inputs(existing_plan) != _plan_inputs(plan):
            raise ValueError("existing Elo plan does not match current repository inputs")
    else:
        _atomic_write_json(plan_path, plan)

    matches_dir = resolved_work / "matches"
    records: dict[str, MatchRecord] = {}
    pending: list[EloCase] = []
    for case in cases:
        record_path = matches_dir / f"{case.case_id}.json"
        if record_path.exists():
            record = _read_record(record_path, case.case_id)
            records[case.case_id] = record
            if record.status == EvaluationStatus.INFRA_ERROR.value:
                pending.append(case)
        else:
            pending.append(case)

    if max_matches is not None:
        pending = pending[:max_matches]
    if pending:
        build_root = resolved_work / "build"
        artifact_base = resolved_work / "artifacts"
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    _evaluate_case,
                    game,
                    case,
                    build_root,
                    artifact_base / case.case_id,
                ): case
                for case in pending
            }
            for future in as_completed(futures):
                case = futures[future]
                try:
                    record = future.result()
                except Exception as exc:  # process-pool failures are infrastructure failures
                    record = MatchRecord(
                        case.case_id,
                        EvaluationStatus.INFRA_ERROR.value,
                        None,
                        None,
                        diagnostic=f"{type(exc).__name__}: {exc}",
                    )
                if record.case_id != case.case_id:
                    record = MatchRecord(
                        case.case_id,
                        EvaluationStatus.INFRA_ERROR.value,
                        None,
                        None,
                        diagnostic="worker returned a mismatched case ID",
                    )
                records[case.case_id] = record
                _atomic_write_json(matches_dir / f"{case.case_id}.json", asdict(record))

    return _write_reports(
        game=game,
        work_dir=resolved_work,
        degree=degree,
        players=players,
        cases=cases,
        records=records,
    )

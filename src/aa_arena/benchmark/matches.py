"""Budget-independent match execution through the shared Saiblo evaluator."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from copy import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from aa_arena.core import EvaluationStatus, PlayerRef
from aa_arena.benchmark.distribution import scope
from aa_arena.benchmark.experiment import binary_small_result
from aa_arena.core.cpp_build import build_player, has_cpp_build
from aa_arena.core.player_entry import find_python_entry
from aa_arena.core.registry import get_plugin
from aa_arena.elo.model import repository_root
from aa_arena.io import atomic_write_json, sha256_file
from aa_arena.replay import narrate
from aa_arena.replay.public_json import MAX_SOURCE_REPLAY_BYTES, compact_replay


class MatchInfrastructureError(RuntimeError):
    """One or more Saiblo matches remained infrastructure failures after retries."""


@dataclass(frozen=True)
class Opponent:
    opponent_id: str
    elo: float
    rank: int
    package_root: Path
    track: str | None = None
    reference_rank: int | None = None


@dataclass(frozen=True)
class SeatResult:
    opponent_id: str
    candidate_roles: tuple[str, ...]
    status: str
    outcome: str | None
    score: float | None
    winner: str | None
    rounds: int | None
    diagnostic: str | None
    replay_path: str | None
    attempts: int
    seed: int
    failed_roles: tuple[str, ...] = ()


def load_opponents(game: str, root: Path | None = None) -> tuple[Opponent, ...]:
    repository = (root or repository_root()).resolve()
    path = repository / "results" / "elo" / game / "measured_elo.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value.get("ratings") if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise ValueError(f"{path}: ratings must be a list")
    pool = repository / "games" / game / "players" / "pool"
    opponents = []
    for rank, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        opponent_id = row.get("player_id")
        if not isinstance(opponent_id, str):
            continue
        package = (pool / opponent_id).resolve()
        if package.parent != pool.resolve() or not package.is_dir():
            continue
        opponents.append(
            Opponent(
                opponent_id,
                float(row["measured_elo"]),
                rank,
                package,
                str(row["track"]) if row.get("track") is not None else None,
            )
        )
    from aa_arena.benchmark.distribution import local_subset
    if local_subset(repository):
        opponents = [replace(row, rank=i, reference_rank=row.rank)
                     for i, row in enumerate(opponents, 1)]
    return tuple(opponents)


def freeze_opponents(
    game: str,
    repository: Path,
    path: Path,
    *,
    leaderboard_path: Path | None = None,
) -> tuple[Opponent, ...]:
    """Create or reload the immutable verified-pool scale for one benchmark run."""

    repository = Path(repository).resolve()
    pool = repository / "games" / game / "players" / "pool"
    path = Path(path).resolve()
    if path.is_file():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("game") != game:
            raise ValueError(f"invalid frozen opponent pool: {path}")
        rows = value.get("opponents")
        if not isinstance(rows, list):
            raise ValueError(f"invalid frozen opponent rows: {path}")
        opponents = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"invalid frozen opponent row: {path}")
            opponent_id = str(row["opponent_id"])
            package = (pool / opponent_id).resolve()
            if package.parent != pool.resolve() or not package.is_dir():
                raise ValueError(f"frozen opponent package is unavailable: {opponent_id}")
            opponents.append(
                Opponent(
                    opponent_id=opponent_id,
                    elo=float(row["elo"]),
                    rank=int(row["rank"]),
                    reference_rank=row.get("reference_rank"),
                    package_root=package,
                    track=str(row["track"]) if row.get("track") is not None else None,
                )
            )
        if len({row.opponent_id for row in opponents}) != len(opponents):
            raise ValueError(f"duplicate ids in frozen opponent pool: {path}")
        return tuple(opponents)

    if leaderboard_path is None:
        opponents = load_opponents(game, repository)
    else:
        leaderboard = json.loads(Path(leaderboard_path).read_text(encoding="utf-8"))
        rows = leaderboard.get("opponents") if isinstance(leaderboard, dict) else None
        if not isinstance(rows, list):
            raise ValueError(f"invalid public leaderboard: {leaderboard_path}")
        opponents = tuple(
            Opponent(
                opponent_id=str(row["opponent_id"]),
                elo=float(row["elo"]),
                rank=int(row["rank"]),
                reference_rank=row.get("reference_rank"),
                package_root=(pool / str(row["opponent_id"])).resolve(),
                track=str(row["track"]) if row.get("track") is not None else None,
            )
            for row in rows
            if isinstance(row, dict)
        )
        if any(
            row.package_root.parent != pool.resolve() or not row.package_root.is_dir()
            for row in opponents
        ):
            raise ValueError("public leaderboard references an unavailable opponent package")
    if not opponents or len({row.opponent_id for row in opponents}) != len(opponents):
        raise ValueError("verified opponent pool must be non-empty with unique ids")
    neutral_anchor_elo = math.fsum(row.elo for row in opponents) / len(opponents)
    atomic_write_json(
        path,
        {
            "schema_version": 2,
            "game": game,
            "elo_method": "fixed-pool Bradley-Terry MLE with one neutral-anchor prior",
            "neutral_anchor_elo": neutral_anchor_elo,
            "leaderboard_sha256": (
                sha256_file(Path(leaderboard_path)) if leaderboard_path is not None else None
            ),
            "opponents": [
                {
                    "opponent_id": row.opponent_id,
                    "elo": row.elo,
                    "rank": row.rank,
                    "reference_rank": row.reference_rank,
                    "track": row.track,
                }
                for row in opponents
            ],
        },
    )
    path.chmod(0o600)
    return opponents


def _stable_seed(game: str, opponent_id: str, assignment: int, base_seed: int) -> int:
    digest = hashlib.sha256(
        f"{game}\0{opponent_id}\0{assignment}\0{base_seed}".encode()
    ).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _candidate_package(strategy_root: Path, roles: Iterable[str]) -> Path:
    role_list = tuple(roles)
    if len(role_list) == 1 and (strategy_root / role_list[0]).is_dir():
        return strategy_root / role_list[0]
    return strategy_root


def _configured_evaluator(game: str, artifact_root: Path, build_root: Path, root: Path):
    plugin = get_plugin(game, root / "games")
    game_dir = root / "games" / game
    default = plugin.evaluator_factory(game_dir)
    evaluator_type = type(default)
    parameters = inspect.signature(evaluator_type).parameters
    supported = {
        "build_root": build_root,
        "artifact_root": artifact_root,
        "timeout_s": 1800.0,
    }
    options = {name: value for name, value in supported.items() if name in parameters}
    return evaluator_type(game_dir, **options) if options else default


class MatchService:
    def __init__(
        self,
        game: str,
        run_root: Path,
        *,
        repository: Path | None = None,
        workers: int = 16,
        seed: int = 20260831,
        infrastructure_retries: int = 2,
        pool_snapshot: Path | None = None,
        pool_leaderboard: Path | None = None,
        practice: bool = False,
    ) -> None:
        self.game = game
        self.run_root = Path(run_root).resolve()
        self.repository = (repository or repository_root()).resolve()
        self.plugin = get_plugin(game, self.repository / "games")
        self.roles = tuple(self.plugin.roles)
        self.opponents = (
            freeze_opponents(
                game,
                self.repository,
                pool_snapshot,
                leaderboard_path=pool_leaderboard,
            )
            if pool_snapshot is not None
            else load_opponents(game, self.repository)
        )
        self.by_id = {opponent.opponent_id: opponent for opponent in self.opponents}
        self.workers = max(1, int(workers))
        self.seed = int(seed)
        self.infrastructure_retries = max(0, int(infrastructure_retries))
        self.hidden_root = self.run_root / "controller" / "matches"
        self.build_root = self.run_root / "controller" / "build"

    def preflight_candidate(self, strategy_root: Path) -> None:
        """Build/type-check the candidate before a budget reservation.

        This deliberately does not execute candidate code. Protocol/runtime
        failures remain legitimate charged match outcomes, while missing entry
        points and compiler/syntax failures are rejected for free.
        """

        roots = [
            _candidate_package(Path(strategy_root), (role,)) for role in self.roles
        ] or [Path(strategy_root)]
        evaluator = _configured_evaluator(
            self.game, self.hidden_root, self.build_root, self.repository
        )
        game_preflight = getattr(evaluator, 'preflight_candidate', None)
        for root in dict.fromkeys(path.resolve() for path in roots):
            if game_preflight is not None:
                game_preflight(root)
                continue
            if has_cpp_build(root):
                build_player(root, self.build_root)
                continue
            entry = find_python_entry(root)
            if entry is None:
                raise ValueError(f"candidate has no runnable Python or C++ entry: {root}")
            sources = sorted(entry.rglob("*.py"))
            if not sources:
                raise ValueError(f"candidate Python package contains no sources: {entry}")
            try:
                for source in sources:
                    compile(
                        source.read_text(encoding="utf-8"),
                        str(source.relative_to(entry)),
                        "exec",
                    )
            except (OSError, SyntaxError, UnicodeError) as exc:
                raise ValueError(f"candidate Python preflight failed: {exc}") from exc

    def validate_opponents(
        self, opponent_ids: Iterable[str], *, allow_repeats: bool = False
    ) -> tuple[Opponent, ...]:
        ids = tuple(opponent_ids)
        if not ids:
            raise ValueError("small_match requires at least one opponent")
        if not allow_repeats and len(set(ids)) != len(ids):
            raise ValueError("small_match opponent IDs must be unique")
        unknown = sorted(set(ids) - set(self.by_id))
        if unknown:
            raise ValueError(f"unknown verified opponents: {unknown}")
        return tuple(self.by_id[item] for item in ids)

    def _assignments(self, opponent: Opponent) -> tuple[tuple[str, ...], ...]:
        if len(self.roles) == 2 and opponent.track in self.roles:
            # Role-specific pools (Rollman): that package is only valid in its track.
            return (tuple(role for role in self.roles if role != opponent.track),)
        if len(self.roles) == 2:
            return ((self.roles[0],), (self.roles[1],))
        # Keep the native Elo policy for LostSpace: A vs B... and B vs A....
        return ((self.roles[0],), tuple(self.roles[1:]))

    def _evaluate_seat(self, strategy_root, opponent, candidate_roles, assignment_index, submission_id):
        from aa_arena.benchmark.admission import admission
        with admission("seat", detail=f"{self.game}:{submission_id}"):
            return self._evaluate_seat_unadmitted(
                strategy_root, opponent, candidate_roles, assignment_index, submission_id)

    def _evaluate_seat_unadmitted(
        self,
        strategy_root: Path,
        opponent: Opponent,
        candidate_roles: tuple[str, ...],
        assignment_index: int,
        submission_id: str,
    ) -> SeatResult:
        role_owners = ["candidate" if role in candidate_roles else "opponent" for role in self.roles]
        candidate_package = _candidate_package(strategy_root, candidate_roles)
        players = [
            PlayerRef(
                "candidate" if owner == "candidate" else opponent.opponent_id,
                str(candidate_package if owner == "candidate" else opponent.package_root),
            )
            for owner in role_owners
        ]
        seed = _stable_seed(self.game, opponent.opponent_id, assignment_index, self.seed)
        artifact = self.hidden_root / submission_id / opponent.opponent_id / f"seat-{assignment_index}"
        attempts = 0
        result = None
        while attempts <= self.infrastructure_retries:
            attempts += 1
            evaluator = _configured_evaluator(self.game, artifact, self.build_root, self.repository)
            result = evaluator.evaluate(players, list(self.roles), seed)
            # Some official adapters omit failed_roles from adjudicated
            # forfeits. Attribute them from transport evidence, never from who won.
            if (result.status is EvaluationStatus.GAME_ERROR
                    and not result.payload.get("failed_roles")
                    and result.replay_path and set(self.roles) == {"P0", "P1"}):
                from aa_arena.saiblo.player_errors import transport_player_errors
                failed_roles, _ = transport_player_errors(
                    Path(result.replay_path).with_name("transport-events.jsonl"))
                if failed_roles:
                    result = replace(result, payload={**result.payload, "failed_roles": failed_roles})
            official_draw = (result.winner is None
                and result.payload.get('official_draw') is True
                and all(result.scores.get(role) == 0.5 for role in self.roles))
            if result.status is EvaluationStatus.GAME_ERROR and result.winner not in self.roles and not official_draw:
                result = replace(result, status=EvaluationStatus.INFRA_ERROR,
                    diagnostic=result.diagnostic or 'game error without an adjudicated winner')
            if result.status is not EvaluationStatus.INFRA_ERROR:
                break
        assert result is not None
        if result.status is EvaluationStatus.INFRA_ERROR:
            return SeatResult(
                opponent.opponent_id,
                candidate_roles,
                result.status.value,
                None,
                None,
                result.winner,
                result.rounds,
                result.diagnostic,
                result.replay_path,
                attempts,
                seed,
            )
        if result.winner is None:
            score = 0.5
            outcome = "draw"
        elif result.winner in candidate_roles:
            score = 1.0
            outcome = "win"
        else:
            score = 0.0
            outcome = "loss"
        return SeatResult(
            opponent.opponent_id,
            candidate_roles,
            result.status.value,
            outcome,
            score,
            result.winner,
            result.rounds,
            result.diagnostic,
            result.replay_path,
            attempts,
            seed,
            tuple(result.payload.get("failed_roles", ())),
        )

    def _run(
        self,
        strategy_root: Path,
        opponents: tuple[Opponent, ...],
        submission_id: str,
    ) -> tuple[SeatResult, ...]:
        jobs = []
        occurrences: dict[str, int] = {}
        for opponent in opponents:
            occurrence = occurrences.get(opponent.opponent_id, 0)
            occurrences[opponent.opponent_id] = occurrence + 1
            # Repeated ladder opponents keep native seat seeds while getting
            # separate evaluator directories, even when evaluated concurrently.
            evaluation_id = (submission_id if occurrence == 0
                             else f"{submission_id}/repeat-{occurrence}")
            jobs.extend((opponent, roles, index, evaluation_id)
                        for index, roles in enumerate(self._assignments(opponent)))
        results: list[SeatResult] = []
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {
                executor.submit(
                    self._evaluate_seat,
                    strategy_root,
                    opponent,
                    candidate_roles,
                    index,
                    evaluation_id,
                ): (opponent.opponent_id, index)
                for opponent, candidate_roles, index, evaluation_id in jobs
            }
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda row: (row.opponent_id, row.candidate_roles))
        failed = [row for row in results if row.status == EvaluationStatus.INFRA_ERROR.value]
        if failed:
            detail = "; ".join(
                f"{row.opponent_id}/{','.join(row.candidate_roles)}: {row.diagnostic}"
                for row in failed[:5]
            )
            raise MatchInfrastructureError(
                f"{len(failed)} Saiblo infrastructure failures remain after retries: {detail}"
            )
        return tuple(results)

    @staticmethod
    def _aggregate(results: Iterable[SeatResult]) -> dict[str, int]:
        values = tuple(results)
        return {
            "wins": sum(row.outcome == "win" for row in values),
            "draws": sum(row.outcome == "draw" for row in values),
            "losses": sum(row.outcome == "loss" for row in values),
            "candidate_errors": sum(row.status == EvaluationStatus.GAME_ERROR.value
                and (not row.failed_roles or bool(set(row.failed_roles) & set(row.candidate_roles)))
                for row in values),
            "opponent_errors": sum(bool(set(row.failed_roles) - set(row.candidate_roles)) for row in values),
            "game_errors": sum(row.status == EvaluationStatus.GAME_ERROR.value for row in values),
            "infrastructure_retries": sum(max(row.attempts - 1, 0) for row in values),
        }

    def small_match(
        self,
        strategy_root: Path,
        opponent_ids: Iterable[str],
        submission_id: str,
        visible_replays: Path,
        *,
        feedback: str = "detailed",
        seed: int | None = None,
        allow_repeats: bool = False,
        redact_diagnostics: bool = False,
    ) -> dict[str, Any]:
        if feedback not in {"detailed", "binary"}:
            raise ValueError("feedback must be detailed or binary")
        opponents = self.validate_opponents(opponent_ids, allow_repeats=allow_repeats)
        runner = self
        if seed is not None:
            runner = copy(self)
            runner.seed = seed
        try:
            results = runner._run(Path(strategy_root), opponents, submission_id)
        except Exception:
            if feedback == "binary":
                raise MatchInfrastructureError("small match evaluation failed; retry pending request") from None
            raise
        if feedback == "binary":
            # Never render, compact, narrate or copy detailed evidence for this arm.
            return binary_small_result({
                "opponents": [opponent.opponent_id for opponent in opponents],
                "seats": [asdict(row) for row in results],
            })
        visible_root = Path(visible_replays) / submission_id
        seat_occurrences: dict[tuple[str, tuple[str, ...]], int] = {}
        serialized = []
        for index, row in enumerate(results):
            value = asdict(row)
            if redact_diagnostics:
                value["diagnostic"] = None
            value["opponent_rank"] = self.by_id[row.opponent_id].rank
            value["opponent_elo"] = self.by_id[row.opponent_id].elo
            if row.replay_path and Path(row.replay_path).is_file():
                role_name = "-".join(row.candidate_roles)
                seat_key = (row.opponent_id, row.candidate_roles)
                occurrence = seat_occurrences.get(seat_key, 0)
                seat_occurrences[seat_key] = occurrence + 1
                if occurrence:
                    role_name += f"-repeat-{occurrence}"
                destination = visible_root / row.opponent_id / f"{role_name}.json"
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = Path(row.replay_path)
                if source.stat().st_size <= MAX_SOURCE_REPLAY_BYTES:
                    narration = narrate(
                        self.game,
                        source,
                        match_id=f"{submission_id}-{index}",
                        roles=self.roles,
                        perspective=row.candidate_roles[0] if len(row.candidate_roles) == 1 else None,
                        opponent_id=row.opponent_id,
                        official_winner=row.winner,
                        official_rounds=row.rounds,
                        diagnostic="" if redact_diagnostics else row.diagnostic or "",
                        games_root=self.repository / "games",
                    )
                    compact_replay(self.game, source, destination)
                else:
                    # A renderer/debug dump must never enter the agent workspace.
                    # Keep a tiny, explicit record so the agent knows why this
                    # battle has no replay evidence instead of silently guessing.
                    destination.write_text(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "game": self.game,
                                "replay_omitted": True,
                                "reason": "private Saiblo replay exceeded the public size limit",
                                "source_size": source.stat().st_size,
                                "rounds": row.rounds,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    narration = narrate(
                        self.game,
                        Path("/nonexistent/private-replay"),
                        match_id=f"{submission_id}-{index}",
                        roles=self.roles,
                        perspective=row.candidate_roles[0] if len(row.candidate_roles) == 1 else None,
                        opponent_id=row.opponent_id,
                        official_winner=row.winner,
                        official_rounds=row.rounds,
                        diagnostic=("" if redact_diagnostics else row.diagnostic or "")
                        + f" private replay omitted at {source.stat().st_size} bytes",
                        games_root=self.repository / "games",
                    )
                markdown = destination.with_suffix(".md")
                markdown.write_text(narration.text, encoding="utf-8")
                value["replay_path"] = str(destination)
                value["replay_sha256"] = sha256_file(destination)
                value["narration_path"] = str(markdown)
            serialized.append(value)
        return {
            "kind": "small",
            **self._aggregate(results),
            "opponents": [opponent.opponent_id for opponent in opponents],
            "seats": serialized,
        }

    @staticmethod
    def _fit_fixed_candidate(
        results: tuple[SeatResult, ...], opponents: dict[str, Opponent]
    ) -> tuple[float, float, float, float]:
        observations = [(opponents[row.opponent_id].elo, float(row.score)) for row in results if row.score is not None]
        if not observations:
            raise ValueError("cannot fit candidate Elo without valid observations")
        if not opponents:
            raise ValueError("cannot fit candidate Elo without a frozen opponent pool")
        observed_anchors = [opponent_elo for opponent_elo, _ in observations]
        # The published pool fit normalizes geometric-mean strength to 1 before
        # applying its common historical-Elo offset. The corresponding neutral
        # point on the reported scale is therefore the arithmetic mean of the
        # complete fixed pool. Use the same one-draw neutral prior here.
        prior_anchor = math.fsum(row.elo for row in opponents.values()) / len(opponents)
        target = sum(score for _, score in observations) + 0.5

        def expected(candidate: float, opponent_elo: float) -> float:
            return 1.0 / (1.0 + 10.0 ** ((opponent_elo - candidate) / 400.0))

        low = min(*observed_anchors, prior_anchor) - 4000.0
        high = max(*observed_anchors, prior_anchor) + 4000.0
        for _ in range(100):
            middle = (low + high) / 2.0
            total = sum(expected(middle, elo) for elo, _ in observations)
            total += expected(middle, prior_anchor)
            if total < target:
                low = middle
            else:
                high = middle
        rating = (low + high) / 2.0
        scale = math.log(10.0) / 400.0
        probabilities = [expected(rating, elo) for elo, _ in observations]
        probabilities.append(expected(rating, prior_anchor))
        information = scale * scale * sum(p * (1.0 - p) for p in probabilities)
        standard_error = math.sqrt(1.0 / information) if information > 0 else 4000.0
        return (
            rating,
            rating - 1.96 * standard_error,
            rating + 1.96 * standard_error,
            prior_anchor,
        )

    def large_match(self, strategy_root: Path, submission_id: str) -> dict[str, Any]:
        results = self._run(Path(strategy_root), self.opponents, submission_id)
        rating, ci_low, ci_high, prior_anchor = self._fit_fixed_candidate(
            results, self.by_id
        )
        aggregate = self._aggregate(results)
        games = aggregate["wins"] + aggregate["draws"] + aggregate["losses"]
        per_opponent = []
        for opponent in self.opponents:
            selected = tuple(row for row in results if row.opponent_id == opponent.opponent_id)
            per_opponent.append(
                {
                    "opponent_id": opponent.opponent_id,
                    "rank": opponent.rank,
                    "elo": opponent.elo,
                    **self._aggregate(selected),
                }
            )
        return {
            "kind": "large",
            **scope(self.repository),
            **aggregate,
            "elo": round(rating, 3),
            "elo_ci_low": round(ci_low, 3),
            "elo_ci_high": round(ci_high, 3),
            "elo_method": "fixed-pool Bradley-Terry MLE with one neutral-anchor prior",
            "elo_prior_anchor": round(prior_anchor, 6),
            "rank": 1 + sum(opponent.elo > rating for opponent in self.opponents),
            "pool_win_rate": (aggregate["wins"] + 0.5 * aggregate["draws"]) / games,
            "games": games,
            "per_opponent": per_opponent,
        }

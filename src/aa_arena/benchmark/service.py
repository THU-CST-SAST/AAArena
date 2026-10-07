"""Transactional bridge between dynamic tools, snapshots, Saiblo, and reports."""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from aa_arena.benchmark.ledger import RunLedger
from aa_arena.benchmark.experiment import ExperimentConfig, binary_small_result
from aa_arena.benchmark.distribution import scope
from aa_arena.benchmark.matches import MatchInfrastructureError, MatchService
from aa_arena.benchmark.snapshot import Snapshot, SnapshotStore
from aa_arena.benchmark.pool_dense import (
    list_trajectories,
    load_catalog,
    materialize_view,
)
from aa_arena.benchmark.trajectory import TrajectoryLog, rebuild_report
from aa_arena import __version__
from aa_arena.io import atomic_write_json, canonical_hash, sha256_file
from aa_arena.resources import build_bundle


MIN_SMALL_BATCH = 1
MAX_SMALL_BATCH = 8
MIN_FREE_STORAGE_BYTES = 8 * 1024**3


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir() or path.name == "translate":
            path.chmod(0o555)
        else:
            path.chmod(0o444)
    root.chmod(0o555)


class BenchmarkService:
    def __init__(
        self,
        run_root: Path,
        *,
        game: str,
        model_profile: str,
        small_budget: int = 128,
        large_budget: int = 16,
        workers: int = 16,
        extend_budget_once: bool = False,
    ) -> None:
        self.run_root = Path(run_root).resolve()
        self.workspace = self.run_root / "workspace"
        self.controller = self.run_root / "controller"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.controller.mkdir(parents=True, exist_ok=True)
        self.experiment = ExperimentConfig.load(self.controller / "experiment.json")
        self.experiment.validate_budgets(small_budget, large_budget)
        if self.experiment.is_clone and extend_budget_once:
            raise ValueError("clone trials cannot extend their budget")
        self.experiment.freeze(self.controller / "experiment.json")
        self.ledger = RunLedger(self.controller / "ledger.sqlite3")
        if extend_budget_once:
            existing = self.ledger.state()
            if existing["game"] != game or existing["model_profile"] != model_profile:
                raise ValueError("cannot extend another game's ledger")
            self.snapshots = SnapshotStore(self.controller / "snapshots")
            if not existing["metadata"].get("budget_extension"):
                champion = existing["metadata"].get("champion", {})
                if existing["frozen_snapshot_id"] != champion.get("snapshot_id"):
                    raise ValueError("extension requires the frozen CHAMPION")
                frozen = self.snapshots.verify(existing["frozen_snapshot_id"])
                if frozen.strategy_hash != champion.get("strategy_hash"):
                    raise ValueError("extension CHAMPION hash mismatch")
            self.ledger.extend_budget_once(small=small_budget, large=large_budget)
            extended = self.ledger.state()
            small_budget = extended["small_total"]
            large_budget = extended["large_total"]
        self.run_id = self.ledger.initialize(
            game=game,
            model_profile=model_profile,
            small_budget=small_budget,
            large_budget=large_budget,
        )
        metadata = self.ledger.state()["metadata"]
        if metadata.get("experiment", self.experiment.as_dict()) != self.experiment.as_dict():
            raise ValueError("ledger experiment configuration changed")
        if "experiment" not in metadata:
            if self.ledger.submissions() and self.experiment != ExperimentConfig():
                raise ValueError("cannot apply an ablation to an existing baseline run")
            self.ledger.update_runtime(metadata={**metadata, "experiment": self.experiment.as_dict()})
        self._prepare_writable_workspace()
        self.game = game
        self.snapshots = SnapshotStore(self.controller / "snapshots")
        from aa_arena.benchmark.remote import public_distribution, RemoteMatchService
        self.remote = public_distribution()
        resources = self.workspace / "resources"
        if not resources.exists():
            staging = self.controller / "resource-staging"
            bundle = build_bundle(
                self.game,
                staging,
                include_replay=not self.experiment.binary_feedback,
                formal=self.remote,
            )
            shutil.copytree(bundle, resources)
            _make_read_only(resources)
        resource_scope = json.loads((resources / "leaderboard.json").read_text()).get("evaluation_scope")
        if self.remote and resource_scope != "full-pool":
            raise ValueError("Cannot resume a local-subset experiment as a formal remote run; use a fresh run directory")
        match_class = RemoteMatchService if self.remote else MatchService
        remote_options = dict(run_id=self.run_id, small_budget=small_budget, large_budget=large_budget, experiment=self.experiment.as_dict()) if self.remote else {}
        self.matches = match_class(
            game,
            self.run_root,
            workers=workers,
            seed=self.experiment.resolved_match_base_seed(),
            pool_snapshot=self.controller / "opponent-pool.json",
            pool_leaderboard=resources / "leaderboard.json",
            **remote_options,
        )
        self.trajectory = TrajectoryLog(self.run_root / "trajectory")
        self.pool_dense_catalog: dict[str, Any] | None = None
        if self.experiment.is_offpolicy:
            catalog_path = self.controller / "pool-dense-catalog.json"
            if not catalog_path.exists():
                raise FileNotFoundError(
                    "offpolicy run requires controller/pool-dense-catalog.json"
                )
            self.pool_dense_catalog = load_catalog(catalog_path)
            if self.pool_dense_catalog.get("game") != game:
                raise ValueError("offpolicy catalog game does not match the experiment")
        if self.experiment.is_clone:
            matches = [row for row in self.matches.opponents if row.rank == self.experiment.clone_rank]
            if len(matches) != 1:
                raise ValueError("clone_rank must identify exactly one frozen global-rank opponent")

    def _prepare_writable_workspace(self) -> None:
        if self.ledger.state()["status"] == "running":
            self.workspace.chmod((self.workspace.stat().st_mode & 0o777) | 0o700)
            for name in ("strategy", "skills", "notes", "replays", "artifacts"):
                writable = self.workspace / name
                if not writable.exists():
                    continue
                if writable.is_symlink() or not writable.is_dir():
                    raise ValueError("writable workspace root must be a real directory")
                for path in (writable, *writable.rglob("*")):
                    if not path.is_symlink():
                        path.chmod((path.stat().st_mode & 0o777) | (0o700 if path.is_dir() else 0o600))

    def initialize_workspace(self) -> None:
        resources = self.workspace / "resources"
        strategy = self.workspace / "strategy"
        for name in ("notes", "replays", "artifacts"):
            (self.workspace / name).mkdir(exist_ok=True)
        if not strategy.exists():
            sdk = resources / "sdk"
            if self.game == "rollman":
                strategy.mkdir()
                shutil.copytree(sdk / "rollman", strategy / "rollman")
                shutil.copytree(sdk / "ghost", strategy / "ghost")
            else:
                shutil.copytree(sdk, strategy)
        skills = self.workspace / "skills"
        skills.mkdir(exist_ok=True)
        if not self.experiment.binary_feedback:
            reading_skill = skills / "replay-reading.md"
            if not reading_skill.exists():
                source_skill = resources / "replay" / "reading_skill.md"
                if source_skill.is_file():
                    shutil.copy2(source_skill, reading_skill)
                else:
                    from aa_arena.resources import REPOSITORY_ROOT

                    shutil.copy2(
                        REPOSITORY_ROOT / "src" / "aa_arena" / "replay_reading_skill.md",
                        reading_skill,
                    )
        self._prepare_writable_workspace()
        manifest = json.loads((resources / "manifest.json").read_text(encoding="utf-8"))
        evaluator_root = self.matches.repository / "games" / self.game / "evaluator"
        evaluator_files = [
            {
                "path": path.relative_to(evaluator_root).as_posix(),
                "sha256": sha256_file(path),
            }
            for path in sorted(evaluator_root.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        ]
        evaluator = self.matches.plugin.evaluator_factory(
            self.matches.repository / "games" / self.game
        )
        self.ledger.update_runtime(
            metadata={
                **scope(formal=self.remote),
                **self.ledger.state().get("metadata", {}),
                "aa_arena_version": __version__,
                "game": self.game,
                "resource_bundle_sha256": manifest["bundle_sha256"],
                "leaderboard_sha256": sha256_file(resources / "leaderboard.json"),
                "verified_pool_sha256": sha256_file(self.controller / "opponent-pool.json"),
                "saiblo_evaluator_sha256": canonical_hash(evaluator_files),
                "saiblo_evaluator_class": (
                    f"{type(evaluator).__module__}.{type(evaluator).__qualname__}"
                ),
            }
        )
        self.reconcile_trajectory()

    def reconcile_trajectory(self) -> int:
        """Backfill committed matches after a controller crash before JSONL fsync."""

        known = {
            str(event.get("submission_id"))
            for event in self.trajectory.read()
            if event.get("submission_id")
        }
        state = self.ledger.state()
        small_used = 0
        large_used = 0
        added = 0
        for row in self.ledger.submissions():
            if row["kind"] == "small":
                small_used += int(row["cost"])
            elif row["kind"] == "large":
                large_used += int(row["cost"])
            if row["status"] != "complete" or row["submission_id"] in known:
                continue
            snapshot = self.snapshots.verify(str(row["snapshot_id"]))
            completed_at = float(row["completed_at"] or row["created_at"])
            event = {
                "schema_version": 1,
                "run_id": self.run_id,
                "submission_id": row["submission_id"],
                "sequence": row["sequence"],
                "kind": row["kind"],
                "status": "complete",
                "snapshot": self._snapshot_record(snapshot),
                "active_seconds": row["active_seconds"],
                "wall_seconds": completed_at - float(state["created_at"]),
                "evaluation_seconds": completed_at - float(row["created_at"]),
                "token_usage": row["token_usage"],
                "budgets": {
                    "small_total": state["small_total"],
                    "small_used": small_used,
                    "small_remaining": state["small_total"] - small_used,
                    "large_total": state["large_total"],
                    "large_used": large_used,
                    "large_remaining": state["large_total"] - large_used,
                },
                "result": self._public_result(row["kind"], row["result"]),
                "thread_id": state.get("thread_id"),
                "turn_id": state.get("current_turn_id"),
                "goal": state.get("goal"),
                "metadata": state.get("metadata"),
                "created_at": completed_at,
                "recovered_event": True,
            }
            self.trajectory.append(event)
            if row["kind"] == "baseline":
                (self.workspace / "artifacts" / "baseline.json").write_text(
                    json.dumps(row["result"], ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                self.ledger.mark_delivered(str(row["submission_id"]))
            added += 1
        if added:
            rebuild_report(self.run_root)
        return added

    def _ensure_storage_headroom(self) -> None:
        free = shutil.disk_usage(self.run_root).free
        if free < MIN_FREE_STORAGE_BYTES:
            gib = free / 1024**3
            raise RuntimeError(
                "benchmark requires at least 8 GiB free before reserving match budget; "
                f"found {gib:.2f} GiB"
            )

    def _preflight(self) -> None:
        self._ensure_storage_headroom()
        strategy = self.workspace / "strategy"
        if not strategy.is_dir():
            raise ValueError("strategy directory is missing")
        entry_names = {"main.py", "Makefile", "makefile", "CMakeLists.txt"}
        if not any(path.name in entry_names for path in strategy.rglob("*")):
            raise ValueError("strategy has no main.py, Makefile, makefile, or CMakeLists.txt")
        self.matches.preflight_candidate(strategy)

    def _snapshot(self) -> Snapshot:
        return self.snapshots.create(
            self.workspace,
            parent_snapshot_id=self.ledger.latest_snapshot_id(),
            include_roots=("strategy", "skills", "notes", "replays", "artifacts"),
        )

    @staticmethod
    def _official_better(candidate: dict[str, Any], champion: dict[str, Any]) -> bool:
        candidate_rank = int(candidate.get("rank") or 10**9)
        champion_rank = int(champion.get("rank") or 10**9)
        if candidate_rank != champion_rank:
            return candidate_rank < champion_rank
        return float(candidate["elo"] if candidate.get("elo") is not None else float("-inf")) > float(
            champion["elo"] if champion.get("elo") is not None else float("-inf")
        )

    def _official_policy_state(self) -> tuple[dict[str, Any] | None, int]:
        champion: dict[str, Any] | None = None
        misses = 0
        for row in self.ledger.submissions():
            if row["status"] != "complete" or row["kind"] not in {"baseline", "large"}:
                continue
            result = row.get("result") or {}
            candidate = {
                "snapshot_id": row["snapshot_id"],
                "match_id": row["submission_id"],
                "kind": row["kind"],
                "rank": result.get("rank"),
                "elo": result.get("elo"),
                "strategy_hash": self.snapshots.get(str(row["snapshot_id"])).strategy_hash,
            }
            if champion is None or self._official_better(candidate, champion):
                champion = candidate
                misses = 0
            elif row["kind"] == "large":
                misses += 1
        return champion, misses

    def _restore_strategy(self, snapshot_id: str) -> None:
        manifest = self.snapshots.load_manifest(snapshot_id)
        assert manifest is not None
        with tempfile.TemporaryDirectory(dir=self.controller, prefix="champion-restore-") as raw:
            restored = self.snapshots.materialize(snapshot_id, Path(raw)) / "strategy"
            if not restored.is_dir():
                raise ValueError(f"champion snapshot has no strategy: {snapshot_id}")
            destination = self.workspace / "strategy"
            # Snapshot materialization is read-only. Restoring a champion must
            # also work as an ordinary user and after an interrupted final freeze.
            workspace_mode = self.workspace.stat().st_mode & 0o777
            self.workspace.chmod(workspace_mode | 0o700)
            if destination.exists():
                for path in (destination, *destination.rglob("*")):
                    if path.is_dir() and not path.is_symlink():
                        path.chmod((path.stat().st_mode & 0o777) | 0o700)
                shutil.rmtree(destination)
            try:
                shutil.copytree(restored, destination)
                for path in (destination, *destination.rglob("*")):
                    if path.is_dir() and not path.is_symlink():
                        path.chmod((path.stat().st_mode & 0o777) | 0o700)
            finally:
                self.workspace.chmod(workspace_mode)
            for row in manifest.get("files", []):
                if not isinstance(row, dict) or not str(row.get("path", "")).startswith(
                    "strategy/"
                ):
                    continue
                path = self.workspace / str(row["path"])
                if path.is_file():
                    path.chmod(int(row["mode"]))

    def restore_frozen_strategy(self) -> str:
        """Materialize the ledger-selected final snapshot into the workspace."""

        snapshot_id = self.ledger.state().get("frozen_snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ValueError("run has no frozen official strategy")
        self._restore_strategy(snapshot_id)
        return snapshot_id

    def _annotate_official_result(
        self, kind: str, snapshot: Snapshot, submission_id: str, result: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        if kind not in {"baseline", "large"}:
            return result, False
        champion, misses = self._official_policy_state()
        candidate = {
            "snapshot_id": snapshot.snapshot_id,
            "match_id": submission_id,
            "kind": kind,
            "rank": result.get("rank"),
            "elo": result.get("elo"),
            "strategy_hash": snapshot.strategy_hash,
        }
        improved = champion is None or self._official_better(candidate, champion)
        if improved:
            champion = candidate
            misses = 0
        elif kind == "large":
            misses += 1
        assert champion is not None
        budgets = self.ledger.budgets()
        should_stop = (
            kind == "large"
            and int(champion.get("rank") or 10**9) == 1
            and budgets.large_remaining > 0
            and not self.ledger.state()["metadata"].get("budget_extension")
            and self.experiment.seed != 20260922
        )
        metadata = dict(self.ledger.state()["metadata"])
        metadata.update(
            champion=champion,
            non_improving_large_streak=misses,
        )
        self.ledger.update_runtime(metadata=metadata)
        return {
            **result,
            "champion": champion,
            "non_improving_large_streak": misses,
            "stopped_early": should_stop,
        }, should_stop

    @staticmethod
    def _snapshot_record(snapshot: Snapshot) -> dict[str, Any]:
        return {
            "snapshot_id": snapshot.snapshot_id,
            "content_id": snapshot.content_id,
            "parent_snapshot_id": snapshot.parent_snapshot_id,
            "strategy_hash": snapshot.strategy_hash,
            "changed_files": snapshot.changed_files,
            "lines_added": snapshot.lines_added,
            "lines_deleted": snapshot.lines_deleted,
            "source_lines": snapshot.source_lines,
            "archive_sha256": snapshot.archive_sha256,
            "manifest_sha256": snapshot.manifest_sha256,
        }

    def _materialized_strategy(self, snapshot: Snapshot, submission_id: str) -> Path:
        destination = self.controller / "evaluation" / submission_id
        if destination.exists():
            shutil.rmtree(destination)
        self.snapshots.materialize(snapshot.snapshot_id, destination)
        return destination / "strategy"

    def _cleanup_submission_storage(self, submission_id: str) -> None:
        for path in (
            self.controller / "evaluation" / submission_id,
            self.matches.hidden_root / submission_id,
            self.controller / "binary-replays" / submission_id,
        ):
            shutil.rmtree(path, ignore_errors=True)

    def _event(
        self,
        *,
        submission_id: str,
        sequence: int,
        kind: str,
        snapshot: Snapshot,
        result: dict[str, Any],
        started_at: float,
    ) -> dict[str, Any]:
        state = self.ledger.state()
        budgets = self.ledger.budgets()
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "submission_id": submission_id,
            "sequence": sequence,
            "kind": kind,
            "status": "complete",
            "snapshot": self._snapshot_record(snapshot),
            "active_seconds": state["active_seconds"],
            "wall_seconds": time.time() - float(state["created_at"]),
            "evaluation_seconds": time.time() - started_at,
            "token_usage": state["token_usage"],
            "budgets": budgets.as_dict(),
            "result": result,
            "thread_id": state.get("thread_id"),
            "turn_id": state.get("current_turn_id"),
            "goal": state.get("goal"),
            "metadata": state.get("metadata"),
            "created_at": time.time(),
        }

    def _execute_pending(
        self,
        pending: dict[str, Any],
        snapshot: Snapshot,
    ) -> dict[str, Any]:
        submission_id = str(pending["submission_id"])
        kind = str(pending["kind"])
        request = pending["request"]
        if self.experiment.is_clone and kind != "small":
            raise ValueError("clone trials prohibit baseline and large evaluations")
        self._cleanup_submission_storage(submission_id)
        strategy = self._materialized_strategy(snapshot, submission_id)
        try:
            if kind == "baseline" and hasattr(self.matches, "baseline_match"):
                return self.matches.baseline_match(strategy, submission_id)
            if kind in {"baseline", "large"}:
                return self.matches.large_match(strategy, submission_id)
            if kind == "small" and request.get("trajectory_view"):
                if not self.experiment.is_offpolicy or self.pool_dense_catalog is None:
                    raise ValueError("trajectory views require offpolicy experiment")
                tid = str(request["trajectory_id"])
                return materialize_view(
                    self.pool_dense_catalog,
                    tid,
                    self.workspace / "replays",
                )
            if kind == "small":
                if self.experiment.is_offpolicy:
                    raise ValueError("offpolicy prohibits small_match; use view_dense_trajectory")
                options: dict[str, Any] = {}
                if self.experiment.binary_feedback:
                    options["feedback"] = "binary"
                if self.experiment.is_clone:
                    options["seed"] = request["seed"]
                if self.experiment.opponent_policy == "ladder":
                    options["allow_repeats"] = True
                return self.matches.small_match(
                    strategy,
                    request["opponent_ids"],
                    submission_id,
                    (self.controller / "binary-replays"
                     if self.experiment.binary_feedback else self.workspace / "replays"),
                    **options,
                )
            raise ValueError(f"unsupported pending submission kind: {kind}")
        except Exception:
            if kind == "small" and self.experiment.binary_feedback:
                raise MatchInfrastructureError(
                    "small match evaluation failed; accepted request remains pending recovery"
                ) from None
            raise
        finally:
            self._cleanup_submission_storage(submission_id)

    def recover_pending(self) -> dict[str, Any] | None:
        """Resume one accepted immutable task without charging it again."""

        pending = self.ledger.pending_submission()
        if pending is None:
            return None
        snapshot = self.snapshots.verify(str(pending["snapshot_id"]))
        started_at = time.time()
        result = self._execute_pending(pending, snapshot)
        return self._finish(
            str(pending["submission_id"]),
            int(pending["sequence"]),
            str(pending["kind"]),
            snapshot,
            result,
            started_at,
        )

    def _resume_matching_pending(self, kind: str, request: dict[str, Any]) -> dict[str, Any] | None:
        pending = self.ledger.pending_submission()
        if pending is None:
            return None
        if pending["kind"] != kind or pending["request"] != request:
            raise ValueError(
                "an accepted match is pending recovery; retry that exact request or restart the controller"
            )
        return self.recover_pending()

    def _public_result(
        self, kind: str, result: dict[str, Any], *, bookkeeping: bool = True
    ) -> dict[str, Any]:
        if kind != "small" or not self.experiment.binary_feedback:
            return result
        clean = binary_small_result(result)
        if bookkeeping:
            for key in ("match_id", "snapshot_id"):
                if isinstance(result.get(key), str):
                    clean[key] = result[key]
            for key, fields in (
                ("budget", ("small_total", "small_used", "small_remaining", "small_expired",
                            "large_total", "large_used", "large_remaining", "large_expired")),
                ("snapshot_integrity", ("content_id", "strategy_hash", "archive_sha256",
                                        "manifest_sha256")),
            ):
                if isinstance(result.get(key), dict):
                    clean[key] = {field: result[key][field] for field in fields
                                  if field in result[key]
                                  and type(result[key][field]) in {str, int}}
        return clean

    def _small_history(self) -> list[dict[str, Any]]:
        # Accepted requests, including pending evaluations, are authoritative.
        return [row for row in self.ledger.submissions() if row["kind"] == "small"]

    def _last_ladder_rank(self) -> int | None:
        history = self._small_history()
        if not history:
            return None
        return self.matches.by_id[history[-1]["request"]["opponent_ids"][-1]].rank

    def _allowed_next_rank(self, rank: int) -> bool:
        policy = self.experiment.opponent_policy
        if policy == "clone":
            return rank == self.experiment.clone_rank
        if policy == "top5":
            return rank <= 5
        if policy == "top4":
            return rank <= 4
        if policy == "ladder":
            last = self._last_ladder_rank()
            return rank == self.experiment.initial_rank if last is None else 0 <= last - rank <= 2
        return True

    def _small_request(self, ids: tuple[str, ...]) -> dict[str, Any]:
        policy = self.experiment.opponent_policy
        history = self._small_history()
        selected = ids
        if policy == "ladder":
            last = self._last_ladder_rank()
            for opponent_id in ids:
                rank = self.matches.by_id[opponent_id].rank
                if last is None:
                    if rank != self.experiment.initial_rank:
                        raise ValueError(
                            f"ladder must start at rank {self.experiment.initial_rank}"
                        )
                elif not 0 <= last - rank <= 2:
                    raise ValueError("ladder may repeat or improve numeric rank by only 1 or 2")
                last = rank
        elif policy == "clone":
            if len(ids) != 1 or self.matches.by_id[ids[0]].rank != self.experiment.clone_rank:
                raise ValueError("clone requires exactly one opponent at clone_rank per submission")
        elif policy == "top5":
            if any(self.matches.by_id[item].rank > 5 for item in ids):
                raise ValueError("top5 permits only evaluation pool ranks 1 through 5")
        elif policy == "top4":
            if any(self.matches.by_id[item].rank > 4 for item in ids):
                raise ValueError("top4 permits only evaluation pool ranks 1 through 4")
        elif policy == "random":
            # A local RNG keyed by accepted cost survives restarts and invalid requests.
            # Caller IDs supply only the batch size. Empty input defaults to one.
            offset = sum(int(row["cost"]) for row in history)
            seed = f"{self.experiment.seed}:{self.game}:random:{offset}"
            pool = sorted(self.matches.by_id)
            selected = tuple(random.Random(seed).sample(pool, len(ids) or 1))
        request: dict[str, Any] = {"opponent_ids": list(selected)}
        if policy != "model" or self.experiment.binary_feedback:
            request.update(
                requested_opponent_ids=list(ids),
                opponent_policy=policy,
                experiment_sha256=self.experiment.fingerprint,
                selected_opponent_ids=list(selected),
            )
        if policy == "clone":
            replicate = len(history) + 1
            seed_material = f"{self.experiment.seed}:{self.game}:clone:{self.experiment.clone_rank}"
            seed_base = int.from_bytes(hashlib.sha256(seed_material.encode()).digest()[:4], "big")
            request.update(replicate=replicate, seed=(seed_base + replicate) & 0x7FFFFFFF)
        return request

    def finalize_clone(self) -> str:
        """Controller-only: freeze current code after learning from the 32nd result.

        Runtime calls this after the final adaptation turn, never immediately on
        match completion. No official metric is manufactured for this snapshot.
        """
        if not self.experiment.is_clone:
            raise ValueError("clone finalization requires a clone experiment")
        if self.ledger.state()["status"] in {"finalizing", "reviewing", "complete"}:
            return self.restore_frozen_strategy()
        if self.ledger.state()["metadata"].get("clone_adaptation_complete") is not True:
            raise ValueError("clone requires the final adaptation turn before freezing")
        if self.ledger.budgets().small_remaining or self.ledger.pending_submission() is not None:
            raise ValueError("clone learning still has uncompleted submissions")
        history = self._small_history()
        if len(history) != 32 or any(row["status"] != "complete" or row["cost"] != 1 for row in history):
            raise ValueError("clone finalization requires 32 completed single-opponent submissions")
        if any(row["delivered_at"] is None for row in history):
            raise ValueError("clone feedback must be delivered before final adaptation and freeze")
        self._preflight()
        snapshot = self._snapshot()
        self.snapshots.verify(snapshot.snapshot_id)
        self.ledger.finalize_clone(snapshot.snapshot_id)
        self.restore_frozen_strategy()
        return snapshot.snapshot_id

    def _finish(
        self,
        submission_id: str,
        sequence: int,
        kind: str,
        snapshot: Snapshot,
        result: dict[str, Any],
        started_at: float,
    ) -> dict[str, Any]:
        snapshot = self.snapshots.verify(snapshot.snapshot_id)
        budgets = self.ledger.budgets()
        result = {
            **self._public_result(kind, result, bookkeeping=False),
            "match_id": submission_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_integrity": {
                "content_id": snapshot.content_id,
                "strategy_hash": snapshot.strategy_hash,
                "archive_sha256": snapshot.archive_sha256,
                "manifest_sha256": snapshot.manifest_sha256,
            },
            "budget": budgets.as_dict(),
        }
        result, should_stop = self._annotate_official_result(kind, snapshot, submission_id, result)
        champion = result.get("champion")
        final_snapshot_id = (
            str(champion["snapshot_id"])
            if kind == "large" and isinstance(champion, dict)
            else None
        )
        self.ledger.complete(
            submission_id, result, final_snapshot_id=final_snapshot_id
        )
        if kind == "large" and isinstance(champion, dict):
            champion_snapshot = str(champion["snapshot_id"])
            if should_stop and self.ledger.state()["status"] == "running":
                self.ledger.finalize_early(
                    champion_snapshot,
                    reason="rank 1 reached",
                )
            if self.ledger.state()["status"] == "finalizing":
                self.restore_frozen_strategy()
        self.trajectory.append(
            self._event(
                submission_id=submission_id,
                sequence=sequence,
                kind=kind,
                snapshot=snapshot,
                result=result,
                started_at=started_at,
            )
        )
        rebuild_report(self.run_root)
        if kind == "baseline":
            baseline_path = self.workspace / "artifacts" / "baseline.json"
            baseline_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            self.ledger.mark_delivered(submission_id)
        return result

    def baseline(self) -> dict[str, Any]:
        if self.experiment.is_clone:
            raise ValueError("clone trials prohibit baseline evaluations; use --skip-baseline")
        self.initialize_workspace()
        recovered = self._resume_matching_pending("baseline", {})
        if recovered is not None:
            return recovered
        self._preflight()
        snapshot = self._snapshot()
        submission_id, sequence = self.ledger.reserve("baseline", 0, snapshot, {})
        started_at = time.time()
        result = self._execute_pending(self.ledger.pending_submission() or {}, snapshot)
        return self._finish(submission_id, sequence, "baseline", snapshot, result, started_at)

    def small_match(self, opponent_ids: Iterable[str] = ()) -> dict[str, Any]:
        ids = tuple(opponent_ids)
        if self.experiment.fixed_small_batch is not None and len(ids) != self.experiment.fixed_small_batch:
            raise ValueError(
                f"small_match requires exactly {self.experiment.fixed_small_batch} opponents"
            )
        if ids or self.experiment.opponent_policy != "random":
            self.matches.validate_opponents(
                ids, allow_repeats=self.experiment.opponent_policy == "ladder"
            )
        pending = self.ledger.pending_submission()
        if pending is not None:
            original = pending["request"].get("requested_opponent_ids", pending["request"].get("opponent_ids"))
            if pending["kind"] != "small" or list(ids) != original:
                raise ValueError("an accepted match is pending recovery; retry that exact request")
            return self.recover_pending()
        budgets = self.ledger.budgets()
        request = self._small_request(ids)
        cost = len(request["opponent_ids"])
        lo, hi = self.experiment.small_batch_bounds()
        minimum = min(lo, budgets.small_remaining)
        if cost < minimum or cost > hi:
            raise ValueError(f"small_match requires between {minimum} and {hi} opponents")
        if cost > budgets.small_remaining:
            raise ValueError(
                f"small_match requests {cost} opponents with only {budgets.small_remaining} remaining"
            )
        self._preflight()
        snapshot = self._snapshot()
        submission_id, sequence = self.ledger.reserve("small", cost, snapshot, request)
        started_at = time.time()
        result = self._execute_pending(self.ledger.pending_submission() or {}, snapshot)
        return self._finish(submission_id, sequence, "small", snapshot, result, started_at)

    def large_match(self) -> dict[str, Any]:
        if self.experiment.is_clone:
            raise ValueError("clone trials prohibit large evaluations")
        recovered = self._resume_matching_pending("large", {})
        if recovered is not None:
            return recovered
        self._preflight()
        snapshot = self._snapshot()
        submission_id, sequence = self.ledger.reserve("large", 1, snapshot, {})
        started_at = time.time()
        result = self._execute_pending(self.ledger.pending_submission() or {}, snapshot)
        return self._finish(submission_id, sequence, "large", snapshot, result, started_at)

    def restore_champion(self) -> dict[str, Any]:
        if self.ledger.state()["status"] != "running":
            raise ValueError("run is not accepting strategy restores")
        champion, _ = self._official_policy_state()
        if champion is None:
            raise ValueError("no completed official champion is available")
        snapshot_id = str(champion["snapshot_id"])
        self._restore_strategy(snapshot_id)
        return {
            "kind": "restore_champion",
            "snapshot_id": snapshot_id,
            "strategy_hash": champion["strategy_hash"],
            "rank": champion.get("rank"),
            "elo": champion.get("elo"),
            "budget": self.ledger.budgets().as_dict(),
        }

    def budget_status(self) -> dict[str, Any]:
        """Return authoritative no-cost planning state without exposing the ledger."""

        state = self.ledger.state()
        submissions = self.ledger.submissions()
        return {
            "kind": "budget_status",
            "run_status": state["status"],
            "budget": self.ledger.budgets().as_dict(),
            "completed_submissions": {
                "small": sum(
                    1
                    for row in submissions
                    if row["kind"] == "small" and row["status"] == "complete"
                ),
                "large": sum(
                    1
                    for row in submissions
                    if row["kind"] == "large" and row["status"] == "complete"
                ),
            },
            "pending_match": any(row["status"] == "evaluating" for row in submissions),
            "official_strategy_frozen": state["frozen_snapshot_id"] is not None,
        }

    def list_opponents(
        self, *, rank_min: int = 1, rank_max: int | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """Expose only the public frozen leaderboard, without match cost."""

        low = max(1, int(rank_min))
        high = int(rank_max) if rank_max is not None else len(self.matches.opponents)
        if high < low:
            raise ValueError("rank_max must be >= rank_min")
        limit = max(1, min(int(limit), 64))
        rows = [
            {
                "opponent_id": row.opponent_id,
                "rank": row.rank,
                "reference_rank": row.reference_rank,
                "elo": row.elo,
                "track": row.track,
            }
            for row in self.matches.opponents
            if low <= row.rank <= high and self._allowed_next_rank(row.rank)
        ][:limit]
        return {
            "kind": "opponent_list",
            **scope(formal=self.remote),
            "game": self.game,
            "rank_min": low,
            "rank_max": high,
            "opponents": rows,
        }

    def _viewed_trajectory_ids(self) -> set[str]:
        viewed: set[str] = set()
        for row in self.ledger.submissions():
            if row["kind"] != "small" or row["status"] != "complete":
                continue
            request = row.get("request") or {}
            if request.get("trajectory_view") and isinstance(request.get("trajectory_id"), str):
                viewed.add(request["trajectory_id"])
        return viewed

    def list_pool_trajectories(
        self,
        *,
        rank_min: int = 1,
        rank_max: int | None = None,
        opponent_id: str | None = None,
        limit: int = 32,
    ) -> dict[str, Any]:
        if not self.experiment.is_offpolicy or self.pool_dense_catalog is None:
            raise ValueError("list_pool_trajectories requires offpolicy experiment")
        budgets = self.ledger.budgets()
        payload = list_trajectories(
            self.pool_dense_catalog,
            rank_min=rank_min,
            rank_max=rank_max,
            opponent_id=opponent_id,
            limit=limit,
            viewed_ids=self._viewed_trajectory_ids(),
        )
        payload["trajectory_views_remaining"] = budgets.small_remaining
        payload["trajectory_views_total"] = budgets.small_total
        payload["large_evaluations_remaining"] = budgets.large_remaining
        return payload

    def view_dense_trajectory(self, trajectory_id: str) -> dict[str, Any]:
        if not self.experiment.is_offpolicy or self.pool_dense_catalog is None:
            raise ValueError("view_dense_trajectory requires offpolicy experiment")
        if not isinstance(trajectory_id, str) or not trajectory_id.strip():
            raise ValueError("trajectory_id must be a non-empty string")
        trajectory_id = trajectory_id.strip()
        if trajectory_id not in self.pool_dense_catalog["_by_id"]:
            raise ValueError(f"unknown trajectory_id {trajectory_id!r}")
        if trajectory_id in self._viewed_trajectory_ids():
            raise ValueError(f"trajectory {trajectory_id!r} was already viewed in this run")
        pending = self.ledger.pending_submission()
        if pending is not None:
            original = pending["request"]
            expected = {"trajectory_view": True, "trajectory_id": trajectory_id}
            if pending["kind"] != "small" or original != expected:
                raise ValueError("a pending trajectory view must be retried with the exact same id")
            return self.recover_pending() or {}
        budgets = self.ledger.budgets()
        if budgets.small_remaining < 1:
            raise ValueError("trajectory view budget exhausted")
        self._preflight()
        snapshot = self._snapshot()
        request = {"trajectory_view": True, "trajectory_id": trajectory_id}
        submission_id, sequence = self.ledger.reserve("small", 1, snapshot, request)
        started_at = time.time()
        result = self._execute_pending(self.ledger.pending_submission() or {}, snapshot)
        return self._finish(submission_id, sequence, "small", snapshot, result, started_at)

    def workspace_manifest(self) -> dict[str, Any]:
        """Return the exact model-visible surface before the first match."""

        return {
            "kind": "workspace_manifest",
            **scope(formal=self.remote),
            "experiment": self.experiment.public_dict(),
            "small_feedback": (
                "Only win bool per seat; false means non-win, including draws. No small replays."
                if self.experiment.binary_feedback
                else (
                    "Off-policy observation: human-pool dense trajectories via view_dense_trajectory; "
                    "your own large evaluations never release dense replays."
                    if self.experiment.is_offpolicy
                    else "Detailed small results and public replays."
                )
            ),
            "opponent_policy": {
                "model": "Choose any frozen public opponent IDs.",
                "ladder": (
                    f"First rank {self.experiment.initial_rank}, then repeat or decrease numeric "
                    "rank by 1 or 2, including within batches."
                ),
                "random": "Public IDs specify batch size only; controller samples opponents using the frozen seed and accepted cost.",
                "top5": "Choose only evaluation pool ranks 1 through 5.",
                "top4": "Choose only evaluation pool ranks 1 through 4.",
                "clone": "Exactly one fixed clone_rank opponent per call; 32 submissions, no baseline/large/champion metric.",
                "offpolicy": (
                    "Observe frozen human-vs-human dense trajectories from the public catalog "
                    "(max 128 views). Submit large_match (max 16) whenever you want a full-pool "
                    "Elo/rank checkpoint; those evaluations never expose dense replays."
                ),
            }[self.experiment.opponent_policy],
            "read_only": (
                [
                    "resources/rules.md",
                    "resources/leaderboard.json",
                    "resources/manifest.json",
                    "resources/sdk/",
                    "resources/sdk-alternatives/",
                    "resources/examples/rank40/",
                ]
                if self.experiment.binary_feedback
                else [
                    "resources/rules.md",
                    "resources/leaderboard.json",
                    "resources/manifest.json",
                    "resources/sdk/",
                    "resources/sdk-alternatives/",
                    "resources/examples/rank40/",
                    "resources/replay/format.md",
                    "resources/replay/guide.md",
                    "resources/replay/reading_skill.md",
                    "resources/replay/translate",
                ]
            ),
            "writable": (
                ["strategy/", "notes/", "artifacts/"]
                if self.experiment.binary_feedback
                else [
                    "strategy/",
                    "skills/",
                    "notes/",
                    "replays/",
                    "artifacts/",
                ]
            ),
            "private": [
                "controller/",
                "evaluator/",
                "opponent source/",
                "credentials/",
                "network/",
            ],
            "replay_contract": {
                "one_json_per_accepted_seat_match": not self.experiment.binary_feedback,
                "one_narration_markdown_per_json": not self.experiment.binary_feedback,
                "raw_saiblo_replay_visible": False,
                "max_public_json_bytes": 256 * 1024,
            },
            "match_tools": ([
                "small_match({opponent_ids: string[]}) — exactly one clone_rank ID, costs one small point",
            ] if self.experiment.is_clone else [
                "list_pool_trajectories({rank_min?, rank_max?, opponent_id?, limit?}) — no cost catalog browse",
                "view_dense_trajectory({trajectory_id}) — one human-pool observation, costs one view",
                "large_match({}) — complete frozen public pool, costs one large point",
                "restore_champion({}) — no cost",
            ] if self.experiment.is_offpolicy else [
                "small_match({opponent_ids: string[]}) — 1-8 public IDs, costs one small point each",
                "large_match({}) — complete frozen public pool, costs one large point",
                "restore_champion({}) — no cost",
            ]),
        }

    def agent_result(self, result: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        """Persist complete evidence and return only the bounded decision summary."""

        if result.get("kind") == "restore_champion":
            return result
        if result.get("kind") == "trajectory_view":
            relative = Path("artifacts") / "match-results" / f"{result['match_id']}.json"
            if persist:
                atomic_write_json(self.workspace / relative, result)
            return {
                "kind": "trajectory_view",
                "match_id": result["match_id"],
                "trajectory_id": result["trajectory_id"],
                "rank_a": result.get("rank_a"),
                "rank_b": result.get("rank_b"),
                "replay_json": result.get("replay_json"),
                "replay_md": result.get("replay_md"),
                "budget": result.get("budget"),
                "raw_result_path": relative.as_posix(),
            }
        result = self._public_result(str(result.get("kind")), result)
        match_id = result.get("match_id")
        if not isinstance(match_id, str) or not match_id:
            raise ValueError("match result has no id")
        def public_paths(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: public_paths(item) for key, item in value.items()}
            if isinstance(value, list):
                return [public_paths(item) for item in value]
            if isinstance(value, str) and value.startswith(str(self.workspace) + "/"):
                return Path(value).relative_to(self.workspace).as_posix()
            return value

        result = public_paths(result)
        relative = Path("artifacts") / "match-results" / f"{match_id}.json"
        if persist:
            atomic_write_json(self.workspace / relative, result)
        if result.get("kind") == "small" and self.experiment.binary_feedback:
            return {**result, "raw_result_path": relative.as_posix()}
        compact = {
            key: result[key]
            for key in (
                "kind",
                "match_id",
                "snapshot_id",
                "budget",
                "wins",
                "draws",
                "losses",
                "candidate_errors",
                "infrastructure_retries",
                "elo",
                "elo_ci_low",
                "elo_ci_high",
                "rank",
                "pool_win_rate",
                "champion",
                "non_improving_large_streak",
                "stopped_early",
            )
            if key in result
        }
        if persist or (self.workspace / relative).exists():
            compact["raw_result_path"] = relative.as_posix()
        if result.get("kind") == "large":
            opponents = result.get("per_opponent")
            if isinstance(opponents, list):
                priority = sorted(
                    (row for row in opponents if isinstance(row, dict)),
                    key=lambda row: (
                        int(row.get("wins") or 0) - int(row.get("losses") or 0),
                        int(row.get("rank") or 10**9),
                    ),
                )[:12]
                compact["priority_opponents"] = [
                    {
                        key: row[key]
                        for key in (
                            "opponent_id",
                            "rank",
                            "elo",
                            "wins",
                            "draws",
                            "losses",
                            "candidate_errors",
                        )
                        if key in row
                    }
                    for row in priority
                ]
        else:
            opponents = result.get("opponents")
            if isinstance(opponents, list):
                compact["opponents"] = opponents
            seats = result.get("seats")
            if isinstance(seats, list):
                ordered = sorted(
                    (row for row in seats if isinstance(row, dict)),
                    key=lambda row: str(row.get("outcome")) != "loss",
                )
                compact["priority_replay_paths"] = [
                    row["replay_path"] for row in ordered if isinstance(row.get("replay_path"), str)
                ][:8]
        return compact

    def model_tool_call(self, name: str, arguments: object) -> dict[str, Any]:
        """The single production/doctor boundary for model-visible tool results."""
        result = self.tool_call(name, arguments)
        if name in {"small_match", "large_match", "view_dense_trajectory"}:
            return self.agent_result(result)
        return result

    def tool_call(self, name: str, arguments: object) -> dict[str, Any]:
        if name == "workspace_shell":
            if not isinstance(arguments, dict) or not isinstance(arguments.get("command"), str):
                raise ValueError("workspace_shell requires {command: string}")
            command = arguments["command"].strip()
            if not command:
                raise ValueError("workspace_shell command must not be empty")
            # The model-facing shell is deliberately a dynamic tool rather than
            # the host Codex shell.  Run it in a tiny bwrap namespace so an API
            # response can never reach the controller, player pool, credentials,
            # or the network, while retaining normal build/edit utilities.
            # Enforce access with mount/network namespaces, not substring scans:
            # source code can legitimately contain Python Ellipsis, bitwise ~,
            # path strings and documentation of network commands.
            writable = [self.workspace / name for name in ("strategy", "skills", "notes", "replays", "artifacts")]
            if any(path.is_symlink() or not path.is_dir() for path in writable):
                raise ValueError("workspace writable roots must be real directories")
            bwrap = shutil.which("bwrap")
            if not bwrap:
                raise RuntimeError("bubblewrap is required for workspace_shell")
            argv = [
                bwrap, "--die-with-parent", "--new-session", "--unshare-net",
                "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup",
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--tmpfs", "/home", "--tmpfs", "/root", "--tmpfs", "/run", "--tmpfs", "/sys",
                "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
                "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
                "--symlink", "usr/bin", "/bin",
                "--dir", "/workspace", "--ro-bind", str(self.workspace), "/workspace",
                *(argument for path in writable for argument in ("--bind", str(path), f"/workspace/{path.name}")),
                "--dir", "/tmp/home", "--setenv", "HOME", "/tmp/home",
                "--setenv", "PATH", "/usr/bin:/bin", "--chdir", "/workspace",
                "--", "/bin/bash", "-lc", command,
            ]
            completed = subprocess.run(
                argv, cwd=self.workspace, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp/home", "LANG": "C.UTF-8"},
                capture_output=True, text=True, errors="replace", timeout=120, check=False,
            )
            output = (completed.stdout + completed.stderr).encode("utf-8")
            return {"exit_code": completed.returncode, "output": output[:32768].decode("utf-8", errors="ignore"), "truncated": len(output) > 32768}
        if name == "workspace_manifest":
            if arguments not in ({}, None):
                raise ValueError("workspace_manifest takes no arguments")
            return self.workspace_manifest()
        if name == "list_opponents":
            if arguments in ({}, None):
                return self.list_opponents()
            if not isinstance(arguments, dict):
                raise ValueError("list_opponents accepts rank_min, rank_max, limit")
            return self.list_opponents(
                rank_min=int(arguments.get("rank_min", 1)),
                rank_max=(
                    int(arguments["rank_max"]) if arguments.get("rank_max") is not None else None
                ),
                limit=int(arguments.get("limit", 20)),
            )
        if name == "budget_status":
            if arguments not in ({}, None):
                raise ValueError("budget_status takes no arguments")
            return self.budget_status()
        if name == "list_pool_trajectories":
            if arguments in ({}, None):
                return self.list_pool_trajectories()
            if not isinstance(arguments, dict):
                raise ValueError("list_pool_trajectories accepts optional filters")
            return self.list_pool_trajectories(
                rank_min=int(arguments.get("rank_min", 1)),
                rank_max=(
                    int(arguments["rank_max"]) if arguments.get("rank_max") is not None else None
                ),
                opponent_id=(
                    str(arguments["opponent_id"]) if arguments.get("opponent_id") else None
                ),
                limit=int(arguments.get("limit", 32)),
            )
        if name == "view_dense_trajectory":
            if not isinstance(arguments, dict) or not isinstance(arguments.get("trajectory_id"), str):
                raise ValueError("view_dense_trajectory requires {trajectory_id: string}")
            return self.view_dense_trajectory(arguments["trajectory_id"])
        if name == "small_match":
            if self.experiment.is_offpolicy:
                raise ValueError("offpolicy prohibits small_match; use view_dense_trajectory")
            if self.experiment.opponent_policy == "random" and arguments in ({}, None):
                return self.small_match()
            if not isinstance(arguments, dict) or not isinstance(
                arguments.get("opponent_ids"), list
            ):
                raise ValueError("small_match requires {opponent_ids: string[]}")
            if not all(isinstance(item, str) and item for item in arguments["opponent_ids"]):
                raise ValueError("opponent_ids must contain non-empty strings")
            return self.small_match(arguments["opponent_ids"])
        if name == "large_match":
            if arguments not in ({}, None):
                raise ValueError("large_match takes no arguments")
            return self.large_match()
        if name == "restore_champion":
            if arguments not in ({}, None):
                raise ValueError("restore_champion takes no arguments")
            return self.restore_champion()
        raise ValueError(f"unknown arena tool: {name}")

    def close(self) -> None:
        self.ledger.close()

    def __enter__(self) -> BenchmarkService:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

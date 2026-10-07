"""Transactional run state and match budget accounting."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aa_arena.benchmark.snapshot import Snapshot


class BudgetError(ValueError):
    pass


@dataclass(frozen=True)
class BudgetState:
    small_total: int
    small_used: int
    large_total: int
    large_used: int
    small_expired: int = 0
    large_expired: int = 0

    @property
    def small_remaining(self) -> int:
        return self.small_total - self.small_used - self.small_expired

    @property
    def large_remaining(self) -> int:
        return self.large_total - self.large_used - self.large_expired

    def as_dict(self) -> dict[str, int]:
        """Return the complete public budget view exposed to the agent."""

        result = {
            "small_total": self.small_total,
            "small_used": self.small_used,
            "small_remaining": self.small_remaining,
            "large_total": self.large_total,
            "large_used": self.large_used,
            "large_remaining": self.large_remaining,
        }
        if self.small_expired:
            result["small_expired"] = self.small_expired
        if self.large_expired:
            result["large_expired"] = self.large_expired
        return result



class RunLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS run_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                run_id TEXT NOT NULL,
                game TEXT NOT NULL,
                model_profile TEXT NOT NULL,
                small_total INTEGER NOT NULL,
                small_used INTEGER NOT NULL DEFAULT 0,
                large_total INTEGER NOT NULL,
                large_used INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                thread_id TEXT,
                current_turn_id TEXT,
                goal_json TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                frozen_snapshot_id TEXT,
                active_seconds REAL NOT NULL DEFAULT 0,
                token_usage_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS submissions (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK (kind IN ('baseline','small','large')),
                cost INTEGER NOT NULL,
                status TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                parent_snapshot_id TEXT,
                request_json TEXT NOT NULL,
                result_json TEXT,
                delivered_at REAL,
                active_seconds REAL NOT NULL,
                token_usage_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                completed_at REAL
            );
            """
        )
        columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(run_state)").fetchall()
        }
        for name, declaration in (
            ("current_turn_id", "TEXT"),
            ("goal_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE run_state ADD COLUMN {name} {declaration}")
        submission_columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(submissions)").fetchall()
        }
        if "delivered_at" not in submission_columns:
            self.connection.execute("ALTER TABLE submissions ADD COLUMN delivered_at REAL")

    def initialize(
        self,
        *,
        game: str,
        model_profile: str,
        small_budget: int,
        large_budget: int,
        run_id: str | None = None,
    ) -> str:
        if small_budget < 0 or large_budget < 0 or small_budget + large_budget == 0:
            raise ValueError("budgets must be non-negative with at least one positive budget")
        existing = self.connection.execute("SELECT * FROM run_state WHERE singleton=1").fetchone()
        if existing:
            expected = (game, model_profile, small_budget, large_budget)
            actual = (
                existing["game"],
                existing["model_profile"],
                existing["small_total"],
                existing["large_total"],
            )
            if actual != expected:
                raise ValueError("existing run ledger does not match requested configuration")
            return str(existing["run_id"])
        now = time.time()
        value = run_id or uuid.uuid4().hex
        self.connection.execute(
            """INSERT INTO run_state(
               singleton,run_id,game,model_profile,small_total,small_used,
               large_total,large_used,status,thread_id,current_turn_id,
               goal_json,metadata_json,frozen_snapshot_id,active_seconds,
               token_usage_json,created_at,updated_at)
               VALUES (1,?,?,?,?,0,?,0,'running',NULL,NULL,'{}','{}',NULL,0,'{}',?,?)""",
            (value, game, model_profile, small_budget, large_budget, now, now),
        )
        return value

    def extend_budget_once(self, *, small: int = 128, large: int = 16) -> None:
        """Add one stage to the SAME ledger; never reset usage/history/session."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            state = self.state()
            metadata = dict(state["metadata"])
            if metadata.get("budget_extension"):
                extension = metadata["budget_extension"]
                if extension["small_added"] != small or extension["large_added"] != large:
                    raise BudgetError("budget extension already applied with different amounts")
                self.connection.execute("COMMIT")
                return
            if state["status"] != "complete" or not state["frozen_snapshot_id"]:
                raise BudgetError("budget extension requires a completed frozen first stage")
            if type(small) is not int or type(large) is not int or small < 0 or large < 1:
                raise BudgetError("extension requires nonnegative small and positive large units")
            if self.pending_submission() is not None:
                raise BudgetError("cannot extend with an evaluation pending")
            metadata["budget_extension"] = {
                "small_added": small, "large_added": large,
                "small_expired": state["small_total"] - state["small_used"],
                "large_expired": state["large_total"] - state["large_used"],
                "first_stage": {key: state[key] for key in (
                    "small_total", "small_used", "large_total", "large_used", "token_usage",
                    "active_seconds", "frozen_snapshot_id")},
                "first_stage_champion": metadata.get("champion"),
                "extended_at": time.time(),
            }
            metadata.pop("stop_reason", None)
            self.connection.execute(
                """UPDATE run_state SET small_total=small_total+?,large_total=large_total+?,
                   status='running',frozen_snapshot_id=NULL,metadata_json=?,updated_at=? WHERE singleton=1""",
                (small, large, json.dumps(metadata, ensure_ascii=False), time.time()),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def state(self) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM run_state WHERE singleton=1").fetchone()
        if row is None:
            raise RuntimeError("run ledger has not been initialized")
        value = dict(row)
        value["token_usage"] = json.loads(value.pop("token_usage_json"))
        value["goal"] = json.loads(value.pop("goal_json"))
        value["metadata"] = json.loads(value.pop("metadata_json"))
        return value

    def budgets(self) -> BudgetState:
        row = self.state()
        return BudgetState(
            int(row["small_total"]),
            int(row["small_used"]),
            int(row["large_total"]),
            int(row["large_used"]),
            int(row["metadata"].get("budget_extension", {}).get("small_expired", 0)),
            int(row["metadata"].get("budget_extension", {}).get("large_expired", 0)),
        )

    def update_runtime(
        self,
        *,
        active_seconds: float | None = None,
        token_usage: dict[str, int] | None = None,
        thread_id: str | None = None,
        current_turn_id: str | None = None,
        goal: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        current = self.state()
        self.connection.execute(
            """UPDATE run_state SET active_seconds=?, token_usage_json=?,
               thread_id=COALESCE(?,thread_id),
               current_turn_id=COALESCE(?,current_turn_id), goal_json=?,
               metadata_json=?, updated_at=? WHERE singleton=1""",
            (
                float(current["active_seconds"] if active_seconds is None else active_seconds),
                json.dumps(current["token_usage"] if token_usage is None else token_usage),
                thread_id,
                current_turn_id,
                json.dumps(current["goal"] if goal is None else goal, ensure_ascii=False),
                json.dumps(
                    current["metadata"] if metadata is None else metadata, ensure_ascii=False
                ),
                time.time(),
            ),
        )

    def reserve(
        self,
        kind: str,
        cost: int,
        snapshot: Snapshot,
        request: dict[str, Any],
    ) -> tuple[str, int]:
        if kind not in {"baseline", "small", "large"}:
            raise ValueError(f"unsupported submission kind: {kind}")
        if cost < 0 or (kind != "baseline" and cost == 0):
            raise ValueError("submission cost must be positive")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            state = self.state()
            if state["status"] != "running":
                raise BudgetError(f"run is not accepting submissions: {state['status']}")
            column = None
            if kind == "small":
                if cost > self.budgets().small_remaining:
                    raise BudgetError("small-match budget exhausted")
                column = "small_used"
            elif kind == "large":
                if cost > self.budgets().large_remaining:
                    raise BudgetError("large-match budget exhausted")
                column = "large_used"
            pending = self.connection.execute(
                "SELECT submission_id FROM submissions WHERE status='evaluating' LIMIT 1"
            ).fetchone()
            if pending is not None:
                raise BudgetError(f"submission is still evaluating: {pending['submission_id']}")
            if column:
                self.connection.execute(
                    f"UPDATE run_state SET {column}={column}+?, updated_at=? WHERE singleton=1",
                    (cost, time.time()),
                )
            submission_id = uuid.uuid4().hex
            cursor = self.connection.execute(
                """INSERT INTO submissions(
                   submission_id,kind,cost,status,snapshot_id,parent_snapshot_id,
                   request_json,active_seconds,token_usage_json,created_at)
                   VALUES (?,?,?,'evaluating',?,?,?,?,?,?)""",
                (
                    submission_id,
                    kind,
                    cost,
                    snapshot.snapshot_id,
                    snapshot.parent_snapshot_id,
                    json.dumps(request, ensure_ascii=False, sort_keys=True),
                    float(state["active_seconds"]),
                    json.dumps(state["token_usage"], sort_keys=True),
                    time.time(),
                ),
            )
            sequence = int(cursor.lastrowid)
            self.connection.execute("COMMIT")
            return submission_id, sequence
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def complete(
        self,
        submission_id: str,
        result: dict[str, Any],
        *,
        final_snapshot_id: str | None = None,
    ) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT kind,snapshot_id FROM submissions WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown submission: {submission_id}")
            self.connection.execute(
                "UPDATE submissions SET status='complete',result_json=?,completed_at=? WHERE submission_id=?",
                (
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    time.time(),
                    submission_id,
                ),
            )
            budgets = self.budgets()
            state = self.state()
            offpolicy = state.get("metadata", {}).get("experiment", {}).get("opponent_policy") == "offpolicy"
            # Main-table runs freeze at the full-pool limit. The paper's
            # off-policy observation protocol can consume remaining replay views.
            # Active cloning (large_total == 0) has its own final adaptation turn.
            should_finalize = (
                state["status"] == "running"
                and budgets.large_total > 0
                and budgets.large_remaining == 0
                and (
                    (row["kind"] == "large" and (not offpolicy or budgets.small_remaining == 0))
                    or (offpolicy and row["kind"] == "small" and budgets.small_remaining == 0)
                )
            )
            if should_finalize:
                meta = state.get("metadata") or {}
                champion = meta.get("champion") if isinstance(meta.get("champion"), dict) else {}
                frozen_snapshot_id = (
                    final_snapshot_id
                    or champion.get("snapshot_id")
                    or str(row["snapshot_id"])
                )
                official = self.connection.execute(
                    """SELECT submission_id FROM submissions
                       WHERE snapshot_id=? AND status='complete'
                       AND kind IN ('baseline','large') LIMIT 1""",
                    (frozen_snapshot_id,),
                ).fetchone()
                if official is None:
                    raise BudgetError(
                        "final snapshot must have a completed official evaluation"
                    )
                self.connection.execute(
                    "UPDATE run_state SET status='finalizing',frozen_snapshot_id=?,updated_at=? WHERE singleton=1",
                    (frozen_snapshot_id, time.time()),
                )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def mark_complete(self) -> None:
        self.connection.execute(
            "UPDATE run_state SET status='complete',updated_at=? WHERE singleton=1", (time.time(),)
        )

    def finalize_early(self, snapshot_id: str, *, reason: str) -> None:
        """Freeze an already large-tested champion before exhausting the budget."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            state = self.state()
            if state["status"] != "running":
                raise BudgetError(f"run cannot finalize early from {state['status']}")
            official = self.connection.execute(
                """SELECT submission_id FROM submissions
                   WHERE snapshot_id=? AND status='complete'
                   AND kind IN ('baseline','large') LIMIT 1""",
                (snapshot_id,),
            ).fetchone()
            if official is None:
                raise BudgetError("early final snapshot has no completed official result")
            pending = self.connection.execute(
                "SELECT submission_id FROM submissions WHERE status='evaluating' LIMIT 1"
            ).fetchone()
            if pending is not None:
                raise BudgetError("cannot finalize while a submission is evaluating")
            metadata = dict(state["metadata"])
            metadata["stop_reason"] = reason
            self.connection.execute(
                """UPDATE run_state SET status='finalizing',frozen_snapshot_id=?,
                   metadata_json=?,updated_at=? WHERE singleton=1""",
                (snapshot_id, json.dumps(metadata, ensure_ascii=False), time.time()),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def finalize_clone(self, snapshot_id: str) -> None:
        """Freeze learned code after all 32 fixed-opponent observations and adaptation.

        The service verifies the new snapshot; unlike official finalization it
        need not have been submitted, and has no measured Elo or pool rank.
        """
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            state = self.state()
            if state["status"] in {"finalizing", "reviewing", "complete"}:
                if state["frozen_snapshot_id"] != snapshot_id:
                    raise BudgetError("clone final snapshot is already frozen")
                self.connection.execute("COMMIT")
                return
            if state["status"] != "running":
                raise BudgetError(f"clone cannot finalize from {state['status']}")
            if (state["small_total"], state["small_used"], state["large_total"],
                    state["large_used"]) != (32, 32, 0, 0):
                raise BudgetError("clone finalization requires exactly 32 small and zero large matches")
            if self.pending_submission() is not None:
                raise BudgetError("cannot finalize while a submission is evaluating")
            rows = self.submissions()
            opponents = set()
            for row in rows:
                ids = row["request"].get("opponent_ids", [])
                if (row["kind"] != "small" or row["status"] != "complete"
                        or row["cost"] != 1 or len(ids) != 1):
                    raise BudgetError("clone requires 32 completed single-opponent submissions")
                opponents.add(ids[0])
            if len(rows) != 32 or len(opponents) != 1:
                raise BudgetError("clone requires the same fixed opponent for all 32 submissions")
            metadata = dict(state["metadata"])
            metadata["stop_reason"] = "clone learning complete; evaluation deferred"
            self.connection.execute(
                """UPDATE run_state SET status='finalizing',frozen_snapshot_id=?,
                   metadata_json=?,updated_at=? WHERE singleton=1""",
                (snapshot_id, json.dumps(metadata, ensure_ascii=False), time.time()),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def begin_final_review(self) -> bool:
        """Claim the single allowed post-freeze review turn."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            state = self.state()
            if state["status"] != "finalizing":
                self.connection.execute("COMMIT")
                return False
            self.connection.execute(
                "UPDATE run_state SET status='reviewing',updated_at=? WHERE singleton=1",
                (time.time(),),
            )
            self.connection.execute("COMMIT")
            return True
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def latest_snapshot_id(self) -> str | None:
        row = self.connection.execute(
            "SELECT snapshot_id FROM submissions ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return str(row[0]) if row else None

    def submissions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM submissions ORDER BY sequence").fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["request"] = json.loads(value.pop("request_json"))
            raw_result = value.pop("result_json")
            value["result"] = json.loads(raw_result) if raw_result else None
            value["token_usage"] = json.loads(value.pop("token_usage_json"))
            values.append(value)
        return values

    def pending_submission(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM submissions WHERE status='evaluating' ORDER BY sequence LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["request"] = json.loads(value.pop("request_json"))
        value.pop("result_json")
        value["token_usage"] = json.loads(value.pop("token_usage_json"))
        return value

    def mark_delivered(self, submission_id: str) -> None:
        self.connection.execute(
            "UPDATE submissions SET delivered_at=COALESCE(delivered_at,?) WHERE submission_id=?",
            (time.time(), submission_id),
        )

    def undelivered_results(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT submission_id,kind,result_json FROM submissions
               WHERE status='complete' AND delivered_at IS NULL AND kind!='baseline'
               ORDER BY sequence"""
        ).fetchall()
        return [
            {
                "match_id": str(row["submission_id"]),
                "kind": str(row["kind"]),
                "result": json.loads(row["result_json"]),
            }
            for row in rows
        ]

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> RunLedger:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

"""Private full-pool evaluator API. Run behind an HTTPS reverse proxy."""

from __future__ import annotations
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import fcntl
import shutil
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import traceback
from aa_arena.benchmark.matches import MatchService, load_opponents
from aa_arena.benchmark.remote import MAX_UPLOAD, unpack_strategy, safe_relative
from aa_arena.io import canonical_hash
from aa_arena.resources import ARENA_GAMES, _rating_rows

IDENT = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def identifier(value):
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise ValueError("Invalid identifier")
    return value


class EvaluationStore:
    def __init__(
        self,
        root,
        repository,
        users,
        *,
        workers=8,
        jobs=2,
        catalog_root=None,
        match_factory=MatchService,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.lock = (self.root / ".server.lock").open("a")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.repository = Path(repository).resolve()
        self.users = users
        self.workers = workers
        self.match_factory = match_factory
        self.catalog_root = Path(catalog_root).resolve() if catalog_root else None
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=jobs)
        self.job_slots = threading.BoundedSemaphore(jobs)
        self.large_job_limit = max(1, jobs - 1)
        with self.db() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS runs(owner TEXT,id TEXT,game TEXT,seed INTEGER,experiment TEXT,small_total INTEGER,large_total INTEGER,small_used INTEGER DEFAULT 0,large_used INTEGER DEFAULT 0,baseline_used INTEGER DEFAULT 0,extended INTEGER DEFAULT 0,pool_hash TEXT,PRIMARY KEY(owner,id));
CREATE TABLE IF NOT EXISTS jobs(owner TEXT,run TEXT,id TEXT,digest TEXT,payload TEXT,status TEXT,result TEXT,attempts INTEGER DEFAULT 0,PRIMARY KEY(owner,run,id));""")
            db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
        self.thread = threading.Thread(target=self.dispatch, daemon=True)
        self.thread.start()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / "jobs.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def authenticate(self, header):
        if not header or not header.startswith("Bearer "):
            raise PermissionError()
        digest = hashlib.sha256(header[7:].encode()).hexdigest()
        for owner, conf in self.users.items():
            if hmac.compare_digest(digest, conf["token_sha256"]):
                return owner
        raise PermissionError()

    def run_path(self, owner, run):
        return self.root / "runs" / hashlib.sha256((owner + "\0" + run).encode()).hexdigest()

    def inventory(self, game):
        if game not in ARENA_GAMES:
            raise ValueError("Unknown game")
        rows = _rating_rows(game, self.repository)
        actual = load_opponents(game, self.repository)
        if [r.opponent_id for r in actual] != [r["opponent_id"] for r in rows]:
            raise RuntimeError("Server requires the complete private opponent pool")
        return [{k: r.get(k) for k in ("opponent_id", "rank", "elo", "track")} for r in rows]

    def open_run(self, owner, p):
        run = identifier(p["run_id"])
        game = p["game"]
        rows = self.inventory(game)
        pool_hash = canonical_hash(rows)
        small = p["small_total"]
        large = p["large_total"]
        seed = p["seed"]
        experiment = p["experiment"]
        from aa_arena.benchmark.experiment import ExperimentConfig

        config = ExperimentConfig(**experiment)
        config.validate_budgets(small, large)
        if (
            type(small) is not int
            or type(large) is not int
            or type(seed) is not int
            or not (0 <= small <= 384 and 0 <= large <= 48)
        ):
            raise ValueError("Invalid budget")
        encoded = json.dumps(experiment, sort_keys=True)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM runs WHERE owner=? AND id=?", (owner, run)).fetchone()
            if old:
                if (old["game"], old["seed"], old["experiment"], old["pool_hash"]) != (
                    game,
                    seed,
                    encoded,
                    pool_hash,
                ):
                    raise ValueError("Run configuration or pool changed")
                if (small, large) != (old["small_total"], old["large_total"]):
                    pending = db.execute(
                        "SELECT count(*) FROM jobs WHERE owner=? AND run=? AND "
                        "(status IN ('queued','running') OR "
                        "(status='failed' AND json_extract(payload,'$.kind')!='small'))",
                        (owner, run),
                    ).fetchone()[0]
                    if not (
                        old["small_total"] == 128
                        and old["large_total"] == 16
                        and old["large_used"] == 16
                        and not old["extended"]
                        and not pending
                        and (small, large) == (384, 48)
                    ):
                        raise ValueError("Invalid continuation")
                    db.execute(
                        "UPDATE runs SET small_total=384,large_total=48,small_used=128,extended=1 WHERE owner=? AND id=?",
                        (owner, run),
                    )
            else:
                if small > 128 or large > 16:
                    raise ValueError("New runs have at most 128/16 budget")
                n = db.execute("SELECT count(*) FROM runs WHERE owner=?", (owner,)).fetchone()[0]
                if n >= int(self.users[owner].get("max_runs", 100)):
                    raise ValueError("Account run quota exceeded")
                db.execute(
                    "INSERT INTO runs(owner,id,game,seed,experiment,small_total,large_total,pool_hash) VALUES(?,?,?,?,?,?,?,?)",
                    (owner, run, game, seed, encoded, small, large, pool_hash),
                )
        return {"run_id": run, "opponents": rows, "pool_sha256": pool_hash}

    def submit(self, owner, run, p):
        identifier(run)
        sid = identifier(p["submission_id"])
        kind = p["kind"]
        if kind not in {"small", "large", "baseline"}:
            raise ValueError("Invalid match kind")
        digest = canonical_hash(p)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            r = db.execute("SELECT * FROM runs WHERE owner=? AND id=?", (owner, run)).fetchone()
            if r is None:
                raise KeyError("Run not found")
            existing = db.execute(
                "SELECT * FROM jobs WHERE owner=? AND run=? AND id=?", (owner, run, sid)
            ).fetchone()
            if existing:
                if existing["digest"] != digest:
                    raise ValueError("Idempotency key reused with different content")
                if existing["status"] == "failed" and existing["attempts"] < 3:
                    if db.execute(
                        "SELECT count(*) FROM jobs WHERE owner=? AND run=? AND "
                        "(status IN ('queued','running') OR "
                        "(status='failed' AND json_extract(payload,'$.kind')!='small'))",
                        (owner, run),
                    ).fetchone()[0]:
                        raise ValueError("Run already has a pending submission")
                    db.execute(
                        "UPDATE jobs SET status='queued' WHERE owner=? AND run=? AND id=?",
                        (owner, run, sid),
                    )
            else:
                if db.execute(
                    "SELECT count(*) FROM jobs WHERE owner=? AND run=? AND "
                        "(status IN ('queued','running') OR "
                        "(status='failed' AND json_extract(payload,'$.kind')!='small'))",
                    (owner, run),
                ).fetchone()[0]:
                    raise ValueError("Run already has a pending submission")
                conf = json.loads(r["experiment"])
                ids = p.get("opponent_ids", [])
                rows = {x["opponent_id"]: x for x in self.inventory(r["game"])}
                if not isinstance(ids, list) or any(
                    not isinstance(x, str) or x not in rows for x in ids
                ):
                    raise ValueError("Invalid opponent list")
                policy = conf["opponent_policy"]
                binary = conf.get("feedback") == "binary"
                if kind == "small":
                    if policy == "offpolicy" or not 1 <= len(ids) <= 8:
                        raise ValueError("Small match unavailable")
                    if len(set(ids)) != len(ids) and policy != "ladder":
                        raise ValueError("Repeated opponents unavailable")
                    if conf.get("fixed_small_batch") and len(ids) != conf["fixed_small_batch"]:
                        raise ValueError("Incorrect batch size")
                    cap = {"top4": 4, "top5": 5}.get(policy)
                    if cap and any(rows[i]["rank"] > cap for i in ids):
                        raise ValueError("Opponent outside experiment arm")
                    if policy == "clone" and (
                        len(ids) != 1 or rows[ids[0]]["rank"] != conf["clone_rank"]
                    ):
                        raise ValueError("Incorrect clone opponent")
                    if p.get("feedback") != ("binary" if binary else "detailed"):
                        raise ValueError("Feedback differs from experiment")
                    if bool(p.get("allow_repeats")) != (policy == "ladder"):
                        raise ValueError("Repeat policy differs from experiment")
                    if policy != "clone" and p["seed"] != r["seed"]:
                        raise ValueError("Seed differs from experiment")
                    column = "small_used"
                    cost = len(ids)
                    limit = r["small_total"]
                elif kind == "large":
                    if policy == "clone":
                        raise ValueError("Clone trials prohibit large matches")
                    column = "large_used"
                    cost = 1
                    limit = r["large_total"]
                else:
                    if policy in {"clone", "offpolicy"} or r["small_used"] or r["large_used"]:
                        raise ValueError("Baseline unavailable")
                    column = "baseline_used"
                    cost = 1
                    limit = 1
                if r[column] + cost > limit:
                    raise ValueError("Budget exhausted")
                # Validate all paths and decompressed bytes before accepting any job.
                scratch = self.run_path(owner, run) / "uploads" / sid
                if scratch.exists():
                    shutil.rmtree(scratch)
                try:
                    unpack_strategy(p["files"], scratch)
                except Exception:
                    shutil.rmtree(scratch, ignore_errors=True)
                    raise
                db.execute(
                    f"UPDATE runs SET {column}={column}+? WHERE owner=? AND id=?",
                    (cost, owner, run),
                )
                stored = {k: v for k, v in p.items() if k != "files"}
                stored["strategy_root"] = str(scratch)
                db.execute(
                    "INSERT INTO jobs(owner,run,id,digest,payload,status) VALUES(?,?,?,?,?,?)",
                    (owner, run, sid, digest, json.dumps(stored), "queued"),
                )
        return self.status(owner, run, sid)

    def status(self, owner, run, sid):
        identifier(run)
        identifier(sid)
        with self.db() as db:
            r = db.execute(
                "SELECT * FROM jobs WHERE owner=? AND run=? AND id=?", (owner, run, sid)
            ).fetchone()
        if r is None:
            raise KeyError("Job not found")
        result = {"submission_id": sid, "status": r["status"]}
        if r["status"] in {"complete", "failed"} and r["result"]:
            result.update(json.loads(r["result"]))
        return result

    def dispatch(self):
        while not self.stop.wait(0.1):
            if not self.job_slots.acquire(blocking=False):
                continue
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                active_large = db.execute(
                    "SELECT count(*) FROM jobs WHERE status='running' "
                    "AND json_extract(payload,'$.kind') != 'small'"
                ).fetchone()[0]
                # CPU admission alone cannot reserve a job slot. Keep one slot
                # available for interactive small matches during long sweeps.
                r = db.execute(
                    "SELECT * FROM jobs WHERE status='queued' AND "
                    "(json_extract(payload,'$.kind')='small' OR ? < ?) "
                    "ORDER BY (json_extract(payload,'$.kind')='small') DESC, rowid LIMIT 1",
                    (active_large, self.large_job_limit),
                ).fetchone()
                if r:
                    db.execute(
                        "UPDATE jobs SET status='running',attempts=attempts+1 WHERE owner=? AND run=? AND id=?",
                        (r["owner"], r["run"], r["id"]),
                    )
            if r:
                self.pool.submit(self.execute, dict(r))
            else:
                self.job_slots.release()

    def execute(self, job):
        owner, run, sid = job["owner"], job["run"], job["id"]
        path = self.run_path(owner, run)
        phase = "setup"
        try:
            with self.db() as db:
                r = dict(
                    db.execute("SELECT * FROM runs WHERE owner=? AND id=?", (owner, run)).fetchone()
                )
            if canonical_hash(self.inventory(r["game"])) != r["pool_hash"]:
                raise ValueError("Frozen pool changed")
            p = json.loads(job["payload"])
            m = self.match_factory(
                r["game"],
                path,
                repository=self.repository,
                workers=self.workers,
                seed=r["seed"],
                pool_snapshot=path / "pool.json",
            )
            phase = "candidate_preflight"
            m.preflight_candidate(Path(p["strategy_root"]))
            phase = "evaluation"
            if p["kind"] == "small":
                result = m.small_match(
                    Path(p["strategy_root"]),
                    p["opponent_ids"],
                    sid,
                    path / "public-replays",
                    feedback=p["feedback"],
                    seed=p["seed"],
                    allow_repeats=p["allow_repeats"],
                    redact_diagnostics=True,
                )
            else:
                result = m.large_match(Path(p["strategy_root"]), sid)
            artifacts = []
            total = 0
            for seat in result.get("seats", []):
                # Backend stderr and source excerpts are private operator diagnostics.
                seat["diagnostic"] = None
                for key in ("replay_path", "narration_path"):
                    if not seat.get(key):
                        continue
                    f = Path(seat[key])
                    base = path / "public-replays" / sid
                    if not f.resolve().is_relative_to(base.resolve()) or f.suffix not in {
                        ".json",
                        ".md",
                    }:
                        raise ValueError("Non-public artifact")
                    b = f.read_bytes()
                    total += len(b)
                    if total > MAX_UPLOAD:
                        raise ValueError("Replay response too large")
                    name = f.relative_to(base).as_posix()
                    seat[key] = name
                    artifacts.append(
                        {
                            "path": name,
                            "data": base64.b64encode(b).decode(),
                            "sha256": hashlib.sha256(b).hexdigest(),
                        }
                    )
            receipt = {
                "run_id": run,
                "submission_id": sid,
                "pool_sha256": r["pool_hash"],
                "request_sha256": job["digest"],
                "result_sha256": canonical_hash(result),
                "small_used": r["small_used"],
                "large_used": r["large_used"],
                "completed_at": time.time(),
            }
            out = json.dumps({"result": result, "artifacts": artifacts, "receipt": receipt})
            with self.db() as db:
                db.execute(
                    "UPDATE jobs SET status='complete',result=? WHERE owner=? AND run=? AND id=?",
                    (out, owner, run, sid),
                )
        except Exception:
            (path / "operator-errors").mkdir(parents=True, exist_ok=True)
            (path / "operator-errors" / f"{sid}.log").write_text(traceback.format_exc())
            with self.db() as db:
                db.execute(
                    "UPDATE jobs SET status='failed',result=? WHERE owner=? AND run=? AND id=?",
                    (json.dumps({"error": {
                        "code": "candidate_preflight_failed" if phase == "candidate_preflight" else "evaluation_failed",
                        "phase": phase,
                        "message": "Candidate validation or compilation failed." if phase == "candidate_preflight" else "Evaluation could not complete; contact the operator with this submission ID.",
                        "submission_id": sid,
                        "retryable": job["attempts"] + 1 < 3,
                    }}), owner, run, sid),
                )
        finally:
            self.job_slots.release()

    def catalog(self, game, index=None):
        if game not in ARENA_GAMES or self.catalog_root is None:
            raise KeyError("Catalog unavailable")
        from aa_arena.benchmark.pool_dense import load_catalog

        path = self.catalog_root / game / "manifest.json"
        load_catalog(path)
        raw = json.loads(path.read_text())
        if index is None:
            return raw
        if not 0 <= int(index) < len(raw["trajectories"]):
            raise KeyError("Unknown catalog item")
        row = raw["trajectories"][int(index)]
        name = safe_relative(row["replay_file"])
        file = (path.parent / name).resolve()
        if not file.is_relative_to(path.parent.resolve()):
            raise ValueError("Invalid catalog path")
        b = file.read_bytes()
        if len(b) > MAX_UPLOAD:
            raise ValueError("Catalog item too large")
        return {
            "path": name,
            "sha256": hashlib.sha256(b).hexdigest(),
            "data": base64.b64encode(b).decode(),
        }

    def close(self):
        self.stop.set()
        self.thread.join()
        self.pool.shutdown(wait=True)
        self.lock.close()


def serve(store, host="127.0.0.1", port=8080):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_api("GET")

        def do_POST(self):
            self.handle_api("POST")

        def handle_api(self, method):
            self.connection.settimeout(30)
            try:
                owner = store.authenticate(self.headers.get("Authorization"))
                parts = self.path.split("/")[1:]
                if method == "POST":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length < 48 * 1024 * 1024:
                        raise ValueError("Invalid request size")
                    p = json.loads(self.rfile.read(length))
                if parts == ["v1", "health"] and method == "GET":
                    out = {"status": "ok", "games": list(ARENA_GAMES)}
                elif parts == ["v1", "runs"] and method == "POST":
                    out = store.open_run(owner, p)
                elif (
                    len(parts) == 4
                    and parts[:2] == ["v1", "runs"]
                    and parts[3] == "jobs"
                    and method == "POST"
                ):
                    out = store.submit(owner, parts[2], p)
                elif (
                    len(parts) == 5
                    and parts[:2] == ["v1", "runs"]
                    and parts[3] == "jobs"
                    and method == "GET"
                ):
                    out = store.status(owner, parts[2], parts[4])
                elif len(parts) == 3 and parts[:2] == ["v1", "catalogs"] and method == "GET":
                    out = store.catalog(parts[2])
                elif (
                    len(parts) == 5
                    and parts[:2] == ["v1", "catalogs"]
                    and parts[3] == "files"
                    and method == "GET"
                ):
                    out = store.catalog(parts[2], parts[4])
                else:
                    raise KeyError("Unknown endpoint")
                self.respond(200, out)
            except PermissionError:
                self.respond(401, {"error": "unauthorized"})
            except KeyError:
                self.respond(404, {"error": "not_found"})
            except (ValueError, TypeError, IndexError, json.JSONDecodeError):
                self.respond(400, {"error": "invalid_request_or_budget"})
            except Exception:
                self.respond(503, {"error": "evaluation_service_unavailable"})

        def respond(self, code, value):
            b = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

    class BoundedHTTPServer(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 64
        slots = threading.BoundedSemaphore(16)

        def process_request(self, request, address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, address)
            except Exception:
                self.slots.release()
                raise

        def process_request_thread(self, request, address):
            try:
                super().process_request_thread(request, address)
            finally:
                self.slots.release()

    return BoundedHTTPServer((host, port), Handler)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, required=True)
    a = p.parse_args()
    c = json.loads(a.config.read_text())
    store = EvaluationStore(
        c["state_root"],
        c["repository"],
        c["users"],
        workers=c.get("workers", 8),
        jobs=c.get("jobs", 2),
        catalog_root=c.get("catalog_root"),
    )
    for game in ARENA_GAMES:
        store.inventory(game)
    server = serve(store, c.get("host", "127.0.0.1"), c.get("port", 8080))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()

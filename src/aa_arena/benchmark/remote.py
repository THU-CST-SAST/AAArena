"""Authenticated remote evaluation; model execution and workspaces remain local."""

from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import time
import urllib.error
import urllib.parse
import urllib.request
from aa_arena.benchmark.matches import MatchInfrastructureError, MatchService, Opponent
from aa_arena.core.registry import get_plugin
from aa_arena.elo.model import repository_root
from aa_arena.io import atomic_write_json, canonical_hash

MAX_FILES = 4096
MAX_UPLOAD = 32 * 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024


def public_distribution(root: Path | None = None) -> bool:
    p = (root or repository_root()) / "configs/distribution.json"
    return p.is_file() and json.loads(p.read_text()).get("formal_evaluation") == "remote-required"


def safe_relative(name: str) -> str:
    p = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or p.is_absolute()
        or any(x in {".", "..", ""} for x in name.split("/"))
    ):
        raise ValueError("Invalid relative file path")
    return p.as_posix()


def pack_strategy(root: Path) -> list[dict]:
    root = Path(root).resolve()
    rows = []
    size = 0
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            raise ValueError("Strategy symlinks are not accepted")
        if p.is_dir():
            continue
        if not p.is_file() or p.stat().st_nlink != 1:
            raise ValueError("Strategy must contain regular independent files")
        size += p.stat().st_size
        if size > MAX_UPLOAD or len(rows) >= MAX_FILES:
            raise ValueError("Strategy upload exceeds limits")
        b = p.read_bytes()
        rows.append(
            {
                "path": safe_relative(p.relative_to(root).as_posix()),
                "data": base64.b64encode(b).decode(),
                "executable": bool(p.stat().st_mode & 0o111),
            }
        )
    if not rows:
        raise ValueError("Empty strategy")
    return rows


def unpack_strategy(rows: list, root: Path) -> None:
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_FILES:
        raise ValueError("Invalid strategy file count")
    seen = set()
    size = 0
    validated = []
    for row in rows:
        name = safe_relative(row["path"])
        if name in seen:
            raise ValueError("Duplicate strategy path")
        seen.add(name)
        b = base64.b64decode(row["data"], validate=True)
        size += len(b)
        if size > MAX_UPLOAD:
            raise ValueError("Strategy upload exceeds limits")
        validated.append((name, b, bool(row.get("executable"))))
    root.mkdir(parents=True, exist_ok=False)
    for name, b, executable in validated:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b)
        p.chmod(0o700 if executable else 0o600)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Evaluation API redirects are forbidden")


class EvaluationClient:
    def __init__(self, base: str | None = None, token: str | None = None):
        self.base = (base or os.environ.get("AA_ARENA_EVAL_URL", "https://101.42.12.204")).rstrip("/")
        self.token = token or os.environ.get("AA_ARENA_EVAL_TOKEN", "")
        u = urllib.parse.urlsplit(self.base)
        if not self.base or not self.token:
            raise ValueError(
                "Formal matches require AA_ARENA_EVAL_URL and AA_ARENA_EVAL_TOKEN; use local practice for published opponents"
            )
        if (
            u.username
            or u.password
            or u.query
            or u.fragment
            or (
                u.scheme != "https"
                and not (u.scheme == "http" and u.hostname in {"127.0.0.1", "localhost", "::1"})
            )
        ):
            raise ValueError(
                "Evaluation API requires HTTPS (HTTP is allowed only on loopback for tests)"
            )
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method: str, path: str, value=None):
        data = None if value is None else json.dumps(value, separators=(",", ":")).encode()
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        # Both run creation and submission use immutable IDs. A bounded retry
        # sends identical bytes, so a lost response cannot double-charge budget.
        for attempt in range(5):
            try:
                with self.opener.open(req, timeout=60) as r:
                    body = r.read(MAX_RESPONSE + 1)
                    if len(body) > MAX_RESPONSE:
                        raise ValueError("Oversized evaluation response")
                    return json.loads(body)
            except urllib.error.HTTPError as exc:
                retry = exc.code in {429, 502, 503, 504}
                if not retry or attempt == 4:
                    raise MatchInfrastructureError(
                        f"Evaluation API HTTP {exc.code}; recover using the same run/submission IDs"
                    ) from None
                exc.close()
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 4:
                    raise MatchInfrastructureError(
                        "Evaluation API unavailable; no local evaluation fallback"
                    ) from None
            time.sleep(min(2 ** attempt, 4))
        raise AssertionError("unreachable")


class RemoteMatchService(MatchService):
    def __init__(
        self,
        game,
        run_root,
        *,
        workers=16,
        seed=20260831,
        pool_snapshot=None,
        pool_leaderboard=None,
        run_id,
        small_budget,
        large_budget,
        experiment,
        **kwargs,
    ):
        self.game = game
        self.run_root = Path(run_root).resolve()
        self.repository = repository_root()
        self.plugin = get_plugin(game, self.repository / "games")
        self.roles = tuple(self.plugin.roles)
        self.workers = max(1, workers)
        self.seed = seed
        self.infrastructure_retries = 0
        self.hidden_root = self.run_root / "controller/matches"
        self.build_root = self.run_root / "controller/build"
        self.client = EvaluationClient()
        self.run_id = run_id
        self.prefix = "/v1/runs/" + urllib.parse.quote(run_id, safe="")
        result = self.client.request(
            "POST",
            "/v1/runs",
            {
                "run_id": run_id,
                "game": game,
                "small_total": small_budget,
                "large_total": large_budget,
                "seed": seed,
                "experiment": experiment,
            },
        )
        rows = result["opponents"]
        if pool_leaderboard:
            expected = json.loads(Path(pool_leaderboard).read_text())["opponents"]
            keys = ("opponent_id", "rank", "elo", "track")
            if [{k: r.get(k) for k in keys} for r in rows] != [
                {k: r.get(k) for k in keys} for r in expected
            ]:
                raise ValueError("Evaluation server leaderboard does not match this release")
        self.opponents = tuple(
            Opponent(
                r["opponent_id"],
                float(r["elo"]),
                int(r["rank"]),
                Path("/remote-only") / r["opponent_id"],
                r.get("track"),
            )
            for r in rows
        )
        self.by_id = {r.opponent_id: r for r in self.opponents}
        record = {
            "schema_version": 3,
            "game": game,
            "transport": "remote",
            "pool_sha256": result["pool_sha256"],
            "opponents": rows,
        }
        if pool_snapshot:
            p = Path(pool_snapshot)
            if p.exists() and json.loads(p.read_text()) != record:
                raise ValueError("Remote pool identity changed")
            atomic_write_json(p, record)
        self.pool_sha256 = result["pool_sha256"]

    def preflight_candidate(self, strategy_root: Path) -> None:
        # Reject transport-invalid candidates before the local ledger reserves
        # a budget unit and freezes an unrecoverable oversized snapshot.
        pack_strategy(strategy_root)
        super().preflight_candidate(strategy_root)

    def submit(
        self,
        strategy_root,
        submission_id,
        kind,
        *,
        opponent_ids=(),
        feedback="detailed",
        seed=None,
        allow_repeats=False,
        visible_replays=None,
    ):
        payload = {
            "submission_id": submission_id,
            "kind": kind,
            "files": pack_strategy(Path(strategy_root)),
            "opponent_ids": list(opponent_ids),
            "feedback": feedback,
            "seed": self.seed if seed is None else seed,
            "allow_repeats": allow_repeats,
        }
        job = self.client.request("POST", self.prefix + "/jobs", payload)
        deadline = time.monotonic() + float(os.environ.get("AA_ARENA_EVAL_WAIT_SECONDS", "43200"))
        while job["status"] in {"queued", "running"}:
            if time.monotonic() > deadline:
                raise MatchInfrastructureError(
                    "Evaluation still pending; resume this run to retrieve the same job"
                )
            time.sleep(1)
            job = self.client.request("GET", self.prefix + "/jobs/" + submission_id)
        if job["status"] != "complete":
            raise MatchInfrastructureError(
                "Remote evaluation failed; resume the same submission after the operator resolves the failure"
            )
        result = job["result"]
        artifacts = job.get("artifacts", [])
        receipt = job["receipt"]
        if (
            receipt["pool_sha256"] != self.pool_sha256
            or receipt["request_sha256"] != canonical_hash(payload)
            or receipt["result_sha256"] != canonical_hash(result)
        ):
            raise ValueError("Evaluation receipt integrity mismatch")
        if feedback == "binary" and artifacts:
            raise ValueError("Binary response contains detailed artifacts")
        root = Path(visible_replays) / submission_id if visible_replays else None
        allowed_paths = {}
        total = 0
        for artifact in artifacts:
            name = safe_relative(artifact["path"])
            body = base64.b64decode(artifact["data"], validate=True)
            total += len(body)
            if total > MAX_UPLOAD or root is None or Path(name).suffix not in {".json", ".md"}:
                raise ValueError("Invalid replay artifact")
            p = root / name
            if not p.resolve().is_relative_to(root.resolve()):
                raise ValueError("Replay path escaped output directory")
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.is_symlink():
                raise ValueError("Replay destination is a symlink")
            if hashlib.sha256(body).hexdigest() != artifact["sha256"]:
                raise ValueError("Replay checksum mismatch")
            p.write_bytes(body)
            allowed_paths[name] = str(p)
        for seat in result.get("seats", []):
            for key in ("replay_path", "narration_path"):
                name = seat.get(key)
                if name:
                    seat[key] = allowed_paths.get(name)
        result["evaluation_receipt"] = job["receipt"]
        return result

    def small_match(
        self,
        strategy_root,
        opponent_ids,
        submission_id,
        visible_replays,
        *,
        feedback="detailed",
        seed=None,
        allow_repeats=False,
    ):
        self.validate_opponents(opponent_ids, allow_repeats=allow_repeats)
        return self.submit(
            strategy_root,
            submission_id,
            "small",
            opponent_ids=opponent_ids,
            feedback=feedback,
            seed=seed,
            allow_repeats=allow_repeats,
            visible_replays=visible_replays,
        )

    def large_match(self, strategy_root, submission_id):
        return self.submit(strategy_root, submission_id, "large")

    def baseline_match(self, strategy_root, submission_id):
        return self.submit(strategy_root, submission_id, "baseline")


def download_catalog(game: str, destination: Path) -> Path:
    """Download replay-only off-policy data; no player-source endpoint exists."""
    client = EvaluationClient()
    value = client.request("GET", "/v1/catalogs/" + game)
    if value.get("game") != game:
        raise ValueError("Catalog game mismatch")
    destination.mkdir(parents=True, exist_ok=True)
    for i, row in enumerate(value["trajectories"]):
        name = safe_relative(row["replay_file"])
        if name.split("/")[0] not in {"replays", "public-replays"}:
            raise ValueError("Invalid catalog replay")
        item = client.request("GET", f"/v1/catalogs/{game}/files/{i}")
        if item["path"] != name:
            raise ValueError("Catalog item mismatch")
        body = base64.b64decode(item["data"], validate=True)
        if len(body) > MAX_UPLOAD or hashlib.sha256(body).hexdigest() != item["sha256"]:
            raise ValueError("Catalog replay checksum mismatch")
        target = destination / name
        if not target.resolve().is_relative_to(destination.resolve()) or target.is_symlink():
            raise ValueError("Unsafe catalog destination")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    path = destination / "manifest.json"
    atomic_write_json(path, value)
    from aa_arena.benchmark.pool_dense import load_catalog

    load_catalog(path)
    return path

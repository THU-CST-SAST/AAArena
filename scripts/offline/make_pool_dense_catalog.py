"""Build offpolicy-dense-v1 catalog: human pool vs human pool compact replays."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import copy
from dataclasses import asdict
from pathlib import Path

from aa_arena.benchmark.matches import MatchService
from aa_arena.core.cpp_build import tree_sha256
from aa_arena.io import sha256_file
from aa_arena.benchmark.pool_dense import load_catalog
from aa_arena.replay.public_json import MAX_SOURCE_REPLAY_BYTES, compact_replay

from capacity import auxiliary_capacity
from aa_arena.io import sha256_file as digest, atomic_write_json as write


def trajectory_id(game: str, a_id: str, b_id: str, assignment_index: int) -> str:
    return hashlib.sha256(f"{game}:{a_id}:{b_id}:{assignment_index}".encode()).hexdigest()[:16]


def plan_pairs(service: MatchService, *, match_base_seed: int) -> list[dict]:
    rows: list[dict] = []
    opponents = sorted(service.opponents, key=lambda o: (o.rank, o.opponent_id))
    for left in opponents:
        for right in opponents:
            if left.opponent_id == right.opponent_id:
                continue
            if left.rank > right.rank:
                continue
            assignments = service._assignments(right)
            for assignment_index, roles in enumerate(assignments):
                if left.track and tuple(roles) != (left.track,):
                    continue
                seed = int.from_bytes(
                    hashlib.sha256(
                        f"{match_base_seed}:{service.game}:{left.opponent_id}:{right.opponent_id}:{assignment_index}".encode()
                    ).digest()[:4],
                    "big",
                ) & 0x7FFFFFFF
                rows.append(
                    {
                        "trajectory_id": trajectory_id(
                            service.game, left.opponent_id, right.opponent_id, assignment_index
                        ),
                        "rank_a": left.rank,
                        "rank_b": right.rank,
                        "opponent_a_id": left.opponent_id,
                        "opponent_b_id": right.opponent_id,
                        "roles_a": list(roles),
                        "assignment_index": assignment_index,
                        "seed": seed,
                    }
                )
    return rows


def build(game: str, root: Path, *, match_base_seed: int = 42) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "manifest.json"
    forced_workers = int(os.environ.get("OFFPOLICY_CATALOG_WORKERS", "0") or 0)

    with (root / "catalog.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if manifest.exists():
            data = load_catalog(manifest)
            if data.get("game") != game or data.get("match_base_seed") != match_base_seed:
                raise ValueError("Existing catalog game or match seed differs")
            if data.get("pool_sha256") != digest(root / "pool.json"):
                raise ValueError("Existing catalog pool hash differs")
            if data.get("generation_plan_sha256") != digest(root / "generation-plan.json"):
                raise ValueError("Existing catalog generation plan hash differs")
            return
        pool = root / "pool.json"
        if not pool.exists():
            raise FileNotFoundError(f"missing pool.json in {root}")
        service = MatchService(
            game,
            root / "generation",
            workers=1,
            seed=match_base_seed,
            pool_snapshot=pool,
        )
        pairs = plan_pairs(service, match_base_seed=match_base_seed)
        plan_path = root / "generation-plan.json"
        write(plan_path, {"pairs": pairs, "match_base_seed": match_base_seed, "game": game})
        trajectories: list[dict] = []

        seat_timeout = int(os.environ.get("OFFPOLICY_CATALOG_SEAT_TIMEOUT", "900"))

        def row_from_replay(spec: dict) -> dict:
            replay_rel = Path("replays") / f"{spec['trajectory_id']}.json"
            replay_path = root / replay_rel
            replay_doc = json.loads(replay_path.read_text())
            if replay_doc.get("replay_omitted"):
                raise ValueError(f"omitted replay for {spec['trajectory_id']}")
            status = replay_doc.get("episode_status")
            if not status:
                status = "game_error" if replay_doc.get("winner") is None else "complete"
            return {
                **spec,
                "replay_file": replay_rel.as_posix(),
                "replay_sha256": sha256_file(replay_path),
                "recorded_outcome_a": replay_doc.get("outcome_a"),
                "recorded_rounds": replay_doc.get("rounds"),
                "episode_status": status,
            }

        def replay_ready(spec: dict) -> bool:
            replay_path = root / "replays" / f"{spec['trajectory_id']}.json"
            if not replay_path.exists():
                return False
            try:
                replay_doc = json.loads(replay_path.read_text())
            except json.JSONDecodeError:
                return False
            return not replay_doc.get("replay_omitted")

        def task(spec: dict) -> dict:
            replay_rel = Path("replays") / f"{spec['trajectory_id']}.json"
            replay_path = root / replay_rel
            resultfile = root / "private-results" / f"{spec['trajectory_id']}.json"
            seat = None
            if resultfile.exists():
                seat = json.loads(resultfile.read_text())
                # Transient match infra failures should be retried, not fatal for the whole catalog.
                if seat.get("status") == "infra_error":
                    seat = None
            if seat is None:
                runner = copy(service)
                runner._admission_kind = "small"
                runner.seed = spec["seed"]
                left = service.by_id[spec["opponent_a_id"]]
                right = service.by_id[spec["opponent_b_id"]]
                runner._checkpoint_strategy_sha = tree_sha256(left.package_root)
                seat = asdict(
                    runner._evaluate_seat(
                        left.package_root,
                        right,
                        tuple(spec["roles_a"]),
                        spec["assignment_index"],
                        f"pool-dense-{spec['trajectory_id']}",
                    )
                )
                write(resultfile, seat)
            if seat["status"] not in ("complete", "game_error") or not seat.get("replay_path"):
                raise ValueError(f"invalid seat for {spec['trajectory_id']}: {seat.get('status')}")
            source = Path(seat["replay_path"])
            if source.stat().st_size > MAX_SOURCE_REPLAY_BYTES:
                raise ValueError(f"oversize replay for {spec['trajectory_id']}")
            replay_path.parent.mkdir(parents=True, exist_ok=True)
            if not replay_path.exists():
                compact_replay(game, source, replay_path)
            replay_doc = json.loads(replay_path.read_text())
            if replay_doc.get("replay_omitted"):
                raise ValueError(f"omitted replay for {spec['trajectory_id']}")
            row = {
                **spec,
                "replay_file": replay_rel.as_posix(),
                "replay_sha256": sha256_file(replay_path),
                "recorded_outcome_a": seat.get("outcome"),
                "recorded_rounds": seat.get("rounds"),
                "episode_status": seat.get("status"),
            }
            return row

        def run_pairs(workers: int) -> None:
            pending = [spec for spec in pairs if not replay_ready(spec)]
            done_on_disk = len(pairs) - len(pending)
            write(
                root / "progress.json",
                {"completed": done_on_disk, "total": len(pairs), "at": time.time()},
            )
            if pending:
                with ThreadPoolExecutor(max_workers=workers) as pool_exec:
                    futures = {pool_exec.submit(task, spec): spec for spec in pending}
                    finished = 0
                    for fut in as_completed(futures):
                        spec = futures[fut]
                        try:
                            fut.result(timeout=seat_timeout)
                        except Exception as exc:
                            raise ValueError(
                                f"catalog seat failed for {spec['trajectory_id']}: {exc}"
                            ) from exc
                        finished += 1
                        write(
                            root / "progress.json",
                            {
                                "completed": done_on_disk + finished,
                                "total": len(pairs),
                                "at": time.time(),
                            },
                        )
            # Build manifest rows (hash replays in parallel; skip per-pair preload scan).
            with ThreadPoolExecutor(max_workers=workers) as pool_exec:
                for row in pool_exec.map(row_from_replay, pairs):
                    trajectories.append(row)
            write(
                root / "progress.json",
                {"completed": len(pairs), "total": len(pairs), "at": time.time()},
            )

        if forced_workers > 0:
            run_pairs(forced_workers)
        else:
            with auxiliary_capacity() as workers:
                run_pairs(workers)
        payload = {
            "protocol": "offpolicy-dense-v1",
            "game": game,
            "pool_sha256": digest(pool),
            "match_base_seed": match_base_seed,
            "generation_plan_sha256": digest(plan_path),
            "trajectories": sorted(trajectories, key=lambda r: (r["rank_a"], r["rank_b"], r["trajectory_id"])),
            "created": time.time(),
        }
        write(manifest, payload)
        manifest.chmod(0o444)
        print(
            json.dumps(
                {
                    "game": game,
                    "trajectories": len(trajectories),
                    "manifest_sha256": digest(manifest),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--game",
        required=True,
        choices=(
            "pacman",
            "antwar",
            "miracle",
            "generals",
            "lota",
            "dorado",
            "lostspace",
            "snakego",
            "antwar2",
            "rollman",
            "monecraft",
            "aquawar",
        ),
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--match-base-seed", type=int, default=42)
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    pool_dst = out / "pool.json"
    if pool_dst.exists() and digest(pool_dst) != digest(Path(args.pool)):
        parser.error("Existing catalog pool differs from --pool")
    if not pool_dst.exists():
        pool_dst.write_bytes(Path(args.pool).read_bytes())
    build(args.game, out, match_base_seed=args.match_base_seed)

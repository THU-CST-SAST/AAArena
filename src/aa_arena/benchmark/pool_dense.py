"""Frozen human-pool dense trajectory catalog for off-policy observation runs."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from aa_arena.io import sha256_file
from aa_arena.replay import narrate


def load_catalog(path: Path) -> dict[str, Any]:
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("protocol") != "offpolicy-dense-v1":
        raise ValueError(f"{path}: expected protocol offpolicy-dense-v1")
    trajectories = data.get("trajectories")
    if not isinstance(trajectories, list) or not trajectories:
        raise ValueError(f"{path}: trajectories must be a non-empty list")
    root = path.parent
    by_id: dict[str, dict[str, Any]] = {}
    for row in trajectories:
        tid = row.get("trajectory_id")
        if not isinstance(tid, str) or not tid:
            raise ValueError("trajectory_id required")
        if tid in by_id:
            raise ValueError(f"duplicate trajectory_id {tid}")
        replay = row.get("replay_file")
        if not isinstance(replay, str):
            raise ValueError(f"{tid}: replay_file required")
        replay_path = (root / replay).resolve()
        if not replay_path.is_relative_to(root) or not replay_path.is_file():
            raise ValueError(f"{tid}: missing replay file")
        expected = row.get("replay_sha256")
        if isinstance(expected, str) and sha256_file(replay_path) != expected:
            raise ValueError(f"{tid}: replay hash mismatch")
        by_id[tid] = {**row, "_replay_path": replay_path}
    data["_by_id"] = by_id
    data["_root"] = root
    return data


def list_trajectories(
    catalog: dict[str, Any],
    *,
    rank_min: int = 1,
    rank_max: int | None = None,
    opponent_id: str | None = None,
    limit: int = 32,
    include_viewed: bool = False,
    viewed_ids: set[str] | None = None,
) -> dict[str, Any]:
    viewed = viewed_ids or set()
    low = max(1, int(rank_min))
    high = int(rank_max) if rank_max is not None else 10_000
    limit = max(1, min(int(limit), 128))
    rows = []
    for row in catalog["trajectories"]:
        tid = row["trajectory_id"]
        if not include_viewed and tid in viewed:
            continue
        ra, rb = int(row["rank_a"]), int(row["rank_b"])
        if ra < low or rb < low or ra > high or rb > high:
            continue
        if opponent_id and opponent_id not in (row["opponent_a_id"], row["opponent_b_id"]):
            continue
        rows.append(
            {
                "trajectory_id": tid,
                "rank_a": ra,
                "rank_b": rb,
                "opponent_a_id": row["opponent_a_id"],
                "opponent_b_id": row["opponent_b_id"],
                "roles_a": row.get("roles_a"),
                "recorded_outcome_a": row.get("recorded_outcome_a"),
                "recorded_rounds": row.get("recorded_rounds"),
                "already_viewed": tid in viewed,
            }
        )
        if len(rows) >= limit:
            break
    return {
        "kind": "pool_trajectory_list",
        "game": catalog.get("game"),
        "total_in_catalog": len(catalog["trajectories"]),
        "matching": len(rows),
        "trajectories": rows,
    }


def materialize_view(
    catalog: dict[str, Any],
    trajectory_id: str,
    workspace_replays: Path,
) -> dict[str, Any]:
    row = catalog["_by_id"].get(trajectory_id)
    if row is None:
        raise ValueError(f"unknown trajectory_id {trajectory_id!r}")
    pool_dir = workspace_replays / "pool_observations"
    pool_dir.mkdir(parents=True, exist_ok=True)
    dest_json = pool_dir / f"{trajectory_id}.json"
    dest_md = pool_dir / f"{trajectory_id}.md"
    if not dest_json.exists():
        shutil.copyfile(row["_replay_path"], dest_json)
    if not dest_md.exists():
        game = str(catalog["game"])
        dest_md.write_text(narrate(game, dest_json, detail="full").text, encoding="utf-8")
    match_id = hashlib.sha256(f"view:{trajectory_id}".encode()).hexdigest()[:32]
    return {
        "kind": "trajectory_view",
        "match_id": match_id,
        "trajectory_id": trajectory_id,
        "rank_a": row["rank_a"],
        "rank_b": row["rank_b"],
        "opponent_a_id": row["opponent_a_id"],
        "opponent_b_id": row["opponent_b_id"],
        "roles_a": row.get("roles_a"),
        "recorded_outcome_a": row.get("recorded_outcome_a"),
        "recorded_rounds": row.get("recorded_rounds"),
        "replay_json": dest_json.relative_to(workspace_replays.parent).as_posix(),
        "replay_md": dest_md.relative_to(workspace_replays.parent).as_posix(),
        "note": "Human-pool vs human-pool observation; not your own challenger matches.",
    }


def new_view_submission_id() -> str:
    return uuid.uuid4().hex

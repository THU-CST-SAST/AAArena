#!/usr/bin/env python3
"""Standalone public replay translator copied into each resource bundle."""

from __future__ import annotations

import argparse
import json
import struct
import re
import zipfile
from pathlib import Path
from typing import Any


AI9_GAMES = {"dorado", "lota", "monecraft", "pacman"}


def _game() -> str:
    manifest = Path(__file__).resolve().parent.parent / "manifest.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8"))["game"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return "unknown"


def _frames(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("frames", "rounds", "logs", "replay", "data"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
        return [value]
    return []


def _load(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as whole_error:
        rows = []
        try:
            for line in text.splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        except json.JSONDecodeError:
            raise whole_error
        if not rows:
            raise whole_error
        return rows


def _load_ai9(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = [name for name in archive.namelist() if name.lower().endswith(".txt")]
            if not members:
                raise ValueError("AI9 replay zip contains no text replay")
            text = archive.read(members[0]).decode("utf-8", errors="replace")
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        document = None
    timeline = []
    if isinstance(document, dict):
        frames = document.get('rounds-info', document.get('breadcrumbs', []))
        if not isinstance(frames, list):
            raise ValueError('AI9 JSON replay has no round timeline')
        timeline = [
            {'round': frame.get('rounds', index) if isinstance(frame, dict) else index,
             'state': frame}
            for index, frame in enumerate(frames)
        ]
        terminal = document.get('result')
        if isinstance(terminal, dict):
            winner = terminal.get('winner')
        elif isinstance(terminal, (int, float)):
            winner = 0 if terminal > 0 else 1 if terminal < 0 else -2
        else:
            winner = document.get('winner')
    else:
        markers = list(re.finditer(r'^(?:Round|round)\s*:\s*(\d+)\s*$', text, re.M))
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
            timeline.append({'round': int(marker.group(1)),
                             'state': text[marker.end():end].strip()})
        winner_match = re.search(r'^winner:(-?\d+)\s*$', text, re.M)
        winner = int(winner_match.group(1)) if winner_match else None
    if not timeline:
        raise ValueError('AI9 replay contains no round records')
    selected = timeline if len(timeline) <= 3 else [timeline[0], timeline[len(timeline)//2], timeline[-1]]
    return {
        "rounds": len(timeline),
        "round_numbers": {
            "first": timeline[0]['round'],
            "last": timeline[-1]['round'],
        },
        "winner": winner,
        "timeline": selected,
        "omitted_rounds": len(timeline) - len(selected),
        "reconstruction": "first, middle and final original round records; intermediate rounds omitted",
    }


def _player_counts(rows: Any, *, alive_only: bool = False) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(rows, list):
        return counts
    for row in rows:
        if not isinstance(row, dict) or (alive_only and row.get("status", 0) != 0):
            continue
        player = str(row.get("player", "unknown"))
        counts[player] = counts.get(player, 0) + 1
    return counts


def _antwar(value: Any, detail: str) -> dict[str, Any]:
    frames = _frames(value)
    towers: dict[str, dict[str, Any]] = {}
    timeline = []
    seed = None
    for index, frame in enumerate(frames, start=1):
        if not isinstance(frame, dict):
            continue
        if seed is None and frame.get("seed") is not None:
            seed = frame.get("seed")
        state = frame.get("round_state")
        if not isinstance(state, dict):
            state = frame
        deltas = state.get("towers")
        if isinstance(deltas, list):
            for tower in deltas:
                if not isinstance(tower, dict) or tower.get("id") is None:
                    continue
                tower_id = str(tower["id"])
                # Saiblo uses type=-1 as an incremental deletion record.
                if tower.get("type") == -1:
                    towers.pop(tower_id, None)
                else:
                    towers[tower_id] = tower
        row = {
            "round": index,
            "coins": state.get("coins"),
            "camps": state.get("camps"),
            "winner": state.get("winner"),
            "tower_count": _player_counts(list(towers.values())),
            "ants_alive": _player_counts(state.get("ants"), alive_only=True),
            "operations": {
                "P0": len(frame.get("op0") or []),
                "P1": len(frame.get("op1") or []),
            },
        }
        if state.get("error"):
            row["error"] = state["error"]
        timeline.append(row)
    selected = timeline
    if detail == "digest" and len(timeline) > 3:
        selected = [timeline[0], timeline[len(timeline) // 2], timeline[-1]]
    return {
        "game": "antwar",
        "rounds": len(frames),
        "seed": seed,
        "final": timeline[-1] if timeline else None,
        "timeline": selected,
        "reconstruction": "towers are accumulated by id; type=-1 deletes a tower",
    }


def _generic(game: str, value: Any, detail: str) -> dict[str, Any]:
    frames = _frames(value)
    selected = frames
    if detail == "digest" and len(frames) > 3:
        selected = [frames[0], frames[len(frames) // 2], frames[-1]]
    summaries = []
    for index, frame in enumerate(selected):
        if isinstance(frame, dict):
            summaries.append(
                {
                    "sample": index,
                    "fields": sorted(str(key) for key in frame),
                    "state": frame,
                }
            )
        else:
            summaries.append({"sample": index, "state": frame})
    return {"game": game, "rounds": len(frames), "samples": summaries}


def _miracle(path: Path, detail: str) -> dict[str, Any]:
    names = (
        "", "TurnStart", "TurnEnd", "Spawn", "Move", "Attack", "Damage", "Death",
        "Heal", "ActivateArtifact", "GameEnd", "GameStart", "BuffAdd", "BuffRemove",
        "Attacking", "Attacked", "Leave", "Arrive", "Summon",
    )
    raw = path.read_bytes()
    if len(raw) < 28:
        raise ValueError("Miracle replay is shorter than its 7-int header")
    count = len(raw) // 4
    integers = struct.unpack(">" + "i" * count, raw[: count * 4])
    header = integers[:7]
    records = []
    tally: dict[str, int] = {}
    winner = None
    rounds = 0
    for offset in range(7, len(integers) - 6, 7):
        chunk = integers[offset : offset + 7]
        round_index, event = int(chunk[0]), int(chunk[1])
        name = names[event] if 0 <= event < len(names) else f"Unknown({event})"
        row = {"round": round_index, "event": event, "name": name, "payload": list(chunk[2:])}
        records.append(row)
        tally[name] = tally.get(name, 0) + 1
        rounds = max(rounds, round_index)
        if event == 10:
            winner = int(chunk[2])
    selected = records
    if detail == "digest" and len(records) > 80:
        selected = records[:30] + records[-50:]
    return {
        "game": "miracle",
        "map_type": int(header[3]),
        "day_time": int(header[4]),
        "rounds": rounds,
        "winner": winner,
        "record_count": len(records),
        "event_tally": tally,
        "timeline": selected,
        "reconstruction": "big-endian int32 records, seven integers per record",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Translate a public AA-Arena replay")
    parser.add_argument("replay")
    parser.add_argument("--perspective")
    parser.add_argument("--detail", choices=("digest", "full"), default="digest")
    args = parser.parse_args()
    path = Path(args.replay)
    game = _game()
    if game == "miracle":
        translated = _miracle(path, args.detail)
    elif game in AI9_GAMES:
        translated = _load_ai9(path)
    else:
        value = _load(path)
        translated = (
            _antwar(value, args.detail)
            if game == "antwar"
            else _generic(game, value, args.detail)
        )
    translated["perspective"] = args.perspective
    print(f"# {game} replay translation")
    print("\n```json")
    print(json.dumps(translated, ensure_ascii=False, indent=2, sort_keys=True))
    print("```")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

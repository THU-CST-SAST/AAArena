"""Bounded, model-facing replay JSON.

Saiblo can emit very large renderer/debug payloads (and Miracle emits a binary
media stream).  The benchmark must never copy those files into the agent
workspace.  This module keeps the per-round action/state information while
dropping renderer-only payloads and bounding nested collections.
"""

from __future__ import annotations

import json
import re
import struct
import zipfile
from pathlib import Path
from typing import Any


MAX_PUBLIC_REPLAY_BYTES = 256 * 1024
MAX_SOURCE_REPLAY_BYTES = 64 * 1024 * 1024
MAX_LIST_ITEMS = 256
AI9_GAMES = frozenset({"dorado", "lota", "monecraft", "pacman"})


def _compact(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > 512:
            return value[:512] + "…"
        return value
    if depth > 8:
        return "<nested>"
    if isinstance(value, dict):
        return {str(key): _compact(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        values = list(value)
        if len(values) > MAX_LIST_ITEMS:
            values = values[:MAX_LIST_ITEMS]
            return [_compact(item, depth=depth + 1) for item in values] + [
                {"_truncated_items": len(list(value)) - MAX_LIST_ITEMS}
            ]
        return [_compact(item, depth=depth + 1) for item in values]
    return str(value)


def _load_text(path: Path, *, game: str | None = None) -> Any:
    text = path.read_text(encoding="utf-8", errors="strict")
    # These backends emit JSONL; each record is part of the public timeline.
    if game in {"generals", "rollman"}:
        rows = []
        traceback_start: int | None = None
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if game == "generals" and stripped == "Traceback (most recent call last):":
                if traceback_start is not None:
                    raise ValueError("incomplete Generals backend traceback")
                traceback_start = line_number
                continue
            if traceback_start is not None:
                # Generals writes backend exception traces between JSON records.
                # Preserve the diagnostic's location/type, never private paths,
                # evaluator source, or exception messages in the public replay.
                terminal = re.fullmatch(
                    r"([A-Za-z_][\w.]*(?:Error|Exception))(?::.*)?", stripped
                )
                if terminal:
                    rows.append({"_replay_diagnostic": {
                        "kind": "python_traceback",
                        "exception_type": terminal.group(1)[:128],
                        "source_lines": [traceback_start, line_number],
                        "details_omitted": True,
                    }})
                    traceback_start = None
                elif not line[0].isspace():
                    raise ValueError("incomplete Generals backend traceback")
                continue
            rows.append(json.loads(line))
        if traceback_start is not None:
            raise ValueError("incomplete Generals backend traceback")
        if not rows:
            raise ValueError("replay contains no JSON records")
        return rows
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Only a complete replay ARRAY may have a trailing renderer document.
        # Never silently discard additional JSON objects in a record stream.
        value, end = json.JSONDecoder().raw_decode(text.lstrip())
        if isinstance(value, list):
            return value
        raise ValueError(f"unexpected trailing replay data after byte {end}")


def _miracle(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) < 28:
        raise ValueError("Miracle replay is shorter than its header")
    count = len(raw) // 4
    ints = struct.unpack(">" + "i" * count, raw[: count * 4])
    names = (
        "", "TurnStart", "TurnEnd", "Spawn", "Move", "Attack", "Damage", "Death",
        "Heal", "ActivateArtifact", "GameEnd", "GameStart", "BuffAdd", "BuffRemove",
        "Attacking", "Attacked", "Leave", "Arrive", "Summon",
    )
    rounds: dict[int, list[dict[str, Any]]] = {}
    for offset in range(7, len(ints) - 6, 7):
        chunk = ints[offset : offset + 7]
        round_no, event = int(chunk[0]), int(chunk[1])
        rounds.setdefault(round_no, []).append(
            {
                "event": event,
                "event_name": names[event] if 0 <= event < len(names) else f"Unknown({event})",
                "payload": [int(item) for item in chunk[2:]],
            }
        )
    return {
        "schema_version": 1,
        "format": "miracle-int32-events",
        "header": [int(item) for item in ints[:7]],
        "rounds": [
            {"round": round_no, "events": events}
            for round_no, events in sorted(rounds.items())
        ],
    }


def _ai9_winner(text: str) -> int | None:
    matches = list(re.finditer(r"^(?:winner|Winner)\s*:\s*(-?\d+)\s*$", text, re.M))
    if matches:
        return int(matches[-1].group(1))
    matches = list(re.finditer(r"^win player id\s*:\s*(-?\d+)\s*$", text, re.M | re.I))
    return int(matches[-1].group(1)) if matches else None


def _ai9_text(text: str) -> dict[str, Any]:
    """Normalize the line-oriented replay used by most AI9 backends."""

    rounds: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        round_match = re.match(r"^(?:Round|round)\s*:\s*(\d+)$", line)
        if round_match:
            current = {
                "round": int(round_match.group(1)) + 1,
                "observations": [],
                "actions": [],
            }
            rounds.append(current)
            continue
        if current is None and line.startswith("{"):
            current = {"round": 1, "observations": [], "actions": []}
            rounds.append(current)
        action_match = re.match(r"^\[([a-z]+)\](.*)$", line)
        if action_match:
            if current is None:
                current = {"round": 1, "observations": [], "actions": []}
                rounds.append(current)
            current["actions"].append(
                {"action": action_match.group(1), "detail": action_match.group(2).strip()}
            )
            continue
        if line.startswith("{") and current is not None:
            # Keep the semicolon object syntax verbatim; compacting it into JSON
            # would hide small semantic differences between AI9 engines.
            current["observations"].append(line if len(line) <= 1024 else line[:1023] + "…")
            if len(current["observations"]) >= 24:
                current["observations"].append({"_truncated_items": True})
                current["observations"] = current["observations"][:24]
            continue
        # One source line can encode a full action batch; keep it bounded here so
        # 1000-round AI9 games cannot exceed the public replay cap.
        if current is not None and len(current["actions"]) >= 48:
            current["actions"].append({"_truncated_items": True})
            continue
        if line.lower().startswith("ai status:") or line.startswith("AI STATUS:"):
            current = current or {"round": len(rounds), "observations": [], "actions": []}
            current["ai_status"] = line.split(":", 1)[1].strip()
    return {
        "schema_version": 1,
        "format": "ai9-text-rounds",
        "rounds": [
            {
                **round_record,
                "observations": round_record["observations"][:24],
                "actions": round_record["actions"][:48],
            }
            for round_record in rounds
        ],
        "winner": _ai9_winner(text),
    }


def _ai9_json(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {
            "schema_version": 1,
            "format": "ai9-json-record",
            "rounds": [{"round": 1, "record": _compact(value)}],
        }
    rounds_info = value.get("rounds-info")
    if isinstance(rounds_info, list):
        return {
            "schema_version": 1,
            "format": "ai9-monecraft-rounds",
            "rounds": [
                {
                    "round": int(item.get("rounds", index)),
                    "actions": _compact(item.get("players", [])),
                    "state_changes": _compact(item),
                }
                for index, item in enumerate(rounds_info)
                if isinstance(item, dict)
            ],
            "result": _compact(value.get("result")),
            "winner": _compact(value.get("result", {})).get("winner")
            if isinstance(value.get("result"), dict)
            else None,
        }
    breadcrumbs = value.get("breadcrumbs")
    rounds: list[dict[str, Any]] = []
    if isinstance(breadcrumbs, list):
        for index, item in enumerate(breadcrumbs, start=1):
            if not isinstance(item, dict):
                continue
            rounds.append(
                {
                    "round": index,
                    "actions": _compact(item.get("creatures", [])),
                    "state_changes": _compact(
                        {"map": item.get("map"), "mines": item.get("mines"), "scores": item.get("scores")}
                    ),
                }
            )
    winner = value.get("result")
    if isinstance(winner, dict):
        winner = winner.get("winner")
    if isinstance(winner, (int, float)):
        numeric = int(winner)
        winner = 0 if numeric > 0 else 1 if numeric < 0 else -2
    return {
        "schema_version": 1,
        "format": "ai9-json-rounds",
        "rounds": rounds,
        "result": _compact({key: item for key, item in value.items() if key != "breadcrumbs"}),
        "winner": winner,
    }


def load_ai9_document(path: Path) -> dict[str, Any]:
    """Load a text, JSON, or zip-wrapped AI9 replay into normalized rounds."""

    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = [name for name in archive.namelist() if name.lower().endswith(".txt")]
            if not members:
                raise ValueError("AI9 replay zip contains no text replay")
            text = archive.read(members[0]).decode("utf-8", errors="replace")
        temporary_text = text.lstrip()
        if temporary_text.startswith(("{", "[")):
            try:
                return _ai9_json(json.loads(temporary_text))
            except json.JSONDecodeError:
                pass
        return _ai9_text(text)
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return _ai9_text(path.read_text(encoding="utf-8", errors="replace"))
    return _ai9_json(value)


def _normalise(game: str, value: Any) -> dict[str, Any]:
    if game in AI9_GAMES:
        return _ai9_json(value)
    if isinstance(value, dict) and game == "snakego":
        operations = value.get("operations")
        round_info = value.get("round_info")
        rounds = []
        if isinstance(operations, list):
            for index, operation in enumerate(operations):
                item: dict[str, Any] = {"round": index + 1, "actions": _compact(operation)}
                if isinstance(round_info, list) and index < len(round_info):
                    item["state_changes"] = _compact(round_info[index])
                rounds.append(item)
        return {
            "schema_version": 1,
            "format": "snakego-round-actions",
            "rounds": rounds,
            "result": _compact(value.get("end_info")),
        }
    if isinstance(value, list):
        if game == "lostspace" and len(value) >= 2:
            return {
                "schema_version": 1,
                "format": "lostspace-round-actions",
                "header": _compact(value[0]),
                "rounds": [
                    {"round": index + 1, "actions": _compact(item)}
                    for index, item in enumerate(value[1:-1])
                ],
                "result": _compact(value[-1]),
            }
        return {
            "schema_version": 1,
            "format": f"{game}-round-records",
            "rounds": [
                {"round": index + 1, "record": _compact(item)}
                for index, item in enumerate(value)
            ],
        }
    return {
        "schema_version": 1,
        "format": f"{game}-record",
        "rounds": [{"round": 1, "record": _compact(value)}],
    }


def compact_replay(game: str, source: str | Path, destination: str | Path) -> dict[str, Any]:
    """Write one bounded public JSON and return its metadata.

    The source remains in the controller's private transient directory.  If a
    game emits a renderer-sized record, only the normalized round/action view
    is persisted in the model workspace.
    """

    source_path = Path(source)
    destination_path = Path(destination)
    source_size = source_path.stat().st_size
    if source_size > MAX_SOURCE_REPLAY_BYTES:
        document = {
            "schema_version": 1,
            "format": f"{game}-replay-omitted",
            "replay_omitted": True,
            "reason": "private Saiblo replay exceeded the public size limit",
            "source_size": source_size,
            "rounds": [],
        }
        encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(encoded + b"\n")
        return {"path": str(destination_path), "size": len(encoded), "source_size": source_size}
    if game == "miracle":
        document = _miracle(source_path)
    elif game in AI9_GAMES:
        document = load_ai9_document(source_path)
    else:
        document = _normalise(game, _load_text(source_path, game=game))
    def encode() -> bytes:
        return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n"

    encoded = encode()
    original_rounds = document.get("rounds", [])
    count = len(original_rounds)
    # Preserve whole records, numeric zero/-1 and array positions. Never turn
    # a large replay into a list of bare round numbers or corrupt coordinates.
    budget = count
    while len(encoded) > MAX_PUBLIC_REPLAY_BYTES and budget > min(3, count):
        budget = max(3, budget // 2)
        indexes = sorted({0, count // 2, count - 1} | {
            round(i * (count - 1) / (budget - 1)) for i in range(budget)
        })
        document["rounds"] = [original_rounds[i] for i in indexes]
        document["bounded"] = True
        document["sampling"] = {
            "source_records": count,
            "retained_records": len(indexes),
            "source_indexes": indexes,
            "omitted_records": count - len(indexes),
            "state_reconstruction_complete": False,
        }
        encoded = encode()
    if len(encoded) > MAX_PUBLIC_REPLAY_BYTES:
        raise ValueError(f"public replay remains too large: {len(encoded)} bytes")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_bytes(encoded)
    return {"path": str(destination_path), "size": len(encoded), "source_size": source_path.stat().st_size}


def validate_public_replay(game: str, source: Path, public: Path) -> None:
    """Certification rejects error terminals, empty and silently lost timelines."""
    if game == "miracle":
        raw = _miracle(source)
        expected = raw
    elif game in AI9_GAMES:
        raw = load_ai9_document(source)
        expected = raw
    else:
        raw = _load_text(source, game=game)
        expected = _normalise(game, raw)
    if isinstance(raw, list) and any(
        isinstance(record, dict) and "_replay_diagnostic" in record for record in raw
    ):
        raise ValueError(f"{game}: certification replay contains backend diagnostic")
    document = json.loads(public.read_text())
    rounds = expected.get("rounds", [])
    observed = document.get("rounds", [])
    if not isinstance(observed, list) or not rounds or not observed or document.get("replay_omitted"):
        raise ValueError(f"{game}: certification replay has no playable records")
    if isinstance(raw, dict):
        terminal = raw.get("end_info", raw.get("result"))
        if isinstance(terminal, dict) and (
            terminal.get("type") in {"PLAYER_ERROR", "error"} or terminal.get("err")
        ):
            raise ValueError(f"{game}: certification replay reports player error: {terminal}")
    for index in {0, len(rounds) // 2, len(rounds) - 1}:
        if rounds[index] not in observed:
            raise ValueError(f"{game}: public replay lost source record {index}")
    sampling = document.get("sampling")
    if sampling:
        indexes = sampling.get("source_indexes", [])
        if sampling.get("source_records") != len(rounds) or observed != [rounds[i] for i in indexes]:
            raise ValueError(f"{game}: inconsistent replay sampling metadata")
    elif observed != rounds:
        raise ValueError(f"{game}: public replay silently lost timeline records")

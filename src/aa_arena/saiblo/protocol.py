"""Saiblo logic/AI protocol.

Ported from ``saiblo/saiblo-local-judger`` at
``a330c16e5349b300249655ae1b68094b4c1828b1``. The upstream project is MIT licensed; see
``UPSTREAM.md`` in this package. This module keeps the upstream wire objects while adding strict
validation and immutable data classes.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class RoundConfig:
    state: int
    time: int
    length: int


@dataclass(frozen=True)
class RoundInfo:
    state: int
    listen: tuple[int, ...]
    player: tuple[int, ...]
    content: tuple[str, ...]


@dataclass(frozen=True)
class GameOver:
    message: Mapping[str, Any]
    scores: tuple[Any, ...]


@dataclass(frozen=True)
class AuxiliaryMessage:
    """Platform control data (for example watch/replay updates), not an AI round."""

    message: Mapping[str, Any]


class AiErrorType(Enum):
    RUN_ERROR = (0, "runError")
    TIMEOUT_ERROR = (1, "timeOutError")
    OUTPUT_LIMIT_ERROR = (2, "outputLimitError")

    @property
    def code(self) -> int:
        return int(self.value[0])

    @property
    def label(self) -> str:
        return str(self.value[1])


def _json_frame(value: object) -> bytes:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def encode_init(player_list: Sequence[int], config: object, replay_path: Path) -> bytes:
    players = [int(value) for value in player_list]
    return _json_frame(
        {
            "player_list": players,
            "player_num": len(players),
            "config": config,
            "replay": str(replay_path),
        }
    )


def encode_ai_message(ai_id: int, content: str, elapsed_time_ms: int) -> bytes:
    return _json_frame(
        {"player": int(ai_id), "content": str(content), "time": int(elapsed_time_ms)}
    )


def encode_ai_error(
    error_ai: int,
    state: int,
    error_type: AiErrorType,
) -> bytes:
    return _json_frame(
        {
            "player": -1,
            "content": json.dumps(
                {
                    "player": int(error_ai),
                    "state": int(state),
                    "error": error_type.code,
                    "error_log": error_type.label,
                },
                separators=(",", ":"),
            ),
        }
    )


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _integer_list(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return tuple(_integer(item, f"{label} item") for item in value)


def parse_logic_message(data: bytes) -> RoundConfig | RoundInfo | GameOver | AuxiliaryMessage:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("logic data is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("logic data must be a JSON object")
    if "state" not in value:
        return AuxiliaryMessage(message=dict(value))
    state = _integer(value.get("state"), "state")

    if state == -1:
        raw_end_info = value.get("end_info")
        try:
            end_info = json.loads(raw_end_info) if isinstance(raw_end_info, str) else raw_end_info
        except json.JSONDecodeError as exc:
            raise ValueError("end_info is not valid JSON") from exc
        if not isinstance(end_info, dict):
            raise ValueError("end_info must be an object")
        scores: list[Any] = []
        for index in range(10):
            key = str(index)
            if key not in end_info:
                break
            scores.append(end_info[key])
        return GameOver(message=dict(value), scores=tuple(scores))

    if state == 0:
        return RoundConfig(
            state=state,
            time=_integer(value.get("time"), "time"),
            length=_integer(value.get("length"), "length"),
        )

    listen = _integer_list(value.get("listen"), "listen")
    players = _integer_list(value.get("player"), "player")
    raw_content = value.get("content")
    if not isinstance(raw_content, list) or not all(isinstance(item, str) for item in raw_content):
        raise ValueError("content must be a list of strings")
    if len(players) != len(raw_content):
        raise ValueError("player and content lengths differ")
    return RoundInfo(
        state=state,
        listen=listen,
        player=players,
        content=tuple(raw_content),
    )

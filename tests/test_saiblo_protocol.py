from __future__ import annotations

import json
import struct
from pathlib import Path

from aa_arena.saiblo.protocol import (
    AiErrorType,
    AuxiliaryMessage,
    GameOver,
    RoundConfig,
    RoundInfo,
    encode_ai_error,
    encode_ai_message,
    encode_init,
    parse_logic_message,
)


def _unframe(packet: bytes) -> dict[str, object]:
    size = struct.unpack(">I", packet[:4])[0]
    assert size == len(packet) - 4
    return json.loads(packet[4:].decode("utf-8"))


def test_official_init_packet_includes_player_num_and_config(tmp_path: Path) -> None:
    replay = tmp_path / "replay.json"

    message = _unframe(encode_init([1, 1, 1, 1], {"random_seed": 7}, replay))

    assert message == {
        "player_list": [1, 1, 1, 1],
        "player_num": 4,
        "config": {"random_seed": 7},
        "replay": str(replay),
    }


def test_official_normal_ai_packet_carries_elapsed_time() -> None:
    assert _unframe(encode_ai_message(1, "move", 37)) == {
        "player": 1,
        "content": "move",
        "time": 37,
    }


def test_official_ai_error_packet_uses_nested_player_minus_one_envelope() -> None:
    outer = _unframe(encode_ai_error(3, 12, AiErrorType.RUN_ERROR))

    assert outer["player"] == -1
    assert json.loads(str(outer["content"])) == {
        "player": 3,
        "state": 12,
        "error": 0,
        "error_log": "runError",
    }


def test_logic_messages_are_game_agnostic() -> None:
    config = parse_logic_message(b'{"state":0,"time":3,"length":2048}')
    round_info = parse_logic_message(
        b'{"state":5,"listen":[1],"player":[0,1],"content":["a","b"]}'
    )
    game_over = parse_logic_message(
        b'{"state":-1,"end_info":"{\\"0\\":1,\\"1\\":0}","end_state":"[]"}'
    )

    assert config == RoundConfig(state=0, time=3, length=2048)
    assert round_info == RoundInfo(state=5, listen=(1,), player=(0, 1), content=("a", "b"))
    assert game_over == GameOver(
        message={"state": -1, "end_info": '{"0":1,"1":0}', "end_state": "[]"},
        scores=(1, 0),
    )


def test_platform_auxiliary_control_message_is_not_a_round() -> None:
    message = parse_logic_message(b'{"watch":"public replay data"}')

    assert message == AuxiliaryMessage(message={"watch": "public replay data"})

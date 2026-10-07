from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SDK_ROOT = ROOT / "games" / "lostspace" / "public_sdk"
sys.path.insert(0, str(SDK_ROOT))
try:
    from lostspace_sdk import (
        Action,
        Client,
        PlayerStatus,
        TurnState,
        decode_message,
        encode_message,
    )
finally:
    sys.path.remove(str(SDK_ROOT))


def _inbound_frame(message: dict[str, object]) -> bytes:
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    return f"{len(body):04d}".encode() + body


def _outbound_messages(payload: bytes) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    stream = io.BytesIO(payload)
    while header := stream.read(4):
        size = int.from_bytes(header, "big", signed=True)
        messages.append(json.loads(stream.read(size).decode("utf-8")))
    return messages


def _turn_message() -> dict[str, object]:
    return {
        "type": "roundbegin",
        "state": 9,
        "inturn": 1,
        "status": 0,
        "hp": 160,
        "keys": [0],
        "pos": [6, 0, 1],
        "tools": {
            "LandMine": [1, 0],
            "Sticky": [0, 0],
            "Kit": 1,
            "Transport": 0,
        },
        "others": [
            {"player_id": 0, "status": 0, "hp": 200, "keys": []},
            {"player_id": 2, "status": 1, "hp": 0, "keys": [2]},
            {"player_id": 3, "status": 0, "hp": 80, "keys": []},
        ],
        "view": [[[1, 1, 1], ["Materials"]]],
    }


def test_protocol_frames_unicode_by_encoded_byte_length() -> None:
    frame = encode_message({"type": "action", "action": ["interact", "逃生舱"]})
    assert int.from_bytes(frame[:4], "big", signed=True) == len(frame[4:])
    body = json.dumps({"type": "id", "id": 2, "birth_pos": [6, 6]}).encode()
    assert decode_message(io.BytesIO(f"{len(body):04d}".encode() + body))["id"] == 2


def test_turn_state_is_copy_safe() -> None:
    raw = _turn_message()
    state = TurnState.from_message(player_id=1, spawn=(6, 0, 1), message=raw)
    raw["keys"].append(3)  # type: ignore[union-attr]
    raw["view"][0][0][0] = 99  # type: ignore[index]
    assert state.keys == (0,)
    assert state.visible_nodes[0].position == (1, 1, 1)
    assert state.status is PlayerStatus.ALIVE


@pytest.mark.parametrize(
    ("action", "wire"),
    [
        (Action.move((1, 2, 1)), ["move", [1, 2, 1]]),
        (Action.attack((1, 2, 1), 3), ["attack", [1, 2, 1], 3]),
        (Action.interact("KeyMachine"), ["interact", "KeyMachine"]),
        (Action.escape(True), ["interact", "EscapeCapsule", True]),
        (Action.collect("Materials", "Kit"), ["interact", "Materials", "Kit"]),
        (Action.trap("LandMine"), ["trap", "LandMine"]),
        (Action.tool("Transport", (3, 3, 0)), ["tool", "Transport", [3, 3, 0]]),
        (Action.detect((1, 1, 1)), ["detect", [1, 1, 1]]),
        (Action.finish(), None),
    ],
)
def test_action_wire_format(action: Action, wire: list[object] | None) -> None:
    expected = {"type": "finish"} if wire is None else {"type": "action", "action": wire}
    assert action.as_message() == expected


def test_client_passes_copy_safe_state_and_finishes_turn() -> None:
    class FinishingClient(Client):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.seen: list[TurnState] = []

        def play(self, state: TurnState) -> tuple[Action, ...]:
            self.seen.append(state)
            return (Action.finish(),)

    input_stream = io.BytesIO(
        _inbound_frame({"type": "id", "id": 1, "birth_pos": [6, 0]})
        + _inbound_frame({"type": "offround", "state": 8, "content": ["hp_update", 160]})
        + _inbound_frame(_turn_message())
    )
    output_stream = io.BytesIO()
    client = FinishingClient(input_stream=input_stream, output_stream=output_stream)
    client.run()

    assert len(client.seen) == 1
    assert client.seen[0].spawn == (6, 0, 1)
    assert _outbound_messages(output_stream.getvalue()) == [{"type": "finish"}]


def test_client_emits_multiple_actions_in_order() -> None:
    class DetectingClient(Client):
        def play(self, state: TurnState) -> tuple[Action, ...]:
            return (Action.detect((1, 1, 1)), Action.finish())

    input_stream = io.BytesIO(
        _inbound_frame({"type": "id", "id": 1, "birth_pos": [6, 0]})
        + _inbound_frame(_turn_message())
        + _inbound_frame({"type": "action", "success": True, "has_trap": False})
    )
    output_stream = io.BytesIO()
    DetectingClient(input_stream=input_stream, output_stream=output_stream).run()

    assert _outbound_messages(output_stream.getvalue()) == [
        {"type": "action", "action": ["detect", [1, 1, 1]]},
        {"type": "finish"},
    ]


def test_client_rejects_a_turn_without_final_finish() -> None:
    class InvalidClient(Client):
        def play(self, state: TurnState) -> tuple[Action, ...]:
            return (Action.detect((1, 1, 1)),)

    client = InvalidClient(
        input_stream=io.BytesIO(
            _inbound_frame({"type": "id", "id": 1, "birth_pos": [6, 0]})
            + _inbound_frame(_turn_message())
        ),
        output_stream=io.BytesIO(),
    )
    with pytest.raises(ValueError, match="finish"):
        client.run()

from __future__ import annotations

import importlib
import io
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SDK_ROOT = ROOT / "games" / "miracle" / "public_sdk"
SDK_MODULES = (
    "ai_client",
    "calculator",
    "card",
    "example_ai",
    "gameunit",
)


class MiracleSdk:
    """Load historical top-level SDK modules only when a test asks for them."""

    def __init__(self) -> None:
        self.ai_client = importlib.import_module("ai_client")
        self.gameunit = importlib.import_module("gameunit")

    def __getattr__(self, name: str) -> ModuleType:
        if name not in SDK_MODULES:
            raise AttributeError(name)
        module = importlib.import_module(name)
        setattr(self, name, module)
        return module


@pytest.fixture
def miracle_sdk() -> MiracleSdk:
    sys.path.insert(0, str(SDK_ROOT))
    try:
        yield MiracleSdk()
    finally:
        sys.path.remove(str(SDK_ROOT))
        for module_name in SDK_MODULES:
            sys.modules.pop(module_name, None)


def test_outbound_frame_counts_utf8_bytes(miracle_sdk: MiracleSdk) -> None:
    frame = miracle_sdk.ai_client.encode_message(
        {"operation_type": "endround", "note": "神迹"}
    )
    size = int.from_bytes(frame[:4], "big", signed=True)
    assert size == len(frame[4:])
    assert json.loads(frame[4:].decode("utf-8"))["note"] == "神迹"


def test_inbound_state_frame_and_map_parse(miracle_sdk: MiracleSdk) -> None:
    payload = {
        "round": 3,
        "camp": 0,
        "map": {
            "units": [],
            "barracks": [-1, -1, -1, -1],
            "miracles": [29, 28],
        },
        "players": [
            [[], 3, 4, [], []],
            [[], 2, 4, [], []],
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    decoded = miracle_sdk.ai_client.read_state(
        io.BytesIO(f"{len(body):06d}".encode() + body)
    )
    game_map = miracle_sdk.gameunit.Map()
    game_map.update(decoded["map"])
    assert decoded["round"] == 3
    assert [item.hp for item in game_map.miracles] == [29, 28]


def test_card_data_is_relative_to_sdk_file(
    miracle_sdk: MiracleSdk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    card = importlib.reload(miracle_sdk.card)
    assert card.CARD_DICT["Archer"][1].cost > 0


def test_operation_serialization(miracle_sdk: MiracleSdk) -> None:
    message = miracle_sdk.ai_client.build_operation(
        1, 7, "move", {"mover": 9, "position": (1, 2, -3)}
    )
    assert message == {
        "player": 1,
        "round": 7,
        "operation_type": "move",
        "operation_parameters": {"mover": 9, "position": (1, 2, -3)},
    }


def test_sdk_decodes_every_official_unit_and_artifact(miracle_sdk: MiracleSdk) -> None:
    data = json.loads((ROOT / "games/miracle/backend/Data.json").read_text())
    sdk = miracle_sdk.gameunit
    for name, index in data["UnitNameParsed"].items():
        assert sdk.CreatureCapacity([index, 3, []]).type == name
        unit = [-1, 0, index, 0, 0, 0, 0, [0, 0], 0, 0,
                [0, 0, 0], 0, False, False, False, False, False, False]
        assert sdk.Unit(unit).type == name
    for name, index in data["ArtifactNameParsed"].items():
        assert sdk.Artifact([0, index, 0, 0, 0, 0, 0]).name == name


def test_neutral_example_selects_fixed_cards_and_ends_round(
    miracle_sdk: MiracleSdk,
) -> None:
    ai = object.__new__(miracle_sdk.example_ai.AI)
    initialized: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    ended: list[bool] = []
    ai.init = lambda: initialized.append((tuple(ai.artifacts), tuple(ai.creatures)))
    ai.end_round = lambda: ended.append(True)
    ai.choose_cards()
    ai.play()
    assert initialized == [
        (("HolyLight",), ("Archer", "Swordsman", "VolcanoDragon"))
    ]
    assert ended == [True]

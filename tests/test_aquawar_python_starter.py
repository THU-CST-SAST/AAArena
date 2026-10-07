from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _decode_packets(payload: bytes) -> list[dict[str, object]]:
    packets: list[dict[str, object]] = []
    offset = 0
    while offset < len(payload):
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        offset += 4
        packets.append(json.loads(payload[offset : offset + length]))
        offset += length
    return packets


def test_aquawar_python_starter_handles_all_protocol_phases() -> None:
    starter = ROOT / "games" / "aquawar" / "public_sdk_python"
    frames = (
        {"Action": "Pick", "RemainFishs": [1, 2, 3, 4, 5], "FirstMover": 0},
        {
            "Action": "Assert",
            "GameInfo": {
                "EnemyFish": [-1, -1, -1, -1],
                "EnemyHP": [100, 100, 100, 100],
                "MyFish": [1, 2, 3, 4],
                "MyHP": [100, 100, 100, 100],
                "MyATK": [10, 10, 10, 10],
            },
        },
        {
            "Action": "Action",
            "GameInfo": {
                "EnemyFish": [-1, -1, -1, -1],
                "EnemyHP": [100, 0, 0, 0],
                "MyFish": [1, 2, 3, 4],
                "MyHP": [100, 0, 0, 0],
                "MyATK": [10, 10, 10, 10],
            },
        },
        {"Action": "Finish", "Result": "Win"},
    )
    completed = subprocess.run(
        (sys.executable, "main.py"),
        cwd=starter,
        input="".join(json.dumps(frame) for frame in frames).encode(),
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert _decode_packets(completed.stdout) == [
        {"Action": "Pick", "ChooseFishs": [1, 2, 3, 4]},
        {"Action": "Null"},
        {"Action": "Action", "Type": 0, "MyPos": 0, "EnemyPos": 0},
        {"Action": "Finish"},
    ]


def test_mimic_fish_in_later_pick_includes_required_imitation_target():
    starter=ROOT/'games/aquawar/public_sdk_python'
    completed=subprocess.run((sys.executable,'main.py'),cwd=starter,
        input=json.dumps({'Action':'Pick','RemainFishs':[9,10,11,12],'FirstMover':0}).encode(),
        capture_output=True,timeout=10)
    assert completed.returncode==0,completed.stderr.decode(errors='replace')
    assert _decode_packets(completed.stdout)==[
        {'Action':'Pick','ChooseFishs':[9,10,11,12],'ImitateFish':1}]

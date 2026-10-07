from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from aa_arena.replay import narrate


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GAMES_ROOT = REPOSITORY_ROOT / "games"

FORBIDDEN_NARRATION = (
    "对手画像",
    "可蒸馏",
    "你的浪费",
    "风格指纹",
    "可预测的窗口",
    "可以放心",
    "策略盲区",
    "败因",
    "转折点",
    "优先修",
    "应该",
    "应当",
    "建议",
    "推荐",
)


def _write_fixture(tmp_path: Path, game: str) -> Path:
    path = tmp_path / f"{game}.replay"
    if game == "antwar":
        document = [
            {
                "op0": [],
                "op1": [],
                "round_state": {
                    "round": 0,
                    "camps": [50, 50],
                    "coins": [100, 100],
                    "speedLv": [0, 0],
                    "anthpLv": [0, 0],
                    "ants": [],
                    "winner": -1,
                },
            },
            {
                "op0": [{"type": 11, "pos": {"x": 2, "y": 3}}],
                "op1": [],
                "round_state": {
                    "round": 1,
                    "camps": [49, 50],
                    "coins": [80, 100],
                    "speedLv": [0, 0],
                    "anthpLv": [0, 0],
                    "ants": [],
                    "winner": 1,
                    "message": "[\"OK\", \"OK\"]",
                },
            },
        ]
        path.write_text(json.dumps(document), encoding="utf-8")
    elif game == "antwar2":
        document = [
            {
                "op0": [],
                "op1": [],
                "round_state": {
                    "round": 0,
                    "camps": [50, 50],
                    "coins": [100, 100],
                    "speedLv": [0, 0],
                    "anthpLv": [0, 0],
                    "weaponCooldowns": [[], []],
                    "towers": [],
                    "ants": [],
                    "activeEffects": [],
                    "winner": -1,
                },
            },
            {
                "op0": [{"type": 11, "pos": {"x": 2, "y": 3}}],
                "op1": [],
                "round_state": {
                    "round": 1,
                    "camps": [49, 50],
                    "coins": [80, 90],
                    "speedLv": [0, 0],
                    "anthpLv": [0, 0],
                    "weaponCooldowns": [[], []],
                    "towers": [{"id": 7, "type": 0, "player": 0}],
                    "ants": [],
                    "activeEffects": [],
                    "winner": 1,
                },
            },
        ]
        path.write_text(json.dumps(document), encoding="utf-8")
    elif game == "aquawar":
        document = [
            {
                "winner": -1,
                "rounds": 1,
                "gamestate": 2,
                "cur_turn": 0,
                "players": [
                    {"fight_fish": [{"id": 10, "hp": 10, "atk": 3}]},
                    {"fight_fish": [{"id": 20, "hp": 10, "atk": 2}]},
                ],
                "operation": [
                    {"Action": "Action", "Type": 0, "MyPos": 0, "EnemyPos": 0}
                ],
            },
            {
                "winner": 0,
                "rounds": 1,
                "gamestate": 4,
                "cur_turn": 1,
                "players": [
                    {"fight_fish": [{"id": 10, "hp": 10, "atk": 3}]},
                    {"fight_fish": [{"id": 20, "hp": 7, "atk": 2}]},
                ],
                "operation": [{"Action": "Finish", "Player": 0}],
            },
        ]
        path.write_text(json.dumps(document), encoding="utf-8")
    elif game == "generals":
        lines = [
            {"Round": 1, "Player": 0, "Action": [1, 2, 3, 4, 3]},
            {"Round": 1, "Player": 0, "Action": [8]},
            {"Round": 1, "Player": 0, "Action": [9], "Content": "game end normally"},
        ]
        path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    elif game == "lostspace":
        document = [
            [[0, 0, 1], [9, 0, 1], [0, 9, 1], [9, 9, 1]],
            [
                [
                    {"type": "hp_update", "playerid": 0, "hp": 10},
                    {"type": "move", "playerid": 0, "pos": [1, 0]},
                ],
                [],
                [],
                [],
            ],
            [
                [{"type": "hp_update", "playerid": 0, "hp": 7}],
                [{"type": "escaped", "playerid": 1}],
                [],
                [],
            ],
            {"0": 3, "1": 4, "2": 2, "3": 1},
        ]
        path.write_text(json.dumps(document), encoding="utf-8")
    elif game == "miracle":
        values = [
            0, 0, 0, 1, 2, 0, 0,
            1, 6, 0, 5, 0, 0, 0,
            2, 10, 0, 0, 0, 0, 0,
        ]
        path.write_bytes(struct.pack(">" + "i" * len(values), *values))
    elif game == "rollman":
        frames = [
            {
                "round": 0,
                "level": 1,
                "score": [0, 0],
                "pacman_coord": [1, 1],
                "ghosts_coord": [[3, 3], [4, 4], [5, 5]],
                "pacman_skill_status": [0, 0, 0, 0, 0],
            },
            {
                "round": 1,
                "level": 1,
                "score": [10, 0],
                "pacman_coord": [1, 2],
                "ghosts_coord": [[3, 2], [4, 3], [5, 4]],
                "pacman_skill_status": [0, 0, 0, 0, 0],
                "StopReason": "time is up",
            },
        ]
        path.write_text("\n".join(json.dumps(frame) for frame in frames), encoding="utf-8")
    elif game == "snakego":
        document = {
            "game_config": {"length": 10, "width": 10, "max_round": 20},
            "item_list": [{"id": 4, "type": 0, "time": 1, "x": 2, "y": 2}],
            "round_info": [{}, {}],
            "operations": [
                [{"basic": {"snake": 0, "type": 2}, "got_item": -1, "extra_length": 0}],
                [
                    {
                        "basic": {"snake": 0, "type": 2},
                        "got_item": 4,
                        "extra_length": 2,
                        "new_snake_id": -1,
                        "dead_snake": [],
                    }
                ],
            ],
            "end_info": {"winner": 0, "type": "NORMAL", "err": None, "score": [12, 8]},
        }
        path.write_text(json.dumps(document), encoding="utf-8")
    else:  # pragma: no cover - fixture table is closed
        raise AssertionError(game)
    return path


@pytest.mark.parametrize(
    ("game", "literal_delta"),
    [
        ("antwar", "P0 基地血量 50→49（-1）"),
        ("antwar2", "P0 基地 HP 50→49（-1）"),
        ("aquawar", "P1 鱼#20 HP 10→7（-3）"),
        ("generals", "移动 3 兵"),
        ("lostspace", "P0 HP 10→7（-3）"),
        ("miracle", "造成伤害（参数 [5]）"),
        ("rollman", "吃豆人比分 0→10（+10）"),
        ("snakego", "P0 长度 +2"),
    ],
)
def test_eight_game_narration_reports_facts_without_strategy_answers(
    tmp_path: Path,
    game: str,
    literal_delta: str,
) -> None:
    replay = _write_fixture(tmp_path, game)

    result = narrate(game, replay, detail="digest", games_root=GAMES_ROOT)

    assert "## 终局事实" in result.text
    assert "## 可观测动作" in result.text
    assert "## 状态变化与数值增量" in result.text
    assert "## 截断元数据" in result.text
    assert literal_delta in result.text
    for forbidden in FORBIDDEN_NARRATION:
        assert forbidden not in result.text


@pytest.mark.parametrize(
    "game",
    ["antwar", "antwar2", "aquawar", "generals", "lostspace", "miracle", "rollman", "snakego"],
)
def test_full_narration_declares_unbounded_unfolded_output(tmp_path: Path, game: str) -> None:
    replay = _write_fixture(tmp_path, game)

    result = narrate(game, replay, detail="full", games_root=GAMES_ROOT)

    assert "detail=full" in result.text
    assert "folding_enabled=false" in result.text
    assert "line_limit=unbounded" in result.text
    assert "omitted_events=" not in result.text


def test_digest_describes_folding_without_calling_folded_events_omitted(tmp_path: Path) -> None:
    replay = _write_fixture(tmp_path, "antwar")
    document = json.loads(replay.read_text(encoding="utf-8"))
    replay.write_text(json.dumps([document[0]] * 300 + [document[1]]), encoding="utf-8")

    result = narrate("antwar", replay, detail="digest", games_root=GAMES_ROOT)

    assert "folding_enabled=true" in result.text
    assert "omitted_events=" not in result.text
    assert "已折叠" in result.text


def test_antwar2_highlights_exclude_interpretive_metric_fields(tmp_path: Path) -> None:
    replay = _write_fixture(tmp_path, "antwar2")

    result = narrate("antwar2", replay, games_root=GAMES_ROOT)

    assert result.highlights["build_count"] == {"P0": 1, "P1": 0}
    assert "idle_resource_rounds" not in result.highlights
    assert "build_downgrade_churn" not in result.highlights
    assert "downgrade_to_weapon_delay" not in result.highlights

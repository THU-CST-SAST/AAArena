"""Shared bounded narration for line-oriented AI9 game packs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aa_arena.replay import Narration, NarrationContext
from aa_arena.replay.public_json import load_ai9_document


def _number(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fields(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in raw.strip("{}").split(";"):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def _observation_summary(raw: str) -> dict[str, Any]:
    fields = _fields(raw)
    return {
        "type": fields.get("type"),
        "id": _number(fields.get("id")),
        "name": fields.get("name"),
        "pos": fields.get("pos"),
        "hp": _number(fields.get("hp")),
        "owner": _number(fields.get("player")),
        "action": fields.get("Action"),
        "target": fields.get("target"),
    }


def _observation_rows(round_record: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for raw in round_record.get("observations", []):
        if isinstance(raw, str):
            rows.append(_observation_summary(raw))
        elif isinstance(raw, dict):
            rows.append(raw)
    return rows


def _score_pair(round_record: dict[str, Any]) -> list[Any]:
    state = round_record.get("state_changes")
    if isinstance(state, dict) and isinstance(state.get("scores"), list):
        return state["scores"]
    return []


def _round_digest(round_record: dict[str, Any]) -> dict[str, Any]:
    observations = _observation_rows(round_record)
    actions = round_record.get("actions", [])
    return {
        "round": round_record.get("round"),
        "units": observations[:24],
        "unit_count": len(observations),
        "actions": actions[:24],
        "action_count": len(actions),
        "scores": _score_pair(round_record),
    }


def _selected_rounds(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(rounds) <= 8:
        return rounds
    return [rounds[0], rounds[1], rounds[len(rounds) // 2], rounds[-2], rounds[-1]]


def narrate_ai9(game: str, replay_path: Path, context: NarrationContext) -> Narration:
    document = load_ai9_document(replay_path)
    rounds = document.get("rounds", [])
    legacy_winner = document.get("winner")
    winner = context.official_winner
    if winner is None and legacy_winner is not None:
        if legacy_winner == -2:
            winner = None
        elif isinstance(legacy_winner, int) and 0 <= legacy_winner < len(context.role_names()):
            winner = context.role_names()[legacy_winner]

    lines = [
        f"# {context.game} AI9 replay：{context.match_id}",
        "",
        f"- 官方裁决：{'平局' if winner is None else winner + ' 获胜'}。",
        f"- 回合记录：{len(rounds)}。",
        f"- 你的座位：{context.perspective or '中立视角'}。",
    ]
    ai_status = next((item.get("ai_status") for item in reversed(rounds) if item.get("ai_status")), None)
    if ai_status:
        lines.append(f"- AI 状态：{ai_status}")
    if context.diagnostic:
        lines.append(f"- 诊断：{context.diagnostic}")

    lines += ["", "## 关键回合", ""]
    for round_record in _selected_rounds(rounds):
        digest = _round_digest(round_record)
        lines.append(
            f"### Round {digest['round']}：{digest['unit_count']} 个状态 / {digest['action_count']} 个动作"
        )
        scores = digest.get("scores")
        if scores:
            lines.append(f"- 分数：{scores}")
        for row in digest["units"][:8]:
            identity = f"{row.get('name') or row.get('type')}#{row.get('id')}"
            lines.append(
                f"- {identity} owner={row.get('owner')} pos={row.get('pos')} "
                f"hp={row.get('hp')} action={row.get('action')} target={row.get('target')}"
            )
        for row in digest["actions"][:12]:
            lines.append(f"- {row.get('action')}: {row.get('detail')}")

    last = rounds[-1] if rounds else {}
    final_units = _observation_rows(last)
    if final_units:
        lines += ["", "## 终局状态", ""]
        for row in final_units[:24]:
            lines.append(
                f"- {row.get('name') or row.get('type')}#{row.get('id')} "
                f"owner={row.get('owner')} pos={row.get('pos')} hp={row.get('hp')}"
            )

    lines += [
        "",
        "## 迭代提示",
        "",
        "- 先比较关键回合的位置、血量、资源与动作顺序；不要只看终局。",
        "- 把改动变成与对手 ID、seed、回放 ID 无关的一般规则，再用小对局验证。",
    ]
    return Narration(
        game=game,
        match_id=context.match_id,
        text="\n".join(lines) + "\n",
        rounds=len(rounds),
        winner=winner,
        highlights={"format": document.get("format"), "round_count": len(rounds)},
    )

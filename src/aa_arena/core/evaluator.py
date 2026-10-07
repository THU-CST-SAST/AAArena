"""统一对战器入口 —— `evaluate(game, players, roles, seed)`。

这是 B (HL) / C (RL) 与人类调用 A 的唯一入口。它按游戏名从注册表装配该游戏的
对战器实现，再委托执行。框架 Core 不含任何游戏语义（见 docs/evaluator-contract.md）。
"""

from __future__ import annotations

from pathlib import Path

from aa_arena.core.contract import EvaluateResult, PlayerRef
from aa_arena.core.registry import get_plugin


def evaluate(
    game: str,
    players: list[PlayerRef],
    roles: list[str],
    seed: int,
    *,
    games_root: Path | None = None,
) -> EvaluateResult:
    """运行一场对局，返回可解析、可复现的结果。

    参数与返回严格遵循 docs/evaluator-contract.md：
    - 相同 (game, players, roles, seed) 必须可复现；
    - status 严格区分 complete / game_error / infra_error。
    """
    if len(players) != len(roles):
        raise ValueError("players 与 roles 必须一一对应且等长")
    plugin = get_plugin(game, games_root)
    game_dir = (games_root or Path(__file__).resolve().parents[3] / "games") / game
    evaluator = plugin.evaluator_factory(game_dir)
    return evaluator.evaluate(players, roles, seed)

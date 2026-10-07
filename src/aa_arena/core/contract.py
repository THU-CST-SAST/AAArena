"""对战器契约的代码形式 —— 与 `docs/evaluator-contract.md` 一一对应。

这里只有**游戏无关**的数据结构与协议。任何游戏的对战器都必须满足 `Evaluator`
协议，并返回 `EvaluateResult`。框架 Core 只认这份抽象，不含游戏语义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class EvaluationStatus(str, Enum):
    """对局终态，严格区分比赛内失败与基础设施故障（见契约 §4）。"""

    COMPLETE = "complete"          # 正常结束，产出有效胜负；计入排行榜
    GAME_ERROR = "game_error"      # 选手侧失败（超时/非法动作/崩溃）；按规则判负，计入
    INFRA_ERROR = "infra_error"    # 基础设施故障（环境问题）；不计入，应重跑


@dataclass(frozen=True)
class PlayerRef:
    """选手引用：`games/<game>/players/` 下的 player_id 或路径。"""

    player_id: str
    # 可选：显式代码路径；为 None 时由对战器按 player_id 从选手池解析
    code_path: str | None = None


@dataclass(frozen=True)
class EvaluateResult:
    """一场对局的可解析、可复现结果（见契约 §3）。"""

    status: EvaluationStatus
    winner: str | None = None                 # 胜者角色标识；平局/未完成为 None
    scores: dict[str, float] = field(default_factory=dict)  # 角色 -> 得分
    rounds: int | None = None                 # 回合数；未知记 None（绝不填 0）
    replay_path: str | None = None            # 回放文件路径；无回放为 None
    diagnostic: str | None = None             # 非 complete 时的诊断信息
    # 游戏特有的补充事实（可选）。用途：官方判定链里 `scores` 之外的分量。
    # 例：antwar/antwar2 官方判定是 5 级字典序——基地血量 → 击败蚂蚁数 →
    # 超级武器使用次数 → AI 用时 → 先手。`scores` 必须**严格**只放第一判据
    # （基地血量），否则 margin 的量纲会变成我们自己发明的加权和；其余分量放这里，
    # 供下游在第一判据打平时继续比较。
    # 纪律：拿不到的量一律**不写这个键**，绝不用 0 冒充"打平"。
    payload: dict[str, object] = field(default_factory=dict)

    def is_valid_match(self) -> bool:
        """只有 complete / game_error 才是有效对局（可进胜负统计）。"""
        return self.status in (EvaluationStatus.COMPLETE, EvaluationStatus.GAME_ERROR)


@runtime_checkable
class Evaluator(Protocol):
    """每个游戏在 `games/<game>/evaluator/` 提供的对战器需满足此协议。"""

    def evaluate(
        self,
        players: list[PlayerRef],
        roles: list[str],
        seed: int,
    ) -> EvaluateResult:
        """运行一场对局并返回可复现结果。

        相同 `(players, roles, seed)` 必须产出一致的 winner/scores/rounds 与回放。
        """
        ...

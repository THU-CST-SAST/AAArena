"""AgentBench Framework Core —— 游戏无关的对战器与游戏注册表。

本包**不含任何游戏语义**：它只定义对战器的抽象 I/O 契约，并按游戏名从注册表
装配 `games/<game>/evaluator/` 提供的具体实现。契约详见
`docs/evaluator-contract.md`。

- `contract`：数据结构与 `Evaluator` 协议（对战器契约的代码形式）。
- `registry`：按游戏名发现并装配 `games/<game>/` 的评测器插件。
- `evaluator`：统一入口 `evaluate(game, players, roles, seed)`。
- `decision_space`：`games/<game>/decision_space.yaml` 里**机器可读**的
  `information_gain:` 段（行为信息增益/KL 的测量口径，B/C 共用）。
"""

from __future__ import annotations

from aa_arena.core.contract import (
    EvaluateResult,
    EvaluationStatus,
    Evaluator,
    PlayerRef,
)
from aa_arena.core.decision_space import (
    DecisionSpaceError,
    InformationGainSpec,
    SupportSpec,
    information_gain_spec,
    load_information_gain,
)
from aa_arena.core.evaluator import evaluate
from aa_arena.core.registry import (
    GamePlugin,
    available_games,
    register_game,
)

__all__ = [
    "DecisionSpaceError",
    "EvaluateResult",
    "EvaluationStatus",
    "Evaluator",
    "GamePlugin",
    "InformationGainSpec",
    "PlayerRef",
    "SupportSpec",
    "available_games",
    "evaluate",
    "information_gain_spec",
    "load_information_gain",
    "register_game",
]

"""Shared Saiblo-compatible judging transport.

The public API intentionally contains no game-specific types. Games prepare process specs and
interpret :class:`JudgerResult` in their own evaluator modules.
"""

from .judger import (
    DEFAULT_MATCH_TIMEOUT_S,
    ProcessSpec,
    SaibloJudgerError,
    JudgerResult,
    run_stdio_match,
)
from .protocol import AiErrorType, AuxiliaryMessage, GameOver, RoundConfig, RoundInfo
from aa_arena.sandbox import SandboxInfrastructureError

__all__ = [
    "DEFAULT_MATCH_TIMEOUT_S",
    "AiErrorType",
    "AuxiliaryMessage",
    "GameOver",
    "JudgerResult",
    "ProcessSpec",
    "RoundConfig",
    "RoundInfo",
    "SaibloJudgerError",
    "SandboxInfrastructureError",
    "run_stdio_match",
]

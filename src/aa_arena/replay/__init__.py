"""回放 → 自然语言（仓库 A 的资产）。

用法::

    from aa_arena.replay import narrate

    narration = narrate("antwar2", "replay.json", perspective="P0", detail="full")
    print(narration.text)

命令行::

    python -m aa_arena.replay --game antwar2 --replay replay.json --perspective P0
"""

from __future__ import annotations

from aa_arena.replay.narration import (
    Narration,
    NarrationContext,
    NarrationError,
    available_narrators,
    narrate,
    narrate_to_path,
)
from aa_arena.replay.prose import (
    DETAIL_LEVELS,
    Document,
    Event,
    budget_lines,
    describe_delta,
    describe_pair,
    fold_runs,
    phase_split,
    render_events,
)

__all__ = [
    "DETAIL_LEVELS",
    "Document",
    "Event",
    "Narration",
    "NarrationContext",
    "NarrationError",
    "available_narrators",
    "budget_lines",
    "describe_delta",
    "describe_pair",
    "fold_runs",
    "narrate",
    "narrate_to_path",
    "phase_split",
    "render_events",
]

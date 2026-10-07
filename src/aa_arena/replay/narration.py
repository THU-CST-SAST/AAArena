"""回放 → 自然语言：框架层（游戏无关）。

这是**仓库 A 的资产**，理由和对战器、决策空间放在 A 一样：回放字段的语义是游戏知识，
只能有一份权威定义。历史上 B 侧只拿到 `replay_skill.md`（一份给人读的 Markdown 指南）
和裸回放，于是每个 run 里 agent 都要自己现写一遍解析代码 —— 同一份数据被 N 个 agent
用 N 种（可能是错的）方式解读，反馈质量不可控，还白烧墙钟。

现在的分工：

* **A**（这里）：把回放 JSON 完整翻译成自然语言，陈述"这一局到底发生了什么"；
* **B**：只负责把翻译结果放进 Feedback 通道交回容器，不碰任何游戏语义。

框架 Core 绝不 import 具体游戏
------------------------------
和 `core/registry.py` 一样走惰性发现：每个游戏在
`games/<game>/evaluator/narrate.py` 里暴露

    def narrate(replay_path: Path, context: NarrationContext) -> Narration | str

框架按游戏名找到它、调用它。接入新游戏不需要改本文件一行。
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from aa_arena.replay.prose import DETAIL_LEVELS, DetailLevel

__all__ = [
    "Narration",
    "NarrationContext",
    "NarrationError",
    "available_narrators",
    "narrate",
    "narrate_to_path",
]


class NarrationError(RuntimeError):
    """回放无法被翻译（文件缺失/损坏，或该游戏还没有 narrator）。"""


@dataclass(frozen=True)
class NarrationContext:
    """翻译一局回放所需的上下文。

    ``perspective`` 是**关键参数**：同一局回放对两个座位的意义完全不同。HL 迭代里
    读回放的是候选自己，它需要的是"**我**在第几回合被打崩了"，而不是一份中立解说。
    不给 perspective 时退化为中立叙述。
    """

    game: str
    match_id: str
    roles: tuple[str, ...] = ()
    #: 候选占哪个座位（如 "P0" / "rollman"）；None = 中立视角。
    perspective: str | None = None
    #: 对手标识（只用于叙述里称呼对手，绝不含对手代码）。
    opponent_id: str = ""
    detail: DetailLevel = "digest"
    #: 对战器判定的结果，用于让叙述与官方裁决对齐（回放末帧偶尔与裁决不一致）。
    official_winner: str | None = None
    official_rounds: int | None = None
    #: 候选自身故障的诊断（超时/非法/崩溃）。0 回合判负时回放里什么都没有，
    #: 这条诊断是唯一线索，必须写进叙述，否则 agent 会在同一个坑里反复迭代。
    diagnostic: str = ""

    def role_names(self) -> tuple[str, ...]:
        return self.roles or ("P0", "P1")

    def label(self, role: str) -> str:
        """把角色名讲成"你"/"对手"，让叙述对读者直接可用。"""

        if self.perspective is None:
            return role
        if role == self.perspective:
            return f"你({role})"
        return f"对手({role})"


@dataclass(frozen=True)
class Narration:
    """一局回放的自然语言叙述。"""

    game: str
    match_id: str
    text: str
    rounds: int | None = None
    winner: str | None = None
    #: 结构化要点（可选）：narrator 顺手算出来的量，B 侧可直接进 events。
    highlights: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:  # 便于 print(narration)
        return self.text


def _games_root() -> Path:
    """定位仓库内的 games/ 目录（相对本文件向上四级到仓库根）。"""

    return Path(__file__).resolve().parents[3] / "games"


def _ensure_parent_package(game: str, evaluator_dir: Path) -> str:
    """造一个合成父包，让 narrator 里的 ``from . import replay`` 能用。

    为什么不直接执行 ``games/<game>/evaluator/__init__.py``：那个 ``__init__`` 会把
    对战器与运行时（编译后端、准备选手包）一起拉起来，有的还要 C++ 工具链。
    翻译一份回放不该需要这些。所以这里只造一个**空的**包壳，把 ``__path__`` 指到
    evaluator 目录 —— 子模块（``replay.py``）照常按包内规则解析，但 ``__init__`` 的
    副作用一个都不会发生。
    """

    name = f"aa_arena_games_replay.{game}"
    root = "aa_arena_games_replay"
    if root not in sys.modules:
        shell = ModuleType(root)
        shell.__path__ = []  # type: ignore[attr-defined]
        sys.modules[root] = shell
    if name not in sys.modules:
        package = ModuleType(name)
        package.__path__ = [str(evaluator_dir)]  # type: ignore[attr-defined]
        sys.modules[name] = package
    return name


def _load_narrator(game: str, games_root: Path | None = None) -> ModuleType:
    root = games_root or _games_root()
    evaluator_dir = root / game / "evaluator"
    module_path = evaluator_dir / "narrate.py"
    if not module_path.is_file():
        raise NarrationError(
            f"{game} 还没有回放叙述器：缺少 {module_path}。\n"
            "每个游戏必须在 games/<game>/evaluator/narrate.py 暴露 "
            "narrate(replay_path, context)。"
        )
    parent = _ensure_parent_package(game, evaluator_dir)
    module_name = f"{parent}.narrate"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise NarrationError(f"无法加载 {module_path}")
    module = importlib.util.module_from_spec(spec)
    # 先登记再执行：narrate.py 里 `from . import replay` 这类相对写法才不会炸。
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def available_narrators(games_root: Path | None = None) -> list[str]:
    """列出已经有回放叙述器的游戏。"""

    root = games_root or _games_root()
    if not root.is_dir():
        return []
    return sorted(
        child.name
        for child in root.iterdir()
        if child.is_dir()
        and not child.name.startswith("_")
        and (child / "evaluator" / "narrate.py").is_file()
    )


def narrate(
    game: str,
    replay_path: str | Path,
    *,
    match_id: str = "",
    roles: tuple[str, ...] = (),
    perspective: str | None = None,
    opponent_id: str = "",
    detail: DetailLevel = "digest",
    official_winner: str | None = None,
    official_rounds: int | None = None,
    diagnostic: str = "",
    games_root: Path | None = None,
) -> Narration:
    """把一局回放翻译成自然语言。

    回放缺失不是异常路径而是**常见路径**：候选第 0 回合就被判负时根本没有回放。
    这时仍然要产出一段有用的叙述（把对战器诊断讲清楚），否则 agent 只会看到"你输了"。
    """

    if detail not in DETAIL_LEVELS:
        raise NarrationError(f"detail 必须是 {DETAIL_LEVELS} 之一，收到 {detail!r}")
    path = Path(replay_path)
    context = NarrationContext(
        game=game,
        match_id=match_id or path.parent.name or game,
        roles=roles,
        perspective=perspective,
        opponent_id=opponent_id,
        detail=detail,
        official_winner=official_winner,
        official_rounds=official_rounds,
        diagnostic=diagnostic,
    )
    if not path.is_file() or path.stat().st_size == 0:
        return _no_replay_narration(context, path)

    module = _load_narrator(game, games_root)
    entry = getattr(module, "narrate", None)
    if not callable(entry):
        raise NarrationError(f"games/{game}/evaluator/narrate.py 没有暴露 narrate()")
    try:
        result = entry(path, context)
    except Exception as error:  # noqa: BLE001 - 翻译失败不该让整轮反馈丢掉
        return Narration(
            game=game,
            match_id=context.match_id,
            text=_degraded_text(context, f"回放解析失败：{type(error).__name__}: {error}"),
            rounds=context.official_rounds,
            winner=context.official_winner,
            highlights={"narration_error": f"{type(error).__name__}: {error}"},
        )
    if isinstance(result, Narration):
        return result
    if isinstance(result, str):
        return Narration(
            game=game,
            match_id=context.match_id,
            text=result,
            rounds=context.official_rounds,
            winner=context.official_winner,
        )
    raise NarrationError(
        f"games/{game}/evaluator/narrate.py 的 narrate() 必须返回 Narration 或 str，"
        f"收到 {type(result).__name__}"
    )


def _no_replay_narration(context: NarrationContext, path: Path) -> Narration:
    """没有回放时的叙述。这条路径**必须**有用，它对应最常见的失败模式。"""

    lines = [
        f"# {context.game} 对局叙述：{context.match_id}",
        "",
        "> 这一局**没有回放可读**。",
        "",
        "## 发生了什么",
        "",
        f"- 回放文件不存在或为空：`{path}`。",
        (
            "- 这几乎总是意味着你的程序在第一帧就被判死（启动失败 / 输出格式非法 / 超时），"
            "对局根本没有开始，所以后端没有写出任何回合。"
        ),
    ]
    if context.diagnostic:
        lines += [
            "",
            "## 对战器诊断（唯一线索，先修这个）",
            "",
            f"- {context.diagnostic}",
            "",
            "先把启动与协议问题修好，再谈策略强度：这一轮的胜负与策略无关。",
        ]
    else:
        lines += [
            "",
            (
                "对战器没有给出更多诊断。请检查：`ai.py` 是否存在且能 import、"
                "`class AI` 是否实现了契约要求的方法、有没有往 stdout 打印过诊断信息"
                "（stdout 是协议通道，打印一个字节就会被判非法输出）。"
            ),
        ]
    return Narration(
        game=context.game,
        match_id=context.match_id,
        text="\n".join(lines) + "\n",
        rounds=context.official_rounds or 0,
        winner=context.official_winner,
        highlights={"replay_missing": True},
    )


def _degraded_text(context: NarrationContext, reason: str) -> str:
    lines = [
        f"# {context.game} 对局叙述：{context.match_id}",
        "",
        f"> 叙述降级：{reason}",
        "",
        "## 仍然确定的事实",
        "",
        f"- 官方裁决胜者：{context.official_winner or '未知'}。",
        f"- 回合数：{context.official_rounds if context.official_rounds is not None else '未知'}。",
    ]
    if context.diagnostic:
        lines.append(f"- 对战器诊断：{context.diagnostic}。")
    return "\n".join(lines) + "\n"


def narrate_to_path(
    game: str,
    replay_path: str | Path,
    destination: str | Path,
    **kwargs: object,
) -> Narration:
    """翻译并落盘（B 侧把它写进 Feedback 目录）。"""

    narration = narrate(game, replay_path, **kwargs)  # type: ignore[arg-type]
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(narration.text, encoding="utf-8")
    return narration

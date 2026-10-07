"""回放叙述的共享工具 —— 把「一堆数字」变成「一段人能读的话」。

这一层是**游戏无关**的：它不认识任何游戏的字段，只提供 8 个游戏都用得上的
构句与压缩原语。游戏语义全部下沉到 `games/<game>/evaluator/narrate.py`。

为什么需要「压缩」这件事
----------------------
antwar2 的一局回放是 **6.9 MB / 353 回合**的裸 JSON。逐帧直译会得到一份比原文还长
的文本，既读不完也塞不进上下文窗口。但简单截断（"只讲前 30 回合"）又会把决定胜负的
终局段丢掉 —— 那恰恰是最该讲的部分。

所以这里的原则是**无损压缩语义、有损压缩篇幅**：

* `fold_runs`：把连续重复的同类事件折成一句（"第 12–34 轮：连续 23 轮未提交操作"），
  信息没丢，行数掉一个数量级；
* `Document.section`：按「开局 / 中盘 / 终局 / 汇总」分段，终局段永不被裁掉；
* `describe_delta`：只讲**变化**，不复述每帧的全量状态。

命名纪律：所有输出都要能追溯回原始回放。每个事件都带 `round` 或 `state_id`，
这样 agent 写经验时能引用具体证据，而不是凭印象。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal

#: 叙述详细度。``digest`` 给"读一眼就知道发生了什么"，``full`` 给逐回合复盘。
DetailLevel = Literal["digest", "full"]
DETAIL_LEVELS: tuple[str, ...] = ("digest", "full")


def _fmt(value: object) -> str:
    """把数字排版成人读的样子（整数不带小数点，浮点保留必要位数）。"""

    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def describe_delta(name: str, before: object, after: object, *, unit: str = "") -> str | None:
    """只在真的变了的时候产出一句话；没变就返回 None（调用方直接跳过）。"""

    if before == after:
        return None
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        diff = after - before
        sign = "+" if diff > 0 else ""
        return f"{name} {_fmt(before)} → {_fmt(after)}{unit}（{sign}{_fmt(diff)}）"
    return f"{name} {_fmt(before)} → {_fmt(after)}{unit}"


def describe_pair(name: str, values: Sequence[object], roles: Sequence[str]) -> str:
    """把 ``[v0, v1]`` 这类按玩家索引的数组讲成 ``name：P0=… / P1=…``。"""

    parts = [
        f"{role}={_fmt(value)}"
        for role, value in zip(roles, values, strict=False)
    ]
    return f"{name}：" + " / ".join(parts)


@dataclass(frozen=True)
class Event:
    """一条可引用的原子事件。``round`` 与 ``ref`` 是给 agent 做证据引用用的。"""

    round: int
    text: str
    actor: str = ""
    ref: str = ""
    #: 折叠的判定键：连续多条 key 相同的事件会被合并成一句。
    #: 留空表示"永不折叠"（终局、突破、死亡这类关键事件必须逐条保留）。
    fold_key: str = ""
    #: 折叠后用哪句话概括。留空则复用 ``text``。
    #:
    #: 为什么需要和 ``text`` 分开：折叠的粒度往往**粗于**单条文本。snakego 里
    #: "蛇#3 向左移动" / "蛇#3 向下移动" 应该折进同一条"连续纯移动"，但如果直接拿
    #: 第一条的 ``text`` 当概括，就会写成"连续 40 次向左移动"——**这是错的**，
    #: 它把方向也一起概括掉了。所以折叠键管"能不能合"，fold_label 管"合完怎么说"。
    fold_label: str = ""

    def render(self) -> str:
        head = f"第 {self.round} 回合"
        if self.actor:
            head += f" {self.actor}"
        tail = f"（{self.ref}）" if self.ref else ""
        return f"- {head}：{self.text}{tail}"


def fold_runs(events: Iterable[Event], *, min_run: int = 3) -> Iterator[Event]:
    """把连续同类事件折成一条，长度信息写进文本里，不丢语义。

    只折叠 ``fold_key`` 非空的事件，并且**至少 ``min_run`` 条**才折 —— 折 2 条是负
    优化（原文两行、折完还是一行加一句解释，反而更绕）。

    **必须容忍交错**：多人游戏的时间线天然是 ``P0, P1, P0, P1 …`` 交替的，
    如果按"严格相邻"判定，任何一条运行流都会被对手的事件打断，折叠永远不会触发。
    实测 antwar2 一局 726 条事件里 600+ 条是 HOLD，不容忍交错的话一条都折不掉。
    所以这里**按 actor 各维护一条运行流**，只有该 actor 自己的运行流被打断时才结算。
    结算位置取运行流结束处，时间顺序仍然单调。
    """

    runs: dict[str, list[Event]] = {}

    def settle(actor: str) -> Iterator[Event]:
        buffer = runs.pop(actor, [])
        if not buffer:
            return
        if len(buffer) < min_run:
            yield from buffer
            return
        first, last = buffer[0], buffer[-1]
        yield Event(
            round=first.round,
            actor=first.actor,
            text=(
                f"第 {first.round}–{last.round} 回合连续 {len(buffer)} 次"
                f"{first.fold_label or first.text}（已折叠）"
            ),
            ref=first.ref,
        )

    for event in events:
        actor = event.actor
        if not event.fold_key:
            # 关键事件：先把该 actor 累积的运行流结算掉，保证它出现在正确位置。
            yield from settle(actor)
            yield event
            continue
        current = runs.get(actor)
        if current and current[-1].fold_key != event.fold_key:
            yield from settle(actor)
            current = None
        runs.setdefault(actor, []).append(event)
    for actor in list(runs):
        yield from settle(actor)


@dataclass
class Document:
    """一份回放叙述的骨架：标题 + 若干带标题的小节。"""

    title: str
    subtitle: str = ""
    _sections: list[tuple[str, list[str]]] = field(default_factory=list)

    def section(self, heading: str, lines: Iterable[str]) -> Document:
        rows = [line for line in lines if line]
        if rows:
            self._sections.append((heading, rows))
        return self

    def paragraph(self, heading: str, text: str) -> Document:
        return self.section(heading, [text] if text else [])

    def render(self) -> str:
        out: list[str] = [f"# {self.title}"]
        if self.subtitle:
            out += ["", f"> {self.subtitle}"]
        for heading, rows in self._sections:
            out += ["", f"## {heading}", ""]
            out += rows
        return "\n".join(out).rstrip() + "\n"


def phase_split(events: Sequence[Event], *, head: int = 12, tail: int = 24) -> dict[str, list[Event]]:
    """把时间线切成开局 / 中盘 / 终局三段。

    ``digest`` 模式下中盘会被折叠得更狠，但**终局段永远完整保留** —— 胜负是在那里
    定下来的，裁掉终局的"摘要"没有任何复盘价值。
    """

    if len(events) <= head + tail:
        return {"opening": list(events), "midgame": [], "endgame": []}
    return {
        "opening": list(events[:head]),
        "midgame": list(events[head : len(events) - tail]),
        "endgame": list(events[len(events) - tail :]),
    }


def budget_lines(lines: Sequence[str], max_lines: int) -> list[str]:
    """把一段行数超标的清单裁到预算内，**从中间裁**并如实说明裁了多少。

    为什么从中间裁而不是从尾部截断：开头是布局与开局套路、结尾是胜负是怎么定的，
    两端都不能丢。中盘的重复段才是可牺牲的部分。

    为什么必须"如实说明"：读者要据此写策略。如果它以为自己看到了完整时间线，
    而实际上被静默截断了，它会对"对手中盘什么都没做"这种假象下结论。
    """

    if max_lines <= 0 or len(lines) <= max_lines:
        return list(lines)
    head = max_lines // 2
    tail = max_lines - head
    dropped = len(lines) - max_lines
    return [
        *lines[:head],
        f"- （中间 {dropped} 行已省略以控制篇幅；需要逐条复盘请用 detail=full）",
        *lines[len(lines) - tail :],
    ]


def render_events(
    events: Iterable[Event],
    *,
    fold: bool = True,
    max_lines: int = 0,
) -> list[str]:
    """渲染事件列表。

    折叠是**按 actor** 结算的（见 ``fold_runs``），结算位置会让不同 actor 的行小幅
    错位。这里最后按回合做一次**稳定**排序，把时间顺序恢复成单调递增 —— 读者是按
    "第几回合发生了什么"找证据的，行序跳来跳去会让人怀疑数据有问题。
    稳定排序保证同一回合内的原有先后（先手/后手、同回合内的操作序）不被打乱。

    ``max_lines`` > 0 时再套一层篇幅预算（见 ``budget_lines``）。
    """

    stream = list(fold_runs(events)) if fold else list(events)
    stream.sort(key=lambda event: event.round)
    rendered = [event.render() for event in stream]
    return budget_lines(rendered, max_lines) if max_lines else rendered

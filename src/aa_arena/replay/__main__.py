"""``python -m aa_arena.replay`` —— 把一局回放翻译成自然语言。

这是 A 侧的对外入口：B 可以直接 import `aa_arena.replay.narrate`（同进程，快），
也可以用这个 CLI（跨语言/跨进程，适合排障时手动看一局）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aa_arena.replay.narration import (
    NarrationError,
    available_narrators,
    narrate,
)
from aa_arena.replay.prose import DETAIL_LEVELS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aa_arena.replay",
        description="把官方回放 JSON 翻译成自然语言（陈述这一局发生了什么）",
    )
    parser.add_argument("--game", help="游戏名（games/<game>/）")
    parser.add_argument("--replay", help="回放文件路径")
    parser.add_argument("--out", help="输出 Markdown 路径（默认写 stdout）")
    parser.add_argument("--match-id", default="", help="对局标识（默认取回放所在目录名）")
    parser.add_argument(
        "--perspective",
        default=None,
        help="以哪个座位的视角叙述（如 P0 / rollman）；不给则中立叙述",
    )
    parser.add_argument("--opponent-id", default="", help="对手标识（仅用于称呼）")
    parser.add_argument(
        "--detail",
        default="digest",
        choices=DETAIL_LEVELS,
        help="digest=读一眼就懂；full=逐回合复盘",
    )
    parser.add_argument("--winner", default=None, help="对战器判定的胜者（与叙述对齐）")
    parser.add_argument("--rounds", type=int, default=None, help="对战器判定的回合数")
    parser.add_argument("--diagnostic", default="", help="候选自身故障诊断（0 回合判负时）")
    parser.add_argument("--list", action="store_true", help="列出已有叙述器的游戏后退出")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        for game in available_narrators():
            print(game)
        return 0
    if not args.game or not args.replay:
        build_parser().print_usage(sys.stderr)
        print("错误：--game 与 --replay 都是必需的（或用 --list）", file=sys.stderr)
        return 2
    try:
        narration = narrate(
            args.game,
            args.replay,
            match_id=args.match_id,
            perspective=args.perspective,
            opponent_id=args.opponent_id,
            detail=args.detail,
            official_winner=args.winner,
            official_rounds=args.rounds,
            diagnostic=args.diagnostic,
        )
    except NarrationError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(narration.text, encoding="utf-8")
        print(f"已写出 {target}（{len(narration.text)} 字符）", file=sys.stderr)
    else:
        sys.stdout.write(narration.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

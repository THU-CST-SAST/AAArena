"""游戏注册表 —— 按游戏名发现并装配 `games/<game>/` 的评测器插件。

框架 Core **绝不 import 具体游戏**。游戏通过在 `games/<game>/plugin.py` 暴露一个
`PLUGIN = GamePlugin(...)` 被本注册表发现（惰性加载）。这样接入新游戏无需修改
框架源码。
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from aa_arena.core.contract import Evaluator


@dataclass(frozen=True)
class GamePlugin:
    """一个游戏向框架声明自己的最小契约。

    `evaluator_factory` 返回满足 `Evaluator` 协议的对象。其余为元信息。
    """

    name: str
    evaluator_factory: Callable[[Path], Evaluator]
    display_name: str = ""
    roles: tuple[str, ...] = ()
    roles_symmetric: bool = False


# 运行时注册表：{game_name: GamePlugin}
_REGISTRY: dict[str, GamePlugin] = {}


def register_game(plugin: GamePlugin) -> None:
    """登记一个游戏插件（通常在 games/<game>/plugin.py 中调用或暴露 PLUGIN）。"""
    if plugin.name in _REGISTRY:
        raise ValueError(f"game already registered: {plugin.name}")
    _REGISTRY[plugin.name] = plugin


def _games_root() -> Path:
    """定位仓库内的 games/ 目录（相对本文件向上四级到仓库根）。"""
    return Path(__file__).resolve().parents[3] / "games"


def _discover(game: str, games_root: Path | None = None) -> GamePlugin | None:
    """惰性发现 games/<game>/plugin.py 暴露的 PLUGIN。"""
    if game in _REGISTRY:
        return _REGISTRY[game]
    root = games_root or _games_root()
    plugin_path = root / game / "plugin.py"
    if not plugin_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"aa_arena_games.{game}", plugin_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = getattr(module, "PLUGIN", None)
    if isinstance(plugin, GamePlugin):
        _REGISTRY.setdefault(plugin.name, plugin)
        return _REGISTRY[plugin.name]
    return None


def get_plugin(game: str, games_root: Path | None = None) -> GamePlugin:
    plugin = _discover(game, games_root)
    if plugin is None:
        raise KeyError(f"no game plugin found for {game!r} (games/{game}/plugin.py missing?)")
    return plugin


def available_games(games_root: Path | None = None) -> list[str]:
    """列出所有可发现的游戏（含已注册 + games/ 下有 plugin.py 的）。"""
    root = games_root or _games_root()
    found = set(_REGISTRY)
    if root.is_dir():
        found.update(
            child.name
            for child in root.iterdir()
            if child.is_dir()
            and not child.name.startswith("_")  # 跳过 _template 等骨架目录
            and (child / "plugin.py").is_file()
        )
    return sorted(found)

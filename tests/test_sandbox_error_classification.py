from __future__ import annotations

import inspect
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aa_arena.core import EvaluationStatus, PlayerRef
from aa_arena.core.registry import get_plugin
from aa_arena.saiblo import ProcessSpec
from aa_arena.sandbox import SandboxInfrastructureError


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GAMES_ROOT = REPOSITORY_ROOT / "games"
SAIBLO_GAMES = (
    "antwar",
    "antwar2",
    "aquawar",
    "generals",
    "lostspace",
    "miracle",
    "rollman",
    "snakego",
)
LAYOUT_TYPES = {
    "antwar": "AntWarLayout",
    "antwar2": "AntWar2Layout",
    "aquawar": "AquaWarLayout",
    "generals": "GeneralsLayout",
    "lostspace": "LostSpaceLayout",
    "miracle": "MiracleLayout",
    "rollman": "RollmanLayout",
    "snakego": "SnakeGoLayout",
}
ARENA_TYPES = {
    "antwar": "AntWarArena",
    "antwar2": "AntWar2Arena",
    "aquawar": "AquaWarArena",
    "generals": "GeneralsArena",
    "lostspace": "LostSpaceArena",
    "miracle": "MiracleArena",
    "rollman": "RollmanArena",
    "snakego": "SnakeGoArena",
}


class FailingArena:
    def __init__(self, **_: Any) -> None:
        pass

    def run_case(self, *_: Any, **__: Any) -> None:
        raise SandboxInfrastructureError("scope verification denied")

    def run_match(self, *_: Any, **__: Any) -> None:
        raise SandboxInfrastructureError("scope verification denied")


@pytest.mark.parametrize("game", SAIBLO_GAMES)
def test_public_evaluator_classifies_sandbox_failure_as_infrastructure(
    game: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = get_plugin(game, GAMES_ROOT)
    initial = plugin.evaluator_factory(GAMES_ROOT / game)
    evaluator_type = type(initial)
    module = sys.modules[evaluator_type.__module__]
    constructor_parameters = inspect.signature(evaluator_type).parameters
    constructor_arguments: dict[str, Path] = {}
    if "build_root" in constructor_parameters:
        constructor_arguments["build_root"] = tmp_path / "build"
    if "artifact_root" in constructor_parameters:
        constructor_arguments["artifact_root"] = tmp_path / "matches"
    evaluator = evaluator_type(GAMES_ROOT / game, **constructor_arguments)

    backend_source = tmp_path / "backend"
    backend_source.mkdir()
    (backend_source / "main.py").write_text("", encoding="utf-8")
    executable = backend_source / "logic" / "build" / "main"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    fake_layout = SimpleNamespace(
        backend_source_root=backend_source,
        build_root=tmp_path / "build",
        validate=lambda: None,
    )
    layout_type = getattr(module, LAYOUT_TYPES[game])
    monkeypatch.setattr(
        layout_type,
        "from_root",
        classmethod(lambda cls, *args, **kwargs: fake_layout),
        raising=False,
    )
    monkeypatch.setattr(
        layout_type,
        "from_game_dir",
        classmethod(lambda cls, *args, **kwargs: fake_layout),
        raising=False,
    )
    monkeypatch.setattr(module, "audit_human_pool", lambda layout: ())
    if hasattr(module, "build_backend"):
        monkeypatch.setattr(
            module,
            "build_backend",
            lambda layout: SimpleNamespace(executable=executable),
        )
    if hasattr(module, "backend_environment"):
        monkeypatch.setattr(module, "backend_environment", lambda build_root: {})

    player_root = tmp_path / "player"
    player_root.mkdir()
    (player_root / "main.py").write_text("", encoding="utf-8")
    prepared = SimpleNamespace(
        player_id="prepared",
        argv=(sys.executable, "main.py"),
        cwd=player_root,
    )
    if hasattr(module, "_player_process_spec"):
        monkeypatch.setattr(
            module,
            "_player_process_spec",
            lambda *args, **kwargs: ProcessSpec((sys.executable, "main.py"), player_root),
        )
    if hasattr(module, "_resolve_player"):
        monkeypatch.setattr(module, "_resolve_player", lambda *args, **kwargs: prepared)
    if hasattr(module, "_resolve_role_player"):
        monkeypatch.setattr(module, "_resolve_role_player", lambda *args, **kwargs: player_root)
    if hasattr(evaluator, "_resolve_player"):
        monkeypatch.setattr(evaluator, "_resolve_player", lambda *args, **kwargs: prepared)

    @contextmanager
    def fake_backend_run_dir(*_: Any, **__: Any):
        yield backend_source

    if hasattr(module, "backend_run_dir"):
        monkeypatch.setattr(module, "backend_run_dir", fake_backend_run_dir)
    monkeypatch.setattr(module, ARENA_TYPES[game], FailingArena)

    players = [
        PlayerRef(player_id=f"p{index}", code_path=str(player_root))
        for index in range(len(plugin.roles))
    ]
    result = evaluator.evaluate(players, list(plugin.roles), seed=17)

    assert result.status is EvaluationStatus.INFRA_ERROR
    assert result.diagnostic == "player sandbox infrastructure failed: scope verification denied"

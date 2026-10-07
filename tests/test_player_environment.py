from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aa_arena.core import EvaluationStatus, PlayerRef
from aa_arena.core.registry import get_plugin


ROOT = Path(__file__).resolve().parents[1]


def _fake_virtual_environment(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("home = /opt/cpython-3.10\n", encoding="utf-8")
    interpreter = root / "bin" / "python"
    payload = json.dumps(
        {
            "version": "3.10",
            "prefix": str(root),
            "base_prefix": "/opt/cpython-3.10",
            "executable": str(interpreter),
        }
    )
    interpreter.write_text(f"#!/bin/sh\nprintf '%s' '{payload}'\n", encoding="utf-8")
    interpreter.chmod(0o755)
    return interpreter


def test_default_player_environment_resolves_a_worktree_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = importlib.import_module("aa_arena.core.player_environment")
    repository = tmp_path / "worktree"
    module_path = repository / "src/aa_arena/core/player_environment.py"
    module_path.parent.mkdir(parents=True)
    target = tmp_path / "shared-player-env"
    target.mkdir()
    (repository / ".player-env").symlink_to(target, target_is_directory=True)
    monkeypatch.delenv("AA_ARENA_PLAYER_ENV", raising=False)
    monkeypatch.setattr(runtime, "__file__", str(module_path))

    assert runtime.player_environment_root() == target.resolve()


def test_player_command_uses_explicit_shared_virtual_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catch accidental replacement of the player interpreter with sys.executable."""

    interpreter = _fake_virtual_environment(tmp_path / "shared-player-env")
    monkeypatch.setenv("AA_ARENA_PLAYER_ENV", str(interpreter.parents[1]))
    runtime = importlib.import_module("aa_arena.core.player_environment")

    command = runtime.player_command()
    assert command[-2:] == (str(interpreter), "main.py")
    assert command == (str(interpreter), "main.py")


def test_player_command_refuses_missing_environment_without_system_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catch a missing player environment silently falling back to system Python."""

    missing = tmp_path / "missing-player-env"
    monkeypatch.setenv("AA_ARENA_PLAYER_ENV", str(missing))
    runtime = importlib.import_module("aa_arena.core.player_environment")

    with pytest.raises(runtime.PlayerEnvironmentError, match="does not exist"):
        runtime.player_command()


def test_player_command_refuses_plain_interpreter_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catch a system-style interpreter directory being accepted as a virtual env."""

    plain = tmp_path / "plain-python"
    (plain / "bin").mkdir(parents=True)
    interpreter = plain / "bin" / "python"
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o755)
    monkeypatch.setenv("AA_ARENA_PLAYER_ENV", str(plain))
    runtime = importlib.import_module("aa_arena.core.player_environment")

    with pytest.raises(runtime.PlayerEnvironmentError, match="not a virtual environment"):
        runtime.player_command()


def test_player_command_rejects_incompatible_python_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catch a virtualenv made from the incompatible system Python 3.14."""

    root = tmp_path / "wrong-version-env"
    (root / "bin").mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("home = /opt/cpython-3.14\n", encoding="utf-8")
    interpreter = root / "bin" / "python"
    payload = json.dumps(
        {
            "version": "3.14",
            "prefix": str(root),
            "base_prefix": "/opt/cpython-3.14",
            "executable": str(interpreter),
        }
    )
    interpreter.write_text(f"#!/bin/sh\nprintf '%s' '{payload}'\n", encoding="utf-8")
    interpreter.chmod(0o755)
    monkeypatch.setenv("AA_ARENA_PLAYER_ENV", str(root))
    runtime = importlib.import_module("aa_arena.core.player_environment")

    with pytest.raises(runtime.PlayerEnvironmentError, match="requires Python 3.10"):
        runtime.player_command()


def test_player_command_rejects_unowned_conda_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catch a Python 3.10 Conda base being accepted as the player environment."""

    root = tmp_path / "conda-base"
    (root / "bin").mkdir(parents=True)
    (root / "conda-meta").mkdir()
    (root / "conda-meta" / "history").write_text("base history\n", encoding="utf-8")
    interpreter = root / "bin" / "python"
    payload = json.dumps(
        {
            "version": "3.10",
            "prefix": str(root),
            "base_prefix": str(root),
            "executable": str(interpreter),
        }
    )
    interpreter.write_text(f"#!/bin/sh\nprintf '%s' '{payload}'\n", encoding="utf-8")
    interpreter.chmod(0o755)
    monkeypatch.setenv("AA_ARENA_PLAYER_ENV", str(root))
    runtime = importlib.import_module("aa_arena.core.player_environment")

    with pytest.raises(runtime.PlayerEnvironmentError, match="not owned by AA-Arena"):
        runtime.player_command()


def test_player_command_detects_environment_removed_after_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catch a long-running worker retaining a stale successful validation."""

    interpreter = _fake_virtual_environment(tmp_path / "player-env")
    monkeypatch.setenv("AA_ARENA_PLAYER_ENV", str(interpreter.parents[1]))
    runtime = importlib.import_module("aa_arena.core.player_environment")
    assert runtime.player_python() == interpreter

    interpreter.unlink()

    with pytest.raises(runtime.PlayerEnvironmentError, match="no executable Python"):
        runtime.player_python()


def test_player_command_does_not_nest_a_second_filesystem_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catch one player being able to contaminate the shared environment for later matches."""

    root = tmp_path / "player-env"
    interpreter = _fake_virtual_environment(root)
    payload = json.dumps(
        {
            "version": "3.10",
            "prefix": str(root),
            "base_prefix": "/opt/cpython-3.10",
            "executable": str(interpreter),
        }
    )
    interpreter.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-I\" ]; then\n"
        f"  printf '%s' '{payload}'\n"
        "  exit 0\n"
        "fi\n"
        f"touch '{root / 'contaminated'}'\n",
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    monkeypatch.setenv("AA_ARENA_PLAYER_ENV", str(root))
    runtime = importlib.import_module("aa_arena.core.player_environment")

    command = runtime.player_command()
    assert command == (str(interpreter), "main.py")
    assert "bwrap" not in command


def test_default_player_environment_is_repository_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a default that points at an ambient or system-wide Python environment."""

    monkeypatch.delenv("AA_ARENA_PLAYER_ENV", raising=False)
    runtime = importlib.import_module("aa_arena.core.player_environment")
    repository_root = Path(__file__).resolve().parents[1]

    assert runtime.player_environment_root() == (repository_root / ".player-env").resolve()
    assert runtime.player_environment_root() != Path(os.environ.get("CONDA_PREFIX", "/"))


def _evaluator_module(game: str) -> tuple[Any, Any]:
    evaluator = get_plugin(game, ROOT / "games").evaluator_factory(ROOT / "games" / game)
    return evaluator, sys.modules[evaluator.__class__.__module__]


def _runtime_module(game: str) -> Any:
    evaluator, _ = _evaluator_module(game)
    package = evaluator.__class__.__module__.rsplit(".", 1)[0]
    return importlib.import_module(f"{package}.runtime")


@pytest.mark.parametrize("game", ["antwar", "aquawar"])
def test_mixed_language_evaluators_use_shared_python_for_python_players(
    game: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catch AntWar/AquaWar Python submissions inheriting the evaluator Python."""

    interpreter = _fake_virtual_environment(tmp_path / "player-env")
    monkeypatch.setenv("AA_ARENA_PLAYER_ENV", str(interpreter.parents[1]))
    entry = tmp_path / "entry"
    entry.mkdir()
    (entry / "main.py").write_text("", encoding="utf-8")
    _, module = _evaluator_module(game)
    if game == "antwar":
        spec = module._player_process_spec(
            entry, tmp_path / "build", public_sdk_root=tmp_path / "sdk"
        )
    else:
        spec = module._player_process_spec(entry, tmp_path / "build")

    assert spec.argv[-2:] == (str(interpreter), "main.py")


@pytest.mark.parametrize("game", ["generals", "lostspace", "miracle"])
def test_prepared_python_players_use_shared_environment(
    game: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catch prepared Python players being launched by the evaluator interpreter."""

    interpreter = _fake_virtual_environment(tmp_path / "player-env")
    monkeypatch.setenv("AA_ARENA_PLAYER_ENV", str(interpreter.parents[1]))
    package = tmp_path / "package"
    package.mkdir()
    (package / "main.py").write_text("", encoding="utf-8")
    runtime = _runtime_module(game)
    opponent = SimpleNamespace(
        opponent_id="python-player",
        package_root=package,
        lang="python",
        exclusion_diagnostic=None,
    )
    prepared = runtime.prepare_player(opponent, build_root=tmp_path / "build")

    assert prepared.argv[-2:] == (str(interpreter), "main.py")


def test_snakego_python_player_uses_shared_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catch SnakeGo Python packages inheriting the evaluator interpreter."""

    interpreter = _fake_virtual_environment(tmp_path / "player-env")
    monkeypatch.setenv("AA_ARENA_PLAYER_ENV", str(interpreter.parents[1]))
    package = tmp_path / "package"
    package.mkdir()
    (package / "main.py").write_text("", encoding="utf-8")
    runtime = _runtime_module("snakego")

    prepared = runtime.prepare_package("python-player", package, build_root=tmp_path / "build")

    assert prepared.argv[-2:] == (str(interpreter), "main.py")




def test_antwar2_candidate_uses_shared_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catch AntWar2's arena-side candidate command bypassing the shared runtime."""

    interpreter = _fake_virtual_environment(tmp_path / "player-env")
    monkeypatch.setenv("AA_ARENA_PLAYER_ENV", str(interpreter.parents[1]))
    _, evaluator_module = _evaluator_module("antwar2")
    arena = importlib.import_module(
        f"{evaluator_module.__package__}.arena"
    )
    captured: dict[str, Any] = {}

    def runner(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "complete"

    native = arena.ProcessSpec(("/bin/true",), tmp_path)
    instance = arena.AntWar2Arena(
        game=native,
        opponents={"opponent": native},
        artifact_root=tmp_path / "matches",
        runner=runner,
    )
    case = arena.MatchCase("candidate", "opponent", "P0", 7)

    assert instance.run_case(case, tmp_path) == "complete"
    assert captured["candidate_process"].argv[-2:] == (str(interpreter), "main.py")


def test_rollman_reports_missing_player_environment_as_infrastructure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catch a missing shared environment being charged to Rollman players."""

    missing = tmp_path / "missing-player-env"
    monkeypatch.setenv("AA_ARENA_PLAYER_ENV", str(missing))
    evaluator, module = _evaluator_module("rollman")
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "main.py").write_text("", encoding="utf-8")
    fake_layout = SimpleNamespace(validate=lambda: None, backend_source_root=backend)
    monkeypatch.setattr(
        module.RollmanLayout,
        "from_game_dir",
        classmethod(lambda cls, *args, **kwargs: fake_layout),
    )
    monkeypatch.setattr(module, "audit_human_pool", lambda layout: ())
    roots = []
    for name in ("pacman", "ghost"):
        root = tmp_path / name
        root.mkdir()
        (root / "main.py").write_text("", encoding="utf-8")
        roots.append(root)
    players = [
        PlayerRef(name, code_path=str(root))
        for name, root in zip(("pacman", "ghost"), roots, strict=True)
    ]

    result = evaluator.evaluate(players, ["rollman", "ghost"], seed=7)

    assert result.status is EvaluationStatus.INFRA_ERROR
    assert "shared player environment" in (result.diagnostic or "")


@pytest.mark.parametrize("game", ["generals", "lostspace", "miracle", "snakego"])
def test_preparation_reports_player_environment_failure_as_infrastructure(
    game: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catch a shared-runtime outage escaping a public game evaluator."""

    player_runtime = importlib.import_module("aa_arena.core.player_environment")
    evaluator, module = _evaluator_module(game)
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "main.py").write_text("", encoding="utf-8")
    fake_layout = SimpleNamespace(
        validate=lambda: None,
        backend_source_root=backend,
        build_root=tmp_path / "build",
    )
    layout_type = getattr(
        module,
        {
            "generals": "GeneralsLayout",
            "lostspace": "LostSpaceLayout",
            "miracle": "MiracleLayout",
            "snakego": "SnakeGoLayout",
        }[game],
    )
    factory_name = "from_root" if game in {"generals", "snakego"} else "from_game_dir"
    monkeypatch.setattr(
        layout_type,
        factory_name,
        classmethod(lambda cls, *args, **kwargs: fake_layout),
    )
    monkeypatch.setattr(module, "audit_human_pool", lambda layout: ())
    if hasattr(module, "backend_environment"):
        monkeypatch.setattr(module, "backend_environment", lambda build_root: {})

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise player_runtime.PlayerEnvironmentError("shared player environment unavailable")

    if game == "snakego":
        monkeypatch.setattr(module, "_resolve_player", unavailable)
    else:
        monkeypatch.setattr(module, "prepare_player", unavailable)
    player_root = tmp_path / "player"
    player_root.mkdir()
    (player_root / "main.py").write_text("", encoding="utf-8")
    players = [
        PlayerRef(f"p{index}", code_path=str(player_root))
        for index in range(len(get_plugin(game, ROOT / "games").roles))
    ]

    result = evaluator.evaluate(
        players, list(get_plugin(game, ROOT / "games").roles), seed=7
    )

    assert result.status is EvaluationStatus.INFRA_ERROR
    assert "shared player environment unavailable" in (result.diagnostic or "")


def test_antwar2_pool_preparation_reports_player_environment_failure_as_infrastructure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catch AntWar2 pool auditing leaking a missing shared-runtime exception."""

    player_runtime = importlib.import_module("aa_arena.core.player_environment")
    evaluator, module = _evaluator_module("antwar2")
    executable = tmp_path / "backend" / "game" / "output" / "main"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    fake_layout = SimpleNamespace(
        validate=lambda: None,
        backend_source_root=tmp_path / "backend-source",
        build_root=tmp_path / "build",
    )
    monkeypatch.setattr(
        module.AntWar2Layout,
        "from_root",
        classmethod(lambda cls, *args, **kwargs: fake_layout),
    )
    monkeypatch.setattr(
        module,
        "build_backend",
        lambda layout: SimpleNamespace(executable=executable),
    )

    def unavailable(layout: Any) -> Any:
        raise player_runtime.PlayerEnvironmentError("shared player environment unavailable")

    monkeypatch.setattr(module, "audit_human_pool", unavailable)
    players = [PlayerRef("p0"), PlayerRef("p1")]

    result = evaluator.evaluate(players, ["P0", "P1"], seed=7)

    assert result.status is EvaluationStatus.INFRA_ERROR
    assert "shared player environment unavailable" in (result.diagnostic or "")



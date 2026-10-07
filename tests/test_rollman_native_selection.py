from __future__ import annotations

import contextlib
import importlib
from pathlib import Path

from aa_arena.core.contract import EvaluationStatus, PlayerRef
from aa_arena.core.registry import get_plugin


ROOT = Path(__file__).resolve().parents[1]


def _evaluator_module():
    evaluator = get_plugin("rollman", ROOT / "games").evaluator_factory(
        ROOT / "games" / "rollman"
    )
    return importlib.import_module(evaluator.__class__.__module__)


def test_rollman_prefers_python_entry_when_package_has_optional_native_source(
    tmp_path: Path, monkeypatch
) -> None:
    module = _evaluator_module()
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "main.py").write_text("# backend\n", encoding="utf-8")

    class FakeLayout:
        backend_source_root = backend

        @classmethod
        def from_game_dir(cls, _game_dir: Path):
            return cls()

        def validate(self) -> None:
            return None

    observed = {}

    class FakeArena:
        def __init__(self, **kwargs) -> None:
            observed["pacman"] = kwargs["pacman"]
            observed["ghost"] = kwargs["ghost"]

        def run_case(self, case):
            return module.MatchResult(
                case=case,
                status="complete",
                result="win",
                rounds=1,
                payload={"terminal_scores": (1.0, 0.0), "winner_player": 0},
            )

    @contextlib.contextmanager
    def fake_backend_run_dir(*_args, **_kwargs):
        yield backend

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "main.py").write_text("# player\n", encoding="utf-8")
    (candidate / "optional_accelerator.cpp").write_text("int helper() { return 1; }\n")
    opponent = tmp_path / "opponent"
    opponent.mkdir()
    (opponent / "main.py").write_text("# player\n", encoding="utf-8")

    monkeypatch.setattr(module, "RollmanLayout", FakeLayout)
    monkeypatch.setattr(module, "audit_human_pool", lambda _layout: ())
    monkeypatch.setattr(module, "player_command", lambda: ("python-runtime",))
    monkeypatch.setattr(module, "backend_run_dir", fake_backend_run_dir)
    monkeypatch.setattr(module, "RollmanArena", FakeArena)

    evaluator = module.RollmanEvaluator(
        tmp_path / "repo" / "games" / "rollman",
        artifact_root=tmp_path / "artifacts",
        build_root=tmp_path / "build",
    )
    result = evaluator.evaluate(
        [PlayerRef("candidate", str(candidate)), PlayerRef("opponent", str(opponent))],
        ["rollman", "ghost"],
        7,
    )

    assert result.status is EvaluationStatus.COMPLETE
    assert observed["pacman"].argv == ("python-runtime",)
    assert observed["pacman"].cwd == candidate


def test_rollman_native_bridge_is_top_level_python_process_with_local_strategy(
    tmp_path: Path, monkeypatch
) -> None:
    module = _evaluator_module()
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "main.py").write_text("# backend\n", encoding="utf-8")

    class FakeLayout:
        backend_source_root = backend

        @classmethod
        def from_game_dir(cls, _game_dir: Path):
            return cls()

        def validate(self) -> None:
            return None

    observed = {}

    class FakeArena:
        def __init__(self, **kwargs) -> None:
            observed["pacman"] = kwargs["pacman"]

        def run_case(self, case):
            return module.MatchResult(
                case=case,
                status="complete",
                result="win",
                rounds=1,
                payload={"terminal_scores": (1.0, 0.0), "winner_player": 0},
            )

    @contextlib.contextmanager
    def fake_backend_run_dir(*_args, **_kwargs):
        yield backend

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    opponent = tmp_path / "opponent"
    opponent.mkdir()
    (opponent / "main.py").write_text("# player\n", encoding="utf-8")
    built = tmp_path / "built"
    built.mkdir()
    executable = built / "main"
    executable.write_text("", encoding="utf-8")
    (built / "rollman_bridge.py").write_text("# bridge\n", encoding="utf-8")

    monkeypatch.setattr(module, "RollmanLayout", FakeLayout)
    monkeypatch.setattr(module, "audit_human_pool", lambda _layout: ())
    monkeypatch.setattr(
        module, "player_command", lambda script="main.py": ("python-runtime", script)
    )
    monkeypatch.setattr(module, "build_player", lambda *_args: executable)
    monkeypatch.setattr(module, "backend_run_dir", fake_backend_run_dir)
    monkeypatch.setattr(module, "RollmanArena", FakeArena)

    evaluator = module.RollmanEvaluator(
        tmp_path / "repo" / "games" / "rollman",
        artifact_root=tmp_path / "artifacts",
        build_root=tmp_path / "build",
    )
    result = evaluator.evaluate(
        [PlayerRef("candidate", str(candidate)), PlayerRef("opponent", str(opponent))],
        ["rollman", "ghost"],
        7,
    )

    assert result.status is EvaluationStatus.COMPLETE
    assert observed["pacman"].argv == ("python-runtime", "rollman_bridge.py")
    assert observed["pacman"].cwd == built
    assert observed["pacman"].env == {"AA_ARENA_ROLE": "rollman"}

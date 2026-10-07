from __future__ import annotations

import subprocess
import time
import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aa_arena.core.contract import EvaluationStatus, PlayerRef
from aa_arena.core.registry import get_plugin


ROOT = Path(__file__).resolve().parents[1]


def _runtime_module():
    evaluator = get_plugin("antwar2", ROOT / "games").evaluator_factory(
        ROOT / "games" / "antwar2"
    )
    package = evaluator.__class__.__module__.rsplit(".", 1)[0]
    return importlib.import_module(f"{package}.runtime")


def _evaluator_module():
    evaluator = get_plugin("antwar2", ROOT / "games").evaluator_factory(
        ROOT / "games" / "antwar2"
    )
    return importlib.import_module(evaluator.__class__.__module__)


def _install_fast_fake_match(tmp_path: Path, monkeypatch):
    evaluator_module = _evaluator_module()
    backend_executable = tmp_path / "backend" / "game" / "output" / "main"
    backend_executable.parent.mkdir(parents=True)
    backend_executable.write_bytes(b"backend")

    class FakeLayout:
        def __init__(self, root: Path, build_root: Path) -> None:
            self.aa_arena_root = Path(root).resolve()
            self.build_root = Path(build_root).resolve()
            self.backend_source_root = self.aa_arena_root / "games" / "antwar2" / "backend"

        @classmethod
        def from_root(cls, root: Path, build_root: Path):
            return cls(root, build_root)

        def validate(self) -> None:
            return None

    class FakeArena:
        def __init__(self, **_kwargs) -> None:
            pass

        def run_case(
            self, case, *, candidate_root: Path | None = None, candidate_process=None
        ):
            del candidate_root, candidate_process
            return evaluator_module.MatchResult(
                case=case,
                status="complete",
                result="win",
                points=1.0,
                score_margin=1.0,
                rounds=1,
                payload={"terminal_base_hp": (10.0, 9.0)},
            )

    monkeypatch.setattr(evaluator_module, "AntWar2Layout", FakeLayout)
    monkeypatch.setattr(evaluator_module, "AntWar2Arena", FakeArena)
    monkeypatch.setattr(evaluator_module, "player_command", lambda: ("python",))
    return evaluator_module, backend_executable


def _explicit_players(tmp_path: Path) -> list[PlayerRef]:
    players: list[PlayerRef] = []
    for player_id in ("candidate", "opponent"):
        root = tmp_path / player_id
        root.mkdir()
        (root / "main.py").write_text("# test player\n", encoding="utf-8")
        players.append(PlayerRef(player_id, str(root)))
    return players


def test_explicit_player_paths_skip_human_pool_audit(tmp_path: Path, monkeypatch) -> None:
    evaluator_module, backend_executable = _install_fast_fake_match(tmp_path, monkeypatch)
    monkeypatch.setattr(
        evaluator_module,
        "build_backend",
        lambda _layout: evaluator_module.FrozenBackend(
            backend_executable, "source-hash", "executable-hash", tmp_path / "manifest.json"
        ),
    )

    def unexpected_audit(_layout):
        raise AssertionError("explicit code paths must not scan the frozen human pool")

    monkeypatch.setattr(evaluator_module, "audit_human_pool", unexpected_audit)
    evaluator = evaluator_module.AntWar2Evaluator(
        tmp_path / "repo" / "games" / "antwar2",
        build_root=tmp_path / "build",
        artifact_root=tmp_path / "artifacts",
    )

    result = evaluator.evaluate(_explicit_players(tmp_path), ["P0", "P1"], 7)

    assert result.status is EvaluationStatus.COMPLETE


def test_repeated_evaluators_reuse_prepared_backend(tmp_path: Path, monkeypatch) -> None:
    evaluator_module, backend_executable = _install_fast_fake_match(tmp_path, monkeypatch)
    build_calls = 0

    def fake_build(_layout):
        nonlocal build_calls
        build_calls += 1
        return evaluator_module.FrozenBackend(
            backend_executable, "source-hash", "executable-hash", tmp_path / "manifest.json"
        )

    monkeypatch.setattr(evaluator_module, "build_backend", fake_build)
    monkeypatch.setattr(evaluator_module, "audit_human_pool", lambda _layout: ())
    players = _explicit_players(tmp_path)
    kwargs = {
        "build_root": tmp_path / "build",
        "artifact_root": tmp_path / "artifacts",
    }

    first = evaluator_module.AntWar2Evaluator(
        tmp_path / "repo" / "games" / "antwar2", **kwargs
    ).evaluate(players, ["P0", "P1"], 7)
    second = evaluator_module.AntWar2Evaluator(
        tmp_path / "repo" / "games" / "antwar2", **kwargs
    ).evaluate(players, ["P0", "P1"], 8)

    assert first.status is EvaluationStatus.COMPLETE
    assert second.status is EvaluationStatus.COMPLETE
    assert build_calls == 1


def test_explicit_compiled_player_uses_native_process_spec(tmp_path: Path, monkeypatch) -> None:
    evaluator_module, backend_executable = _install_fast_fake_match(tmp_path, monkeypatch)
    monkeypatch.setattr(
        evaluator_module,
        "build_backend",
        lambda _layout: evaluator_module.FrozenBackend(
            backend_executable, "source-hash", "executable-hash", tmp_path / "manifest.json"
        ),
    )
    monkeypatch.setattr(evaluator_module, "audit_human_pool", lambda _layout: ())
    native_executable = tmp_path / "native-build" / "main"
    native_executable.parent.mkdir()
    native_executable.write_bytes(b"native")
    monkeypatch.setattr(evaluator_module, "build_player", lambda *_args: native_executable, raising=False)
    monkeypatch.setattr(
        evaluator_module,
        "has_cpp_build",
        lambda root: Path(root).name == "candidate-cpp",
        raising=False,
    )
    observed = {}

    class InspectingArena:
        def __init__(self, **kwargs) -> None:
            observed["opponent"] = next(iter(kwargs["opponents"].values()))

        def run_case(self, case, *, candidate_root=None, candidate_process=None):
            observed["candidate_root"] = candidate_root
            observed["candidate"] = candidate_process
            return evaluator_module.MatchResult(
                case=case,
                status="complete",
                result="win",
                points=1.0,
                score_margin=1.0,
                rounds=1,
                payload={"terminal_base_hp": (10.0, 9.0)},
            )

    monkeypatch.setattr(evaluator_module, "AntWar2Arena", InspectingArena)
    candidate = tmp_path / "candidate-cpp"
    candidate.mkdir()
    (candidate / "Makefile").write_text("all:\n\ttrue\n", encoding="utf-8")
    opponent = tmp_path / "opponent-python"
    opponent.mkdir()
    (opponent / "main.py").write_text("# player\n", encoding="utf-8")
    evaluator = evaluator_module.AntWar2Evaluator(
        tmp_path / "repo" / "games" / "antwar2",
        build_root=tmp_path / "build",
        artifact_root=tmp_path / "artifacts",
    )

    result = evaluator.evaluate(
        [PlayerRef("candidate", str(candidate)), PlayerRef("opponent", str(opponent))],
        ["P0", "P1"],
        7,
    )

    assert result.status is EvaluationStatus.COMPLETE
    assert observed["candidate"].argv == (str(native_executable),)
    assert observed["candidate_root"] is None



def test_invalid_explicit_cpp_candidate_is_a_game_error(tmp_path: Path, monkeypatch) -> None:
    evaluator_module, backend_executable = _install_fast_fake_match(tmp_path, monkeypatch)
    monkeypatch.setattr(
        evaluator_module,
        "build_backend",
        lambda _layout: evaluator_module.FrozenBackend(
            backend_executable, "source-hash", "executable-hash", tmp_path / "manifest.json"
        ),
    )
    monkeypatch.setattr(evaluator_module, "audit_human_pool", lambda _layout: ())
    monkeypatch.setattr(
        evaluator_module,
        "build_player",
        lambda *_args: (_ for _ in ()).throw(evaluator_module.CppBuildError("bad source")),
    )
    monkeypatch.setattr(
        evaluator_module,
        "has_cpp_build",
        lambda root: Path(root).name == "candidate-cpp",
    )
    candidate = tmp_path / "candidate-cpp"
    candidate.mkdir()
    (candidate / "Makefile").write_text("all:\n\tfalse\n", encoding="utf-8")
    opponent = tmp_path / "opponent-python"
    opponent.mkdir()
    (opponent / "main.py").write_text("# player\n", encoding="utf-8")
    evaluator = evaluator_module.AntWar2Evaluator(
        tmp_path / "repo" / "games" / "antwar2",
        build_root=tmp_path / "build",
        artifact_root=tmp_path / "artifacts",
    )

    result = evaluator.evaluate(
        [PlayerRef("candidate", str(candidate)), PlayerRef("opponent", str(opponent))],
        ["P0", "P1"],
        7,
    )

    assert result.status is EvaluationStatus.GAME_ERROR
    assert result.winner == "P1"
    assert "candidate build failed" in (result.diagnostic or "")


def test_backend_build_is_serialized_and_reused(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime_module()
    source = tmp_path / "source"
    (source / "game" / "src").mkdir(parents=True)
    (source / "game" / "src" / "main.cpp").write_text("int main() {}\n", encoding="utf-8")
    players = tmp_path / "players"
    sdk = players / "rank01__starter" / "SDK"
    sdk.mkdir(parents=True)
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text("rank\n", encoding="utf-8")
    layout = runtime.AntWar2Layout(
        aa_arena_root=tmp_path,
        build_root=tmp_path / "build",
        backend_archive=tmp_path / "unused.zip",
        backend_source_root=source,
        human_manifest=manifest,
        human_extracted_root=players,
        public_sdk_root=sdk,
    )
    make_calls = 0

    def fake_run(command, **_kwargs):
        nonlocal make_calls
        if command[0] == "make":
            make_calls += 1
            time.sleep(0.05)
            executable = Path(command[2]) / "output" / "main"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"backend")
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "g++ test\n", "")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: runtime.build_backend(layout), range(2)))

    assert make_calls == 1
    assert results[0].executable == results[1].executable
    assert results[0].executable.is_file()

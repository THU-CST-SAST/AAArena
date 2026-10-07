from __future__ import annotations

import importlib
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from aa_arena.core import EvaluationStatus, PlayerRef
from aa_arena.core.registry import get_plugin

ROOT = Path(__file__).resolve().parents[1]


def lostspace_runtime():
    evaluator = get_plugin("lostspace", ROOT / "games").evaluator_factory(
        ROOT / "games/lostspace"
    )
    package = evaluator.__class__.__module__.rsplit(".", 1)[0]
    return importlib.import_module(f"{package}.runtime")


def test_lostspace_dependency_preflight_never_invokes_pip(monkeypatch, tmp_path: Path) -> None:
    runtime = lostspace_runtime()
    monkeypatch.setattr(runtime.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("dependency preflight must not run subprocess/pip"),
    )

    env = runtime.backend_environment(tmp_path)

    assert Path(env["PYTHONPATH"]).name == "shim"


def test_lostspace_missing_dependency_names_install_extra(monkeypatch, tmp_path: Path) -> None:
    runtime = lostspace_runtime()
    monkeypatch.setattr(runtime.importlib.util, "find_spec", lambda name: None)

    with pytest.raises(runtime.LostSpaceRuntimeError, match=r"\[lostspace\]"):
        runtime.backend_environment(tmp_path)


def test_lostspace_evaluator_reports_missing_dependency_as_infra_error(monkeypatch) -> None:
    evaluator = get_plugin("lostspace", ROOT / "games").evaluator_factory(
        ROOT / "games/lostspace"
    )
    module = sys.modules[evaluator.__class__.__module__]
    monkeypatch.setattr(
        module,
        "backend_environment",
        lambda build_root: (_ for _ in ()).throw(
            module.LostSpaceRuntimeError("install with: pip install -e '.[lostspace]'")
        ),
        raising=False,
    )
    players = [PlayerRef(f"probe-{index}") for index in range(4)]

    result = evaluator.evaluate(players, ["P0", "P1", "P2", "P3"], seed=7)

    assert result.status is EvaluationStatus.INFRA_ERROR
    assert ".[lostspace]" in (result.diagnostic or "")


def test_optional_extras_cover_each_runtime() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]

    assert extras["lostspace"] == ["antlr4-python3-runtime==4.9.1"]
    assert set(extras["rollman"]) == {
        "cloudpickle==3.1.0",
        "gym==0.26.2",
        "gym-notices==0.0.8",
        "numpy==2.1.2; python_version < '3.14'",
        "numpy==2.3.5; python_version >= '3.14'",
    }
    assert set(extras["games"]) == set(extras["lostspace"] + extras["rollman"])

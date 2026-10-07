from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aa_arena.core import EvaluateResult, EvaluationStatus
from aa_arena import resources
from aa_arena.resources import ARENA_GAMES, certify


FORMAL_STARTER_COVERAGE = {
    "antwar": {("python", ("P0", "P1")), ("cpp", ("P0", "P1"))},
    "antwar2": {("python", ("P0", "P1")), ("cpp", ("P0", "P1"))},
    "aquawar": {("python", ("P0", "P1")), ("cpp", ("P0", "P1"))},
    "generals": {("python", ("P0", "P1")), ("cpp", ("P0", "P1"))},
    "lostspace": {
        ("python", ("P0", "P1", "P2", "P3")),
        ("cpp", ("P0", "P1", "P2", "P3")),
    },
    "miracle": {("python", ("P0", "P1")), ("cpp", ("P0", "P1"))},
    "rollman": {
        ("python", ("rollman",)),
        ("python", ("ghost",)),
        ("cpp", ("rollman",)),
        ("cpp", ("ghost",)),
    },
    "snakego": {("python", ("P0", "P1")), ("cpp", ("P0", "P1"))},
}


@pytest.mark.parametrize("game", ARENA_GAMES)
def test_every_game_builds_an_exact_static_public_bundle(game: str, tmp_path: Path) -> None:
    result = certify(game, output_root=tmp_path, live=False)
    bundle = Path(result.bundle_root)
    assert (bundle / "rules.md").is_file()
    assert (bundle / "leaderboard.json").is_file()
    assert (bundle / "sdk" / "README.md").is_file() or any(
        (child / "README.md").is_file() for child in (bundle / "sdk").iterdir() if child.is_dir()
    )
    assert (bundle / "replay" / "translate").stat().st_mode & 0o111
    assert (bundle / "replay" / "reading_skill.md").is_file()
    assert (bundle / "examples" / "rank40" / "README.md").is_file()
    leaderboard = json.loads((bundle / "leaderboard.json").read_text())
    assert leaderboard["opponents"]
    assert result.static_checks > 6


@pytest.mark.parametrize("game", FORMAL_STARTER_COVERAGE)
def test_formal_game_certificates_list_runnable_languages_and_roles(
    game: str, tmp_path: Path
) -> None:
    result = certify(game, output_root=tmp_path, live=False)

    observed = {
        (starter["language"], tuple(starter["roles"]))
        for starter in result.runnable_starters
    }
    assert observed == FORMAL_STARTER_COVERAGE[game]
    assert all(starter["live_match_status"] == "not_run" for starter in result.runnable_starters)
    assert all((Path(result.bundle_root) / starter["bundle_path"]).is_dir() for starter in result.runnable_starters)


def test_live_certificate_runs_and_marks_every_declared_starter(
    tmp_path: Path, monkeypatch
) -> None:
    replay = tmp_path / "source-replay.json"
    replay.write_text('{"Round": 0, "Action": [8]}\n{"Round": 1, "Action": [9]}\n', encoding="utf-8")
    monkeypatch.setattr(resources, "_certify_player_visibility", lambda _root: None)
    monkeypatch.setattr(
        resources,
        "evaluate",
        lambda *_args, **_kwargs: EvaluateResult(
            status=EvaluationStatus.COMPLETE,
            winner="P0",
            scores={"P0": 1.0, "P1": 0.0},
            rounds=1,
            replay_path=str(replay),
        ),
    )

    monkeypatch.setattr(resources, "narrate", lambda *_args, **_kwargs: SimpleNamespace(text="facts"))
    monkeypatch.setattr(
        resources.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            _args[0] if _args else (), 0, '{"rounds": 1}\n', ""
        ),
    )

    result = certify("generals", output_root=tmp_path / "bundle", live=True)

    assert result.live_matches == 5  # two seats for Python + C++, then rank-40
    assert {starter["live_match_status"] for starter in result.runnable_starters} == {
        "complete"
    }


def test_miracle_publishes_a_complete_cpp_starter(tmp_path: Path) -> None:
    result = certify("miracle", output_root=tmp_path, live=False)

    cpp = [
        starter
        for starter in result.runnable_starters
        if starter["language"] == "cpp"
    ]
    assert cpp == [
        {
            "language": "cpp",
            "roles": ["P0", "P1"],
            "bundle_path": "sdk-alternatives/cpp",
            "build_system": "make",
            "live_match_status": "not_run",
        }
    ]


def test_antwar_publishes_a_complete_cpp_starter(tmp_path: Path) -> None:
    result = certify("antwar", output_root=tmp_path, live=False)

    assert any(
        starter["language"] == "cpp"
        and starter["roles"] == ["P0", "P1"]
        and starter["bundle_path"] == "sdk-alternatives/cpp"
        and starter["build_system"] == "make"
        for starter in result.runnable_starters
    )


def test_lostspace_publishes_a_complete_cpp_starter(tmp_path: Path) -> None:
    result = certify("lostspace", output_root=tmp_path, live=False)

    assert any(
        starter["language"] == "cpp"
        and starter["roles"] == ["P0", "P1", "P2", "P3"]
        and starter["bundle_path"] == "sdk-alternatives/cpp"
        and starter["build_system"] == "make"
        for starter in result.runnable_starters
    )


def test_antwar2_publishes_a_complete_cpp_starter(tmp_path: Path) -> None:
    result = certify("antwar2", output_root=tmp_path, live=False)

    assert any(
        starter["language"] == "cpp"
        and starter["roles"] == ["P0", "P1"]
        and starter["bundle_path"] == "sdk-alternatives/cpp"
        and starter["build_system"] == "make"
        for starter in result.runnable_starters
    )




def test_aquawar_publishes_pure_python_protocol_starter(tmp_path: Path) -> None:
    result = certify("aquawar", output_root=tmp_path, live=False)

    python_starters = [
        starter
        for starter in result.runnable_starters
        if starter["language"] == "python"
    ]
    assert python_starters == [
        {
            "language": "python",
            "roles": ["P0", "P1"],
            "bundle_path": "sdk-alternatives/python",
            "build_system": "python",
            "live_match_status": "not_run",
        }
    ]


def test_rollman_publishes_role_specific_cpp_starters(tmp_path: Path) -> None:
    result = certify("rollman", output_root=tmp_path, live=False)

    actual = {
        (starter["bundle_path"], tuple(starter["roles"]))
        for starter in result.runnable_starters
        if starter["language"] == "cpp" and starter["build_system"] == "make"
    }
    assert actual == {
        ("sdk-alternatives/cpp-rollman", ("rollman",)),
        ("sdk-alternatives/cpp-ghost", ("ghost",)),
    }


def test_public_antwar_docs_use_standalone_import_and_incremental_towers(tmp_path: Path) -> None:
    result = certify("antwar", output_root=tmp_path, live=False)
    bundle = Path(result.bundle_root)
    readme = (bundle / "sdk" / "README.md").read_text(encoding="utf-8")
    guide = (bundle / "replay" / "guide.md").read_text(encoding="utf-8")
    assert "from aa_arena.core import PlayerRef" in readme
    assert "from agentbench.core" not in readme
    assert "type=-1" in guide


@pytest.mark.parametrize("game", ("antwar2", "aquawar", "generals", "snakego"))
def test_previously_missing_sdk_interfaces_are_explicit(game: str, tmp_path: Path) -> None:
    bundle = Path(certify(game, output_root=tmp_path, live=False).bundle_root)
    guides = list((bundle / "sdk").rglob("INTERFACE.md"))
    assert guides
    assert all("submission interface" in guide.read_text(encoding="utf-8") for guide in guides)


def test_public_antwar_translator_is_standalone_and_reconstructs_tower_deltas(
    tmp_path: Path,
) -> None:
    bundle = Path(certify("antwar", output_root=tmp_path / "bundle", live=False).bundle_root)
    replay = tmp_path / "replay.json"
    replay.write_text(
        json.dumps(
            [
                {"round_state": {"towers": [{"id": 1, "player": 0, "type": 0}]}},
                {"round_state": {"towers": [{"id": 2, "player": 1, "type": 0}]}},
                {"round_state": {"towers": [{"id": 1, "player": 0, "type": -1}]}},
            ]
        ),
        encoding="utf-8",
    )
    translator = bundle / "replay" / "translate"
    assert "aa_arena" not in translator.read_text(encoding="utf-8")
    completed = subprocess.run(
        (sys.executable, str(translator), str(replay)),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(completed.stdout.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    assert payload["rounds"] == 3
    assert payload["final"]["tower_count"] == {"1": 1}

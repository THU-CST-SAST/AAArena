from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from aa_arena.core.registry import get_plugin

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "antwar": {"entrypoint": "backend/src/main.cpp", "language": "c++17"},
    "lostspace": {"entrypoint": "backend/main.py", "language": "python"},
    "miracle": {"entrypoint": "backend/main.py", "language": "python"},
    "rollman": {"entrypoint": "backend/main.py", "language": "python"},
}
PLAYER_POOL_SOURCES = {
    "antwar": (
        "27_antwar",
        ("player_submissions/", "top_algorithms/corpus/27_antwar_final_ladder/"),
    ),
    "lostspace": (
        "25_lostspace",
        ("player_submissions/", "top_algorithms/corpus/25_lostspace_final_ladder/"),
    ),
    "miracle": (
        "24_miracle",
        ("player_submissions/", "top_algorithms/corpus/24_miracle_final/"),
    ),
    "rollman": (
        "29_rollman",
        (
            "player_submissions/",
            "top_algorithms/corpus/29_rollman_ghost_final/",
            "top_algorithms/corpus/29_rollman_rollman_final/",
        ),
    ),
}
BANNED_PARTS = {".git", "__pycache__", "replay", "output"}
BANNED_SUFFIXES = {".pyc", ".pyo", ".o", ".obj", ".exe"}
BANNED_NAMES = {".env", "credentials.json", "id_rsa", "id_ed25519"}


def tree_sha256(root: Path, *, exclude_names: frozenset[str] = frozenset()) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file()
        and item.name not in exclude_names
        and "__pycache__" not in item.parts
        and item.suffix != ".pyc"
    ):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


@pytest.mark.parametrize(("game", "expected"), EXPECTED.items())
def test_backend_bundle_and_provenance(game: str, expected: dict[str, str]) -> None:
    game_dir = ROOT / "games" / game
    backend = game_dir / "backend"
    provenance = json.loads((game_dir / "PROVENANCE.json").read_text(encoding="utf-8"))

    assert (game_dir / expected["entrypoint"]).is_file()
    assert provenance["schema_version"] == "1.0"
    assert provenance["game"] == game
    assert provenance["backend"]["path"] == "backend"
    assert provenance["backend"]["entrypoint"] == expected["entrypoint"].removeprefix(
        "backend/"
    )
    assert provenance["backend"]["language"] == expected["language"]
    assert provenance["backend"]["tree_sha256"] == tree_sha256(backend)

    for path in backend.rglob("*"):
        relative = path.relative_to(backend)
        assert not (set(relative.parts) & BANNED_PARTS), relative
        assert path.suffix.lower() not in BANNED_SUFFIXES, relative
        assert path.name not in BANNED_NAMES, relative
        assert path.name not in {"main", "upload.py", "main_test.py", ".gitlab-ci.yml"}, relative


def test_known_and_unknown_upstream_revisions_are_honest() -> None:
    antwar = json.loads((ROOT / "games/antwar/PROVENANCE.json").read_text(encoding="utf-8"))
    rollman = json.loads((ROOT / "games/rollman/PROVENANCE.json").read_text(encoding="utf-8"))
    lostspace = json.loads(
        (ROOT / "games/lostspace/PROVENANCE.json").read_text(encoding="utf-8")
    )
    miracle = json.loads((ROOT / "games/miracle/PROVENANCE.json").read_text(encoding="utf-8"))

    assert antwar["source"]["repository"] == "https://git.tsinghua.edu.cn/agent-logic/ant_game"
    assert antwar["source"]["commit"] == "544a3a1809a5fd107057108f908e086f996790b5"
    assert rollman["source"]["repository"] == "git@github.com:PacMan-Logic/PacmanLogic.git"
    assert rollman["source"]["commit"] == "33f0d9ca123fb1dcc3307b945f2552869ca15290"
    assert lostspace["source"]["repository"] is None
    assert lostspace["source"]["commit"] is None
    assert miracle["source"]["repository"] is None
    assert miracle["source"]["commit"] is None


def test_antwar_clean_ladder_preserves_original_rank_gaps(tmp_path: Path) -> None:
    game_dir = ROOT / "games" / "antwar"
    evaluator = get_plugin("antwar", ROOT / "games").evaluator_factory(game_dir)
    package = evaluator.__class__.__module__.rsplit(".", 1)[0]
    runtime = importlib.import_module(f"{package}.runtime")
    layout = runtime.AntWarLayout.from_game_dir(game_dir, tmp_path)

    pool = runtime.audit_human_pool(layout)

    assert [opponent.rank for opponent in pool[:4]] == [1, 2, 3, 5]


def test_antwar_plugin_loads_without_any_sibling_gamepack(tmp_path: Path) -> None:
    isolated_games = tmp_path / "games"
    isolated_antwar = isolated_games / "antwar"
    isolated_antwar.mkdir(parents=True)
    shutil.copy2(ROOT / "games/antwar/plugin.py", isolated_antwar / "plugin.py")
    shutil.copytree(ROOT / "games/antwar/evaluator", isolated_antwar / "evaluator")

    command = (
        "from pathlib import Path; "
        "from aa_arena.core.registry import get_plugin; "
        "root = Path(__import__('sys').argv[1]); "
        "plugin = get_plugin('antwar', root / 'games'); "
        "evaluator = plugin.evaluator_factory(root / 'games' / 'antwar'); "
        "assert evaluator.game_dir == (root / 'games' / 'antwar').resolve()"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(ROOT / "src"), environment.get("PYTHONPATH")))
    )
    completed = subprocess.run(
        (sys.executable, "-c", command, str(tmp_path)),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("game", EXPECTED)
def test_layout_resolves_backend_inside_gamepack(game: str, tmp_path: Path) -> None:
    game_dir = ROOT / "games" / game
    evaluator = get_plugin(game, ROOT / "games").evaluator_factory(game_dir)
    package = evaluator.__class__.__module__.rsplit(".", 1)[0]
    runtime = importlib.import_module(f"{package}.runtime")
    layout_type = getattr(
        runtime,
        {
            "antwar": "AntWarLayout",
            "lostspace": "LostSpaceLayout",
            "miracle": "MiracleLayout",
            "rollman": "RollmanLayout",
        }[game],
    )
    args = (game_dir, tmp_path) if game != "rollman" else (game_dir,)

    layout = layout_type.from_game_dir(*args)

    assert layout.backend_source_root == (game_dir / "backend").resolve()
    layout.validate()


@pytest.mark.parametrize("game", EXPECTED)
def test_game_metadata_paths_resolve_within_gamepack(game: str) -> None:
    game_dir = ROOT / "games" / game
    metadata = yaml.safe_load((game_dir / "game.yaml").read_text(encoding="utf-8"))

    for key in ("rules", "decision_space", "replay_format", "evaluator", "players_manifest"):
        relative = Path(metadata[key])
        assert not relative.is_absolute()
        assert (game_dir / relative).exists()
    assert Path(metadata["backend_source"]) == Path("backend")
    assert (game_dir / metadata["provenance"]).resolve() == (game_dir / "PROVENANCE.json").resolve()
    for track in metadata["tracks"]:
        source_ladder = Path(track["source_ladder"])
        assert not source_ladder.is_absolute()
        assert (game_dir / source_ladder).exists()


def test_root_readme_documents_offline_assets_and_both_harnesses() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for command in ("scripts/install_assets.py", "scripts/run_experiments.py", "--harness claude", "AA_ARENA_PROFILE_DIR"):
        assert command in readme


def test_miracle_and_lostspace_docs_reference_bundled_paths() -> None:
    lostspace_rules = (ROOT / "games/lostspace/rules.md").read_text(encoding="utf-8")
    lostspace_decisions = (ROOT / "games/lostspace/decision_space.yaml").read_text(
        encoding="utf-8"
    )
    miracle_rules = (ROOT / "games/miracle/rules.md").read_text(encoding="utf-8")
    miracle_decisions = (ROOT / "games/miracle/decision_space.yaml").read_text(
        encoding="utf-8"
    )
    miracle_replay = (ROOT / "games/miracle/replay_format.md").read_text(encoding="utf-8")

    assert "judge_dev_sample_ai/" not in lostspace_rules + lostspace_decisions
    assert "judge_dev_sample_ai/" not in miracle_rules + miracle_decisions
    assert "public_sdk/legacy_main.py" in lostspace_decisions
    assert "public_sdk/ai_client.py" in miracle_decisions
    assert "gamecode_logic/main.py" not in miracle_replay
    assert "`backend/main.py`" in miracle_replay


@pytest.mark.parametrize(("game", "source"), PLAYER_POOL_SOURCES.items())
def test_player_pool_provenance_preserves_historical_sources(
    game: str, source: tuple[str, tuple[str, ...]]
) -> None:
    provenance = json.loads(
        (ROOT / "games" / game / "PROVENANCE.json").read_text(encoding="utf-8")
    )

    player_pool = provenance["player_pool"]
    assert player_pool["path"] == "players"
    assert player_pool["manifest"] == "players/manifest.tsv"
    assert player_pool["source_repository"] == "https://github.com/Aoraku/AgentBench"
    assert player_pool["source_commit"] == "1fb3cac2dabeb862fd7c610d5c44e23037e38d4e"
    source_slug, source_paths = source
    assert player_pool["source_game_slug"] == source_slug
    assert tuple(player_pool["historical_source_paths"]) == source_paths


def test_lostspace_sdk_provenance_distinguishes_legacy_and_reconstructed_files() -> None:
    provenance = json.loads(
        (ROOT / "games/lostspace/PROVENANCE.json").read_text(encoding="utf-8")
    )
    origins = {item["path"]: item["origin"] for item in provenance["sdk"]}

    assert origins["public_sdk/legacy_main.py"] == "historically_recovered"
    assert origins["public_sdk/main.py"] == "reconstructed_compatibility"
    assert origins["public_sdk/lostspace_sdk"] == "reconstructed_compatibility"


def test_all_public_sdks_are_documented_and_hash_verified() -> None:
    sdk_roots = (
        ROOT / "games/antwar/public_sdk",
        ROOT / "games/lostspace/public_sdk",
        ROOT / "games/miracle/public_sdk",
        ROOT / "games/rollman/public_sdk-rollman",
        ROOT / "games/rollman/public_sdk-ghost",
    )
    for sdk_root in sdk_roots:
        assert (sdk_root / "README.md").is_file()
        provenance = json.loads(
            (sdk_root / "SDK_PROVENANCE.json").read_text(encoding="utf-8")
        )
        assert provenance["payload_tree_sha256"] == tree_sha256(
            sdk_root, exclude_names=frozenset({"SDK_PROVENANCE.json"})
        )

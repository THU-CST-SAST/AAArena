from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import unittest
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "aquawar": {
        "repository": "https://github.com/Aoraku/AgentBench",
        "commit": "9d3eafb1fac113f7658d847a808d60deb5f65faa",
        "tree_sha256": "3dd67da720a7d5e6247f0b79b40fd3fa1474e92423313a30719ba89d2a2ca55e",
    },
    "generals": {
        "repository": "https://gitee.com/xiaoaojianghu425/generals_-impact_-sdk_-cpp",
        "commit": "ca0796b7d62d3399ef25e660a0309a064e6cfcc8",
        "tree_sha256": "21668f6639049b7052a6394dcb1147fd447aeb97e7a45517fe7ae2be64fc0e86",
    },
    "snakego": {
        "repository": "https://github.com/xsun2001/thuac2022-adk",
        "commit": "b8d8ea2178ade3827467421a81ad970913e4e202",
        "tree_sha256": "0b794ca7adcaf4e719b319b06e5b4ecd766eb1805ae4862620a53afc8999f23a",
    },
}

RECOVERED_TREE_SHA256 = {
    ("antwar", "include"): "63dd6e541f1e000c27af612a8517e3cf68a7e86ed8e70a0cb314c4e49dd76f99",
    ("lostspace", "jsoncpp"): "81dc2bb60521b5dbeeac456b2844784aed1398e5d3f0449efd67bdd9bd2d93ba",
    ("miracle", "v11"): "8abced1584536fb772f2fe9969a4373749659d2c3cd0965906766edaade2663b",
}
MIRACLE_CARD_H_SHA256 = "0de4ad21bb5b804ed138d5a300da2b4f1f8088fb59b8e4c71000ec907b38a5ee"
CATALOG_GAMES = ("antwar", "aquawar", "generals", "lostspace", "miracle", "snakego")
STRATEGY_OWNED_NAMES = {
    "main.cpp",
    "Action.cpp",
    "UCT.h",
    "Snakego.h",
    "astar.h",
    "ai-sample.cpp",
    "copy.cpp",
    "conio.h",
}


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in {
            "SDK_PROVENANCE.json",
            "native_sdk_catalog.json",
            "PROVENANCE.md",
        }:
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


@pytest.mark.parametrize("game", ("antwar", "lostspace", "miracle"))
def test_recovered_cpp_starter_builds_in_a_clean_copy(game: str, tmp_path: Path) -> None:
    source = REPOSITORY_ROOT / "games" / game / "public_sdk_cpp"
    package = tmp_path / f"{game}-cpp"
    shutil.copytree(source, package)

    completed = subprocess.run(
        ("make",), cwd=package, capture_output=True, text=True, check=False, timeout=120
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert (package / "main").is_file()



def _install_rollman_test_environment(package: Path) -> None:
    bridge = package / "rollman_bridge.py"
    if not bridge.is_file():
        return
    core = package / "core"
    core.mkdir(exist_ok=True)
    (core / "__init__.py").write_text("", encoding="utf-8")
    (core / "GymEnvironment.py").write_text(
        """class _State:
    def __init__(self, value): self.value = value
    def gamestate_to_statedict(self): return dict(self.value)
class PacmanEnv:
    def __init__(self): self.value = {}
    def ai_reset(self, value): self.value = value
    def game_state(self): return _State(self.value)
    def step(self, pacman, ghosts):
        return {}, 0, 0, self.value.get("level") == 1, False
""",
        encoding="utf-8",
    )


def _starter_command(package: Path) -> tuple[tuple[str, ...], dict[str, str] | None]:
    if (package / "rollman_bridge.py").is_file():
        environment = dict(os.environ)
        environment["AA_ARENA_CPP_STRATEGY"] = str(package / "main")
        return (sys.executable, "rollman_bridge.py"), environment
    return (str(package / "main"),), None


def _build_and_run_first_packet(
    source: Path, package: Path, stdin: bytes
) -> tuple[subprocess.CompletedProcess[bytes], bytes]:
    shutil.copytree(source, package)
    _install_rollman_test_environment(package)
    built = subprocess.run(
        ("make",), cwd=package, capture_output=True, text=True, check=False, timeout=120
    )
    assert built.returncode == 0, built.stderr or built.stdout
    command, environment = _starter_command(package)
    completed = subprocess.run(
        command,
        cwd=package,
        env=environment,
        input=stdin,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert len(completed.stdout) >= 4
    length = struct.unpack(">I", completed.stdout[:4])[0]
    return completed, completed.stdout[4 : 4 + length]


@pytest.mark.parametrize(
    ("seat", "stdin"),
    ((0, b"0 123\n"), (1, b"1 123\n0\n")),
)
def test_antwar2_cpp_starter_emits_big_endian_noop_packet(
    seat: int, stdin: bytes, tmp_path: Path
) -> None:
    del seat
    _completed, packet = _build_and_run_first_packet(
        REPOSITORY_ROOT / "games" / "antwar2" / "public_sdk_cpp",
        tmp_path / "antwar2-cpp",
        stdin,
    )

    assert packet == b"0\n"


@pytest.mark.parametrize(
    ("role", "directory", "expected"),
    (
        ("rollman", "public_sdk-cpp-rollman", {"role": 0, "action": "0"}),
        ("ghost", "public_sdk-cpp-ghost", {"role": 1, "action": "0 0 0"}),
    ),
)
def test_rollman_cpp_starters_emit_length_prefixed_role_actions(
    role: str, directory: str, expected: dict[str, object], tmp_path: Path
) -> None:
    del role
    _completed, packet = _build_and_run_first_packet(
        REPOSITORY_ROOT / "games" / "rollman" / directory,
        tmp_path / directory,
        b"0\n{}\n",
    )

    assert json.loads(packet) == expected



def test_antwar2_cpp_starter_strategy_callback_controls_packet(tmp_path: Path) -> None:
    source = REPOSITORY_ROOT / "games" / "antwar2" / "public_sdk_cpp"
    package = tmp_path / "antwar2-custom"
    shutil.copytree(source, package)
    main = package / "main.cpp"
    text = main.read_text(encoding="utf-8")
    assert "return {};" in text
    main.write_text(
        text.replace('return {};', 'return {"2 7"};', 1),
        encoding="utf-8",
    )
    built = subprocess.run(
        ("make",), cwd=package, capture_output=True, text=True, check=False, timeout=120
    )
    assert built.returncode == 0, built.stderr or built.stdout
    completed = subprocess.run(
        (str(package / "main"),),
        cwd=package,
        input=b"0 123\n",
        capture_output=True,
        check=False,
        timeout=10,
    )
    length = struct.unpack(">I", completed.stdout[:4])[0]

    assert completed.stdout[4 : 4 + length] == b"1\n2 7\n"



@pytest.mark.parametrize(
    ("directory", "replacement", "expected"),
    (
        ("public_sdk-cpp-rollman", 'return "1";', {"role": 0, "action": "1"}),
        ("public_sdk-cpp-ghost", 'return "1 2 3";', {"role": 1, "action": "1 2 3"}),
    ),
)
def test_rollman_cpp_strategy_callbacks_control_role_packets(
    directory: str, replacement: str, expected: dict[str, object], tmp_path: Path
) -> None:
    source = REPOSITORY_ROOT / "games" / "rollman" / directory
    package = tmp_path / directory
    shutil.copytree(source, package)
    main = package / "main.cpp"
    text = main.read_text(encoding="utf-8")
    assert "return kAction;" in text
    main.write_text(
        text.replace("return kAction;", replacement, 1),
        encoding="utf-8",
    )
    built = subprocess.run(
        ("make",), cwd=package, capture_output=True, text=True, check=False, timeout=120
    )
    assert built.returncode == 0, built.stderr or built.stdout
    _install_rollman_test_environment(package)
    command, environment = _starter_command(package)
    completed = subprocess.run(
        command,
        cwd=package,
        env=environment,
        input=b"0\n{}\n",
        capture_output=True,
        check=False,
        timeout=10,
    )
    length = struct.unpack(">I", completed.stdout[:4])[0]

    assert json.loads(completed.stdout[4 : 4 + length]) == expected



@pytest.mark.parametrize(
    ("directory", "changed_action"),
    (
        ("public_sdk-cpp-rollman", "1"),
        ("public_sdk-cpp-ghost", "1 2 3"),
    ),
)
def test_rollman_cpp_seat1_callback_retains_level_initialization(
    directory: str, changed_action: str, tmp_path: Path
) -> None:
    source = REPOSITORY_ROOT / "games" / "rollman" / directory
    package = tmp_path / directory
    shutil.copytree(source, package)
    main = package / "main.cpp"
    text = main.read_text(encoding="utf-8")
    assert "const Observation &observation" in text
    main.write_text(
        text.replace(
            "return kAction;",
            (
                'return observation.raw_json.find("\\\"level\\\":2") '
                '!= std::string::npos ? "'
                + changed_action
                + '" : kAction;'
            ),
            1,
        ),
        encoding="utf-8",
    )
    built = subprocess.run(
        ("make",), cwd=package, capture_output=True, text=True, check=False, timeout=120
    )
    assert built.returncode == 0, built.stderr or built.stdout
    _install_rollman_test_environment(package)
    command, environment = _starter_command(package)
    completed = subprocess.run(
        command,
        cwd=package,
        env=environment,
        input=(
            b'1\n{"board":[],"level":1}\n'
            b"player 0 send info\n"
            b'{"pacman_action":0,"ghosts_action":[0,0,0]}\n'
            b'{"board":[],"level":2}\n'
            b"player 0 send info\n"
        ),
        capture_output=True,
        check=False,
        timeout=10,
    )
    packets = []
    offset = 0
    while offset < len(completed.stdout):
        length = struct.unpack(">I", completed.stdout[offset : offset + 4])[0]
        offset += 4
        packets.append(json.loads(completed.stdout[offset : offset + length]))
        offset += length

    assert packets[0]["action"] != changed_action
    assert packets[1]["action"] == changed_action



@pytest.mark.parametrize(
    ("directory", "changed_action"),
    (
        ("public_sdk-cpp-rollman", "1"),
        ("public_sdk-cpp-ghost", "1 2 3"),
    ),
)
def test_rollman_cpp_seat0_waits_for_new_level_before_callback(
    directory: str, changed_action: str, tmp_path: Path
) -> None:
    source = REPOSITORY_ROOT / "games" / "rollman" / directory
    package = tmp_path / directory
    shutil.copytree(source, package)
    main = package / "main.cpp"
    text = main.read_text(encoding="utf-8")
    main.write_text(
        text.replace(
            "return kAction;",
            (
                'return observation.raw_json.find("\\\"level\\\":2") '
                '!= std::string::npos ? "'
                + changed_action
                + '" : kAction;'
            ),
            1,
        ),
        encoding="utf-8",
    )
    built = subprocess.run(
        ("make",), cwd=package, capture_output=True, text=True, check=False, timeout=120
    )
    assert built.returncode == 0, built.stderr or built.stdout
    _install_rollman_test_environment(package)
    command, environment = _starter_command(package)
    completed = subprocess.run(
        command,
        cwd=package,
        env=environment,
        input=(
            b'0\n{"board":[],"level":1}\n'
            b"player 1 send info\n"
            b'{"pacman_action":0,"ghosts_action":[0,0,0]}\n'
            b'{"board":[],"level":2}\n'
            b"player 1 send info\n"
            b'{"pacman_action":0,"ghosts_action":[0,0,0]}\n'
        ),
        capture_output=True,
        check=False,
        timeout=10,
    )
    packets = []
    offset = 0
    while offset < len(completed.stdout):
        length = struct.unpack(">I", completed.stdout[offset : offset + 4])[0]
        offset += 4
        packets.append(json.loads(completed.stdout[offset : offset + length]))
        offset += length

    assert packets[0]["action"] != changed_action
    assert packets[1]["action"] == changed_action


class PublicCppSdkTest(unittest.TestCase):
    def test_native_sdks_match_pinned_official_sources(self) -> None:
        for game, expected in EXPECTED.items():
            with self.subTest(game=game):
                root = REPOSITORY_ROOT / "games" / game / "public_sdk_cpp"
                self.assertTrue(root.is_dir(), f"missing public C++ SDK: {root}")
                provenance = json.loads((root / "SDK_PROVENANCE.json").read_text(encoding="utf-8"))
                self.assertEqual(provenance["source_repository"], expected["repository"])
                self.assertEqual(provenance["source_commit"], expected["commit"])
                self.assertEqual(provenance["tree_sha256"], expected["tree_sha256"])
                self.assertEqual(_tree_sha256(root), expected["tree_sha256"])

    def test_recovered_sdk_trees_match_audited_player_pool_bytes(self) -> None:
        for (game, subtree), expected in RECOVERED_TREE_SHA256.items():
            with self.subTest(game=game, subtree=subtree):
                root = REPOSITORY_ROOT / "games" / game / "public_sdk_cpp" / subtree
                self.assertTrue(root.is_dir(), f"missing recovered SDK tree: {root}")
                self.assertEqual(_tree_sha256(root), expected)

        card = (
            REPOSITORY_ROOT
            / "games"
            / "miracle"
            / "public_sdk_cpp_compat"
            / "card-h-v1"
            / "card.h"
        )
        self.assertTrue(card.is_file(), f"missing recovered compatibility header: {card}")
        self.assertEqual(hashlib.sha256(card.read_bytes()).hexdigest(), MIRACLE_CARD_H_SHA256)

    def test_native_sdk_catalogs_pin_existing_files_and_hashes(self) -> None:
        for game in CATALOG_GAMES:
            with self.subTest(game=game):
                sdk_root = REPOSITORY_ROOT / "games" / game / "public_sdk_cpp"
                catalog_path = sdk_root / "native_sdk_catalog.json"
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                self.assertEqual(catalog["schema_version"], "1.0")
                self.assertEqual(catalog["game"], game)
                self.assertTrue(catalog["bundles"])
                for bundle in catalog["bundles"]:
                    self.assertTrue(bundle["bundle_id"])
                    self.assertEqual(len(bundle["fingerprint_sha256"]), 64)
                    self.assertTrue(bundle["files"])
                    source_root = (
                        REPOSITORY_ROOT
                        / "games"
                        / game
                        / bundle.get("source_root", "public_sdk_cpp")
                    )
                    for item in bundle["files"]:
                        source = source_root / item["source"]
                        self.assertTrue(source.is_file(), f"missing catalog source: {source}")
                        actual = hashlib.sha256(source.read_bytes()).hexdigest()
                        self.assertEqual(actual, item["sha256"])

    def test_native_sdk_catalogs_do_not_expose_strategy_owned_files(self) -> None:
        for game in CATALOG_GAMES:
            catalog_path = (
                REPOSITORY_ROOT
                / "games"
                / game
                / "public_sdk_cpp"
                / "native_sdk_catalog.json"
            )
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            exposed = {
                Path(item["requested_path"]).name
                for bundle in catalog["bundles"]
                for item in bundle["files"]
            }
            self.assertFalse(
                exposed & STRATEGY_OWNED_NAMES,
                f"{game} exposes strategy-owned files: {sorted(exposed & STRATEGY_OWNED_NAMES)}",
            )


if __name__ == "__main__":
    unittest.main()

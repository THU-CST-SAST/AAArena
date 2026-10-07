from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path

import pytest

from aa_arena.core import cpp_build
from aa_arena.core.build_sandbox import BUILD_SANDBOX_POLICY_VERSION, run_isolated_build
from aa_arena.core.cpp_build import CppBuildError, compile_cpp_package, native_build_root

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    shutil.which("bwrap", path="/usr/bin:/bin") is None,
    reason="bubblewrap is unavailable",
)


@pytest.mark.parametrize("recipe", ["make", "cmake"])
def test_recipe_cannot_access_host_files_network_or_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recipe: str,
) -> None:
    # These are deliberately created public test sentinels, never real secrets.
    private = tmp_path / "controller-private.txt"
    private.write_text("BUILD_TEST_SENTINEL")
    source = tmp_path / "candidate"
    source.mkdir()
    monkeypatch.setenv("BUILD_TEST_HOST_ENV", "host-value")
    monkeypatch.setenv("CPATH", str(tmp_path))
    (source / "main.cpp").write_text("int main() { return 0; }\n")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        (source / "probe.py").write_text(f"""
import json, os, pathlib, socket, sys
p = pathlib.Path({str(private)!r})
r = {{'uid': os.getuid(), 'gid': os.getgid(),
     'env_hidden': 'BUILD_TEST_HOST_ENV' not in os.environ and 'CPATH' not in os.environ}}
for label, mode in [('read_blocked', 'r'), ('write_blocked', 'r+')]:
    try:
        with p.open(mode) as f:
            f.read() if mode == 'r' else f.write('ESCAPED')
        r[label] = False
    except OSError:
        r[label] = True
try:
    with socket.create_connection(('127.0.0.1', {port}), timeout=0.2):
        r['network_blocked'] = False
except OSError:
    r['network_blocked'] = True
r['root_caps_dropped'] = 'CapEff:\\t0000000000000000' in pathlib.Path('/proc/self/status').read_text()
pathlib.Path(sys.argv[1]).write_text(json.dumps(r))
""")
        if recipe == "make":
            (source / "Makefile").write_text(
                "all:\n\t/usr/bin/python3 probe.py make.json\n\tg++ main.cpp -o main\n"
            )
            reports = ["make.json"]
        else:
            (source / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.16)\nproject(probe LANGUAGES CXX)\n"
                "execute_process(COMMAND /usr/bin/python3 probe.py configure.json "
                'WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")\n'
                "add_custom_target(probe ALL COMMAND /usr/bin/python3 probe.py build.json "
                'WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")\n'
                "add_executable(main main.cpp)\n"
            )
            reports = ["configure.json", "build.json"]
        built = compile_cpp_package(source, tmp_path / "cache")
        root = native_build_root(built)
        for report in reports:
            assert json.loads((root / report).read_text()) == {
                "uid": 65534,
                "gid": 65534,
                "env_hidden": True,
                "read_blocked": True,
                "write_blocked": True,
                "network_blocked": True,
                "root_caps_dropped": True,
            }
        assert private.read_text() == "BUILD_TEST_SENTINEL"
        listener.settimeout(0.05)
        with pytest.raises(TimeoutError):
            listener.accept()
        metadata = json.loads((root / cpp_build.NATIVE_BUILD_METADATA).read_text())
        assert metadata["build_sandbox_policy_version"] == BUILD_SANDBOX_POLICY_VERSION
        assert compile_cpp_package(source, tmp_path / "cache") == built


@pytest.mark.parametrize("recipe", ["make", "cmake", "source"])
def test_compiler_cannot_include_host_private_file(tmp_path: Path, recipe: str) -> None:
    header = tmp_path / "private.hpp"
    header.write_text("#define SENTINEL_BUILD_VALUE 17421\n")
    source = tmp_path / "candidate"
    source.mkdir()
    (source / "main.cpp").write_text(
        f'#include "{header}"\nint main() {{ return SENTINEL_BUILD_VALUE; }}\n'
    )
    if recipe == "make":
        (source / "Makefile").write_text("all:\n\tg++ main.cpp -o main\n")
    elif recipe == "cmake":
        (source / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.16)\nproject(probe LANGUAGES CXX)\n"
            "add_executable(main main.cpp)\n"
        )
    with pytest.raises(CppBuildError) as error:
        compile_cpp_package(source, tmp_path / "cache")
    assert "No such file or directory" in str(error.value)
    assert "17421" not in str(error.value)
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("entry", ["source-link", "metadata-link", "fifo"])
def test_candidate_links_and_special_files_never_reach_host_processing(
    tmp_path: Path,
    entry: str,
) -> None:
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("untouched")
    source = tmp_path / "candidate"
    source.mkdir()
    (source / "main.cpp").write_text("int main() { return 0; }\n")
    if entry == "source-link":
        (source / "private.hpp").symlink_to(sentinel)
    else:
        recipe = (
            f"ln -s {sentinel} {cpp_build.NATIVE_BUILD_METADATA}"
            if entry == "metadata-link"
            else "mkfifo trap.hpp"
        )
        (source / "Makefile").write_text(f"all:\n\t{recipe}\n\tg++ main.cpp -o main\n")
    with pytest.raises(CppBuildError, match="unsafe build tree entry"):
        compile_cpp_package(source, tmp_path / "cache")
    assert sentinel.read_text() == "untouched"
    assert not (tmp_path / "cache").exists()


def test_missing_bwrap_never_runs_recipe_on_host(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "candidate"
    source.mkdir()
    sentinel = tmp_path / "escaped"
    (source / "main.cpp").write_text("int main() { return 0; }\n")
    (source / "Makefile").write_text(f"all:\n\ttouch {sentinel}\n\tg++ main.cpp -o main\n")
    monkeypatch.setattr("aa_arena.core.build_sandbox.shutil.which", lambda *a, **k: None)
    with pytest.raises(CppBuildError, match="requires bubblewrap"):
        compile_cpp_package(source, tmp_path / "cache")
    assert not sentinel.exists()


def test_compatibility_fallback_mounts_only_the_declared_shim(tmp_path: Path, monkeypatch) -> None:
    shim = tmp_path / "generated_compat.h"
    shim.write_text("#define COMPAT_READY 1\n")
    monkeypatch.setattr(cpp_build, "_COMPAT_SHIM", shim)
    source = tmp_path / "candidate"
    source.mkdir()
    (source / "main.cpp").write_text(
        "#ifndef COMPAT_READY\n#error random_shuffle not declared\n#endif\n"
        "int main() { return 0; }\n"
    )
    (source / "Makefile").write_text("all:\n\tg++ main.cpp -o main\n")
    assert compile_cpp_package(source, tmp_path / "cache").is_file()


@pytest.mark.parametrize(
    "game_name", ["antwar", "aquawar", "generals", "lostspace", "miracle", "snakego"]
)
def test_declared_public_sdk_compilation(tmp_path: Path, game_name: str) -> None:
    game = ROOT / "games" / game_name
    built = cpp_build.build_cpp_player(
        player_dir=game / "public_sdk_cpp",
        game_dir=game,
        cache_root=tmp_path / "cache",
    )
    metadata = json.loads((native_build_root(built) / cpp_build.NATIVE_BUILD_METADATA).read_text())
    assert built.is_file()
    assert metadata["source_sha256"]
    assert metadata["sdk_catalog_identity"]
    assert metadata["cache_identity"]


def test_dorado_declared_sdk_compiles_shared_player(tmp_path: Path) -> None:
    # Exercise the exact SDK paths used by Dorado without changing its caller.
    from games.dorado.evaluator.runtime import DoradoLayout

    game = ROOT / "games/dorado"
    layout = DoradoLayout.from_game_dir(game, tmp_path / "cache")
    player = next(iter(sorted(layout.player_pool_root.glob("*/ai.cpp"))))
    staging = tmp_path / "staging"
    shutil.copytree(player.parent, staging)
    result = run_isolated_build(
        [
            "g++",
            "-std=c++14",
            "-O2",
            "-fPIC",
            "-shared",
            "-I",
            str(layout.sdk_include_root),
            "ai.cpp",
            *(str(path) for path in layout.sdk_sources),
            "-o",
            "player.so",
        ],
        cwd=staging,
        # Dorado's SDK includes ../Pos.h and its .cpp units include
        # include/trans.h and include/ActMaker/*.h. The full declared header
        # tree is required, but the backend source tree is not mounted.
        readonly_paths=(layout.sdk_include_root.parent, *layout.sdk_sources),
    )
    assert result.returncode == 0, result.stderr
    assert (staging / "player.so").is_file()


def test_unsafe_cmake_output_stops_before_host_source_fallback(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "candidate"
    source.mkdir()
    sentinel = tmp_path / "private.cpp"
    sentinel.write_text("test sentinel, not candidate source")
    (source / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\nproject(probe NONE)\n"
        f'execute_process(COMMAND /bin/ln -s "{sentinel}" '
        '"${CMAKE_CURRENT_SOURCE_DIR}/main.cpp")\n'
        'message(FATAL_ERROR "failed candidate configure")\n'
    )

    def forbidden_fallback(*args, **kwargs):
        pytest.fail("unsafe CMake output reached host source scanning")

    monkeypatch.setattr(cpp_build, "_compile_source_only", forbidden_fallback)
    with pytest.raises(CppBuildError, match="unsafe build tree entry"):
        compile_cpp_package(source, tmp_path / "cache")


@pytest.mark.parametrize(
    "game,package",
    [
        ("antwar2", "public_sdk_cpp"),
        ("rollman", "public_sdk-cpp-rollman"),
        ("rollman", "public_sdk-cpp-ghost"),
    ],
)
def test_generic_game_sdk_uses_isolated_builder(game: str, package: str, tmp_path: Path) -> None:
    import importlib

    runtime = importlib.import_module(f"games.{game}.evaluator.runtime")
    executable = runtime.build_player(ROOT / "games" / game / package, tmp_path / "cache")
    metadata = json.loads(
        (native_build_root(executable) / cpp_build.NATIVE_BUILD_METADATA).read_text()
    )
    assert executable.is_file()
    assert metadata["build_sandbox_policy_version"] == BUILD_SANDBOX_POLICY_VERSION

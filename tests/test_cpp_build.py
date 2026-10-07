from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from aa_arena.core.cpp_build import (
    CPP_STANDARDS,
    NATIVE_BUILD_METADATA,
    SOURCE_BUILD_POLICY_VERSION,
    CppBuildError,
    ExecutableSnapshot,
    build_cpp_player,
    compile_cpp_package,
    discover_built_executable,
    parse_missing_build_path,
)


def _make_executable(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_game_catalog(
    game_dir: Path,
    files: dict[str, bytes],
    *,
    bundle_id: str = "test-sdk-v1",
    fingerprint: str = "a" * 64,
) -> None:
    sdk_root = game_dir / "public_sdk_cpp"
    catalog_files = []
    for requested_path, content in files.items():
        source_name = f"files/{requested_path}"
        source = sdk_root / source_name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        catalog_files.append(
            {
                "requested_path": requested_path,
                "source": source_name,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    (sdk_root / "native_sdk_catalog.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "game": game_dir.name,
                "bundles": [
                    {
                        "bundle_id": bundle_id,
                        "fingerprint_sha256": fingerprint,
                        "files": catalog_files,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _published_build_root(executable: Path) -> Path:
    for parent in executable.parents:
        if (parent / NATIVE_BUILD_METADATA).is_file():
            return parent
    raise AssertionError(f"no native build metadata above {executable}")


def test_make_build_discovers_a_non_main_output(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "bot.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (source / "Makefile").write_text(
        "all:\n\tg++ -std=c++17 bot.cpp -o bot\n",
        encoding="utf-8",
    )
    _make_executable(source / "developer-tool")

    executable = compile_cpp_package(source, tmp_path / "build")
    reused = compile_cpp_package(source, tmp_path / "build")

    assert executable.name == "bot"
    assert reused == executable
    assert subprocess.run((str(executable),), check=False).returncode == 0


def test_cmake_build_discovers_a_non_main_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_cmake = _make_executable(
        fake_bin / "cmake",
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-S\" ]; then\n"
        "  mkdir -p \"$4\"\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$1\" = \"--build\" ]; then\n"
        "  printf '#!/bin/sh\\nexit 0\\n' > \"$2/player_ai\"\n"
        "  chmod +x \"$2/player_ai\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
    )
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    assert fake_cmake.is_file()
    source = tmp_path / "source"
    source.mkdir()
    (source / "player.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (source / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\n"
        "project(player LANGUAGES CXX)\n"
        "add_executable(player_ai player.cpp)\n",
        encoding="utf-8",
    )

    executable = compile_cpp_package(source, tmp_path / "build")

    assert executable.name == "player_ai"
    assert subprocess.run((str(executable),), check=False).returncode == 0


def test_cmake_build_falls_back_to_source_only_when_cmake_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "player.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (source / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\n"
        "project(player LANGUAGES CXX)\n"
        "add_executable(player_ai player.cpp)\n",
        encoding="utf-8",
    )
    def missing_cmake(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise CppBuildError("cmake executable is unavailable")

    monkeypatch.setattr("aa_arena.core.cpp_build._compile_with_cmake", missing_cmake)

    executable = compile_cpp_package(source, tmp_path / "build")

    assert ".ahl-source-build" in executable.parts
    assert subprocess.run((str(executable),), check=False).returncode == 0


def test_failed_make_recipe_falls_back_to_source_only(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "player.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (source / "Makefile").write_text(
        "player: player.cpp\n\t@echo stale recipe >&2\n\t@false\n",
        encoding="utf-8",
    )

    executable = compile_cpp_package(source, tmp_path / "build")

    assert ".ahl-source-build" in executable.parts
    assert subprocess.run((str(executable),), check=False).returncode == 0


def test_make_recipe_missing_a_strategy_source_falls_back_to_source_only(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (source / "Makefile").write_text(
        "main: missing_strategy.cpp\n\tg++ missing_strategy.cpp -o main\n",
        encoding="utf-8",
    )

    executable = compile_cpp_package(source, tmp_path / "build")

    assert ".ahl-source-build" in executable.parts
    assert subprocess.run((str(executable),), check=False).returncode == 0


def test_discovery_excludes_preexisting_undeclared_executables(
    tmp_path: Path,
) -> None:
    root = tmp_path / "build"
    old = _make_executable(root / "old-tool")
    before = ExecutableSnapshot.capture(root)
    new = _make_executable(root / "bot")

    assert discover_built_executable(root, before, ()) == new
    assert old != new


def test_discovery_accepts_one_new_regular_executable(tmp_path: Path) -> None:
    root = tmp_path / "build"
    root.mkdir()
    before = ExecutableSnapshot.capture(root)
    executable = _make_executable(root / "nested/player")

    assert discover_built_executable(root, before, ()) == executable


def test_discovery_accepts_one_exact_declared_target(tmp_path: Path) -> None:
    root = tmp_path / "build"
    declared = _make_executable(root / "bin/player")
    before = ExecutableSnapshot.capture(root)

    assert discover_built_executable(root, before, (declared,)) == declared


def test_discovery_rejects_multiple_equally_valid_outputs(tmp_path: Path) -> None:
    root = tmp_path / "build"
    root.mkdir()
    before = ExecutableSnapshot.capture(root)
    _make_executable(root / "first")
    _make_executable(root / "second")

    with pytest.raises(CppBuildError, match="ambiguous"):
        discover_built_executable(root, before, ())


def test_discovery_rejects_a_declared_target_outside_the_build_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "build"
    root.mkdir()
    outside = _make_executable(tmp_path / "outside")

    with pytest.raises(CppBuildError, match="outside build root"):
        discover_built_executable(
            root,
            ExecutableSnapshot.capture(root),
            (outside,),
        )


def test_discovery_ignores_compiler_probes_and_object_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "build"
    root.mkdir()
    before = ExecutableSnapshot.capture(root)
    _make_executable(root / "CMakeFiles/CompilerIdCXX/a.out")
    object_file = _make_executable(root / "player.o")
    player = _make_executable(root / "player")

    assert os.access(object_file, os.X_OK)
    assert discover_built_executable(root, before, ()) == player


def test_source_only_cpp_package_compiles_without_a_build_recipe(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.cpp").write_text(
        "#include <iostream>\n"
        "int main() { std::cout << \"ok\"; return 0; }\n",
        encoding="utf-8",
    )

    executable = compile_cpp_package(source, tmp_path / "build")

    completed = subprocess.run(
        (str(executable),),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout == "ok"


def test_source_only_build_accepts_relative_source_and_cache_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = Path("source")
    source.mkdir()
    (source / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")

    executable = compile_cpp_package(source, Path("cache/build"))

    assert executable.is_absolute()
    assert subprocess.run((str(executable),), check=False).returncode == 0


def test_source_only_build_links_helper_translation_units(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.cpp").write_text(
        "int answer();\nint main() { return answer() == 42 ? 0 : 1; }\n",
        encoding="utf-8",
    )
    (source / "answer.cpp").write_text(
        "int answer() { return 42; }\n",
        encoding="utf-8",
    )

    executable = compile_cpp_package(source, tmp_path / "build")

    assert subprocess.run((str(executable),), check=False).returncode == 0


def test_source_only_build_searches_player_header_directories(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    include = source / "include"
    include.mkdir(parents=True)
    (include / "entry.hpp").write_text(
        "inline int result() { return 0; }\n",
        encoding="utf-8",
    )
    (source / "control.cpp").write_text(
        "#include \"../include/entry.hpp\"\n"
        "int main() { return result(); }\n",
        encoding="utf-8",
    )

    executable = compile_cpp_package(source, tmp_path / "build")

    assert subprocess.run((str(executable),), check=False).returncode == 0


def test_source_only_build_does_not_compile_a_textually_included_cpp_twice(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.cpp").write_text(
        "#include \"implementation.cpp\"\n"
        "int main() { return answer() == 7 ? 0 : 1; }\n",
        encoding="utf-8",
    )
    (source / "implementation.cpp").write_text(
        "int answer() { return 7; }\n",
        encoding="utf-8",
    )

    executable = compile_cpp_package(source, tmp_path / "build")

    assert subprocess.run((str(executable),), check=False).returncode == 0


def test_source_only_c_package_is_compiled_as_c(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.c").write_text(
        "int main(void) { return _Generic(1, int: 0, default: 1); }\n",
        encoding="utf-8",
    )

    executable = compile_cpp_package(source, tmp_path / "build")

    assert subprocess.run((str(executable),), check=False).returncode == 0


def test_source_only_build_accepts_exactly_one_working_main_candidate(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "good.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (source / "broken.cpp").write_text(
        "int main() { return symbol_that_does_not_exist; }\n",
        encoding="utf-8",
    )

    executable = compile_cpp_package(source, tmp_path / "build")

    assert subprocess.run((str(executable),), check=False).returncode == 0


def test_source_only_build_rejects_multiple_working_main_candidates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "first.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (source / "second.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")

    with pytest.raises(CppBuildError, match="ambiguous source-only main"):
        compile_cpp_package(source, tmp_path / "build")


def test_source_only_build_reports_no_entry_point(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "helper.cpp").write_text("int answer() { return 42; }\n", encoding="utf-8")

    with pytest.raises(CppBuildError, match="no_entry_point"):
        compile_cpp_package(source, tmp_path / "build")


def test_source_only_build_attempts_cpp_standards_in_declared_order(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.cpp").write_text(
        "int main() { this is not valid C++; }\n",
        encoding="utf-8",
    )

    with pytest.raises(CppBuildError) as captured:
        compile_cpp_package(source, tmp_path / "build")

    diagnostic = str(captured.value)
    positions = [diagnostic.index(f"standard={standard}") for standard in CPP_STANDARDS]
    assert positions == sorted(positions)
    assert SOURCE_BUILD_POLICY_VERSION == 2


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    (
        (
            "main.cpp:1:10: fatal error: sdk/one.hpp: No such file or directory",
            "sdk/one.hpp",
        ),
        (
            "make: *** No rule to make target 'sdk/helper.cpp', needed by 'main'. Stop.",
            "sdk/helper.cpp",
        ),
        (
            "CMake Error: Cannot find source file:\n  sdk/helper.cpp\n",
            "sdk/helper.cpp",
        ),
    ),
)
def test_missing_build_path_parser_handles_supported_build_tools(
    diagnostic: str,
    expected: str,
) -> None:
    assert parse_missing_build_path(diagnostic) == expected


def test_build_cpp_player_recovers_only_iteratively_missing_sdk_files(
    tmp_path: Path,
) -> None:
    game_dir = tmp_path / "game"
    _write_game_catalog(
        game_dir,
        {
            "sdk/one.hpp": b"#pragma once\n#define ONE 1\n",
            "sdk/two.hpp": b"#pragma once\n#define TWO 2\n",
            "sdk/unused.hpp": b"#pragma once\n#define UNUSED 3\n",
        },
    )
    player = tmp_path / "player"
    player.mkdir()
    (player / "main.cpp").write_text(
        "#include \"sdk/one.hpp\"\n"
        "#include \"sdk/two.hpp\"\n"
        "int main() { return ONE + TWO == 3 ? 0 : 1; }\n",
        encoding="utf-8",
    )
    source_before = hashlib.sha256((player / "main.cpp").read_bytes()).hexdigest()

    executable = build_cpp_player(
        player_dir=player,
        game_dir=game_dir,
        cache_root=tmp_path / "cache",
    )

    build_root = _published_build_root(executable)
    assert subprocess.run((str(executable),), check=False).returncode == 0
    assert (build_root / "sdk/one.hpp").is_file()
    assert (build_root / "sdk/two.hpp").is_file()
    assert not (build_root / "sdk/unused.hpp").exists()
    assert not (player / "sdk").exists()
    assert hashlib.sha256((player / "main.cpp").read_bytes()).hexdigest() == source_before
    metadata = json.loads((build_root / NATIVE_BUILD_METADATA).read_text())
    assert [item["requested_path"] for item in metadata["applied_sdk_files"]] == [
        "sdk/one.hpp",
        "sdk/two.hpp",
    ]
    assert metadata["source_build_policy_version"] == 2
    assert metadata["sdk_catalog_identity"]
    assert metadata["compiler_identity"]["g++"]


def test_build_cpp_player_resolves_a_missing_include_relative_to_its_includer(
    tmp_path: Path,
) -> None:
    game_dir = tmp_path / "game"
    _write_game_catalog(
        game_dir,
        {"template.hpp": b"#pragma once\n#define RESULT 0\n"},
    )
    player = tmp_path / "player"
    entry = player / "example/main.cpp"
    entry.parent.mkdir(parents=True)
    entry.write_text(
        "#include \"../include/template.hpp\"\n"
        "int main() { return RESULT; }\n",
        encoding="utf-8",
    )

    executable = build_cpp_player(
        player_dir=player,
        game_dir=game_dir,
        cache_root=tmp_path / "cache",
    )

    build_root = _published_build_root(executable)
    assert (build_root / "include/template.hpp").is_file()
    assert subprocess.run((str(executable),), check=False).returncode == 0


def test_build_cpp_player_nests_a_transitive_sdk_include_beside_its_includer(
    tmp_path: Path,
) -> None:
    game_dir = tmp_path / "game"
    _write_game_catalog(
        game_dir,
        {
            "sdk/client.hpp": (
                b"#pragma once\n#include \"jsoncpp/json/json.h\"\n"
                b"#define RESULT JSON_RESULT\n"
            ),
            "jsoncpp/json/json.h": b"#pragma once\n#define JSON_RESULT 0\n",
        },
    )
    player = tmp_path / "player"
    player.mkdir()
    (player / "main.cpp").write_text(
        "#include \"sdk/client.hpp\"\nint main() { return RESULT; }\n",
        encoding="utf-8",
    )

    executable = build_cpp_player(
        player_dir=player,
        game_dir=game_dir,
        cache_root=tmp_path / "cache",
    )

    build_root = _published_build_root(executable)
    assert (build_root / "sdk/client.hpp").is_file()
    assert (build_root / "sdk/jsoncpp/json/json.h").is_file()
    assert not (build_root / "jsoncpp/json/json.h").exists()
    assert subprocess.run((str(executable),), check=False).returncode == 0


def test_build_cpp_player_resolves_relative_make_diagnostics_from_make_cwd(
    tmp_path: Path,
) -> None:
    game_dir = tmp_path / "game"
    sdk_header = b"#pragma once\n#define RESULT 0\n"
    _write_game_catalog(
        game_dir,
        {
            "sdk.hpp": sdk_header,
            "include/sdk.hpp": sdk_header,
        },
    )
    player = tmp_path / "player"
    make_dir = player / "include"
    make_dir.mkdir(parents=True)
    (make_dir / "sdk.hpp").write_bytes(sdk_header)
    (make_dir / "main.cpp").write_text(
        '#include "include/sdk.hpp"\nint main() { return RESULT; }\n',
        encoding="utf-8",
    )
    (make_dir / "Makefile").write_text(
        "all:\n\tg++ -std=c++17 -I. main.cpp -o main\n",
        encoding="utf-8",
    )

    executable = build_cpp_player(
        player_dir=player,
        game_dir=game_dir,
        cache_root=tmp_path / "cache",
    )

    build_root = _published_build_root(executable)
    assert (build_root / "include/include/sdk.hpp").is_file()
    assert subprocess.run((str(executable),), check=False).returncode == 0


def test_build_cpp_player_cache_identity_covers_source_and_sdk(
    tmp_path: Path,
) -> None:
    game_dir = tmp_path / "game"
    _write_game_catalog(
        game_dir,
        {"sdk/value.hpp": b"#pragma once\n#define VALUE 1\n"},
    )
    player = tmp_path / "player"
    player.mkdir()
    main = player / "main.cpp"
    main.write_text(
        "#include \"sdk/value.hpp\"\nint main() { return VALUE > 0 ? 0 : 1; }\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    first = build_cpp_player(player_dir=player, game_dir=game_dir, cache_root=cache)

    main.write_text(
        "#include \"sdk/value.hpp\"\nint main() { return VALUE == 1 ? 0 : 1; }\n",
        encoding="utf-8",
    )
    source_changed = build_cpp_player(
        player_dir=player,
        game_dir=game_dir,
        cache_root=cache,
    )

    _write_game_catalog(
        game_dir,
        {"sdk/value.hpp": b"#pragma once\n#define VALUE 2\n"},
        bundle_id="test-sdk-v2",
        fingerprint="b" * 64,
    )
    sdk_changed = build_cpp_player(
        player_dir=player,
        game_dir=game_dir,
        cache_root=cache,
    )

    assert _published_build_root(first) != _published_build_root(source_changed)
    assert _published_build_root(source_changed) != _published_build_root(sdk_changed)


def test_build_cpp_player_stages_runtime_files_without_overwriting_player_bytes(
    tmp_path: Path,
) -> None:
    game_dir = tmp_path / "game"
    _write_game_catalog(game_dir, {"sdk/unused.hpp": b"#pragma once\n"})
    runtime_source = game_dir / "backend/mapconf2.map"
    runtime_source.parent.mkdir(parents=True)
    runtime_source.write_bytes(b"official map\n")
    player = tmp_path / "player"
    player.mkdir()
    (player / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (player / "mapconf2.map").write_bytes(b"player map\n")

    executable = build_cpp_player(
        player_dir=player,
        game_dir=game_dir,
        cache_root=tmp_path / "cache",
        runtime_files={"mapconf2.map": runtime_source},
    )

    build_root = _published_build_root(executable)
    assert (build_root / "mapconf2.map").read_bytes() == b"player map\n"
    metadata = json.loads((build_root / NATIVE_BUILD_METADATA).read_text())
    assert metadata["runtime_files"][0]["copied"] is False


def test_build_cpp_player_never_recovers_strategy_owned_main_cpp(
    tmp_path: Path,
) -> None:
    game_dir = tmp_path / "game"
    _write_game_catalog(
        game_dir,
        {"main.cpp": b"int main() { return 0; }\n"},
    )
    player = tmp_path / "player"
    player.mkdir()
    (player / "Makefile").write_text(
        "all: main.cpp\n\tg++ main.cpp -o main\n",
        encoding="utf-8",
    )

    with pytest.raises(CppBuildError, match="sdk_resolution_error"):
        build_cpp_player(
            player_dir=player,
            game_dir=game_dir,
            cache_root=tmp_path / "cache",
        )

    assert not (player / "main.cpp").exists()


def test_build_cpp_player_stops_at_the_sdk_retry_limit(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    headers = {
        f"sdk/h{index}.hpp": (
            f"#pragma once\n#include \"sdk/h{index + 1}.hpp\"\n".encode()
            if index < 16
            else b"#pragma once\n"
        )
        for index in range(17)
    }
    _write_game_catalog(game_dir, headers)
    player = tmp_path / "player"
    player.mkdir()
    (player / "main.cpp").write_text(
        "#include \"sdk/h0.hpp\"\nint main() { return 0; }\n",
        encoding="utf-8",
    )

    with pytest.raises(CppBuildError, match="SDK recovery retry limit"):
        build_cpp_player(
            player_dir=player,
            game_dir=game_dir,
            cache_root=tmp_path / "cache",
        )

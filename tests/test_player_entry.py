from __future__ import annotations

from pathlib import Path

from aa_arena.core.player_entry import (
    find_build_dir,
    find_native_entry,
    has_native_source,
)


def test_source_only_package_is_a_native_build_candidate(tmp_path: Path) -> None:
    package = tmp_path / "package"
    source = package / "submission/main.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("int main() { return 0; }\n", encoding="utf-8")

    assert has_native_source(package) is True
    assert find_build_dir(package) is None
    assert find_native_entry(package) == package


def test_native_entry_prefers_an_explicit_nested_build_project(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    project = package / "submission"
    project.mkdir(parents=True)
    (project / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (project / "Makefile").write_text(
        "all:\n\tg++ main.cpp -o player\n",
        encoding="utf-8",
    )

    assert find_native_entry(package) == project


def test_ignored_metadata_source_is_not_a_native_candidate(tmp_path: Path) -> None:
    package = tmp_path / "package"
    metadata = package / ".git/generated.cpp"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("int main() { return 0; }\n", encoding="utf-8")

    assert has_native_source(package) is False
    assert find_native_entry(package) is None

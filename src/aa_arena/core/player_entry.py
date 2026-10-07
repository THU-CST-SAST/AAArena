"""Deterministic discovery of runnable entry points inside submitted packages.

Saiblo submissions are archives, and many archives contain one or more wrapper
directories.  Evaluators must therefore resolve an entry relative to the package
tree instead of assuming every archive was flattened before ingestion.
"""

from __future__ import annotations

from pathlib import Path

_IGNORED_PARTS = frozenset({"__pycache__", ".git", ".svn", ".hg"})
_MAKEFILES = frozenset({"Makefile", "makefile", "GNUmakefile"})


def _usable(path: Path, package_root: Path) -> bool:
    try:
        relative = path.relative_to(package_root)
    except ValueError:
        return False
    return not (_IGNORED_PARTS & set(relative.parts))


def _preferred(paths: list[Path], package_root: Path) -> Path | None:
    if not paths:
        return None
    return min(
        paths,
        key=lambda path: (len(path.relative_to(package_root).parts), path.as_posix()),
    )


def _describes_native_build(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lowered = text.lower()
    if path.name == "CMakeLists.txt":
        return "add_executable" in lowered
    if "sphinx-build" in lowered:
        return False
    return any(
        marker in text
        for marker in ("g++", "clang++", "CXX", "CXXFLAGS", ".cpp", ".cc", ".cxx")
    )


def find_python_entry(package_root: str | Path) -> Path | None:
    """Return the directory containing the preferred recursive ``main.py``."""

    root = Path(package_root)
    if not root.is_dir():
        return None
    direct = root / "main.py"
    if direct.is_file():
        return root
    candidates = [
        path.parent
        for path in root.rglob("main.py")
        if path.is_file() and _usable(path, root)
    ]
    return _preferred(candidates, root)


def find_build_dir(
    package_root: str | Path,
    *,
    allow_make: bool = True,
    allow_cmake: bool = True,
) -> Path | None:
    """Return the preferred recursive Make/CMake project directory."""

    root = Path(package_root)
    if not root.is_dir():
        return None
    names: set[str] = set()
    if allow_make:
        names.update(_MAKEFILES)
    if allow_cmake:
        names.add("CMakeLists.txt")
    direct = [
        root / name
        for name in names
        if (root / name).is_file() and _describes_native_build(root / name)
    ]
    if direct:
        return root
    candidates = [
        path.parent
        for path in root.rglob("*")
        if path.is_file()
        and path.name in names
        and _usable(path, root)
        and _describes_native_build(path)
    ]
    return _preferred(candidates, root)


def has_native_source(package_root: str | Path) -> bool:
    """Whether a package contains C/C++ source, without claiming it is buildable."""

    root = Path(package_root)
    if not root.is_dir():
        return False
    return any(
        path.is_file() and path.suffix.lower() in {".c", ".cc", ".cpp", ".cxx"}
        and _usable(path, root)
        for path in root.rglob("*")
    )


def find_native_entry(package_root: str | Path) -> Path | None:
    """Return a build project, or the package root for source-only compilation."""

    root = Path(package_root)
    build_dir = find_build_dir(root)
    if build_dir is not None:
        return build_dir
    return root if has_native_source(root) else None

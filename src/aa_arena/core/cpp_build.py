"""Game-independent, concurrent-safe helpers for Makefile-based C++ packages."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from pathlib import PurePosixPath
from typing import Mapping

from aa_arena.core.buildcache import published_build_dir
from aa_arena.core.build_sandbox import (
    BUILD_SANDBOX_POLICY_VERSION,
    BuildSandboxError,
    run_isolated_build,
    validate_build_tree,
)
from aa_arena.core.native_sdk import (
    AppliedOverlay,
    AppliedRuntimeFile,
    NativeSdkCatalog,
    NativeSdkCatalogError,
    SdkResolution,
    apply_runtime_overlay,
    apply_sdk_overlay,
)


class CppBuildError(RuntimeError):
    """A C++ source package could not be validated or built."""


class CppBuildSandboxError(CppBuildError):
    """Stop recovery immediately when isolation or tree validation fails."""


EXECUTABLE_NAME = "main.exe" if sys.platform == "win32" else "main"
_MAKEFILE_NAMES = ("Makefile", "makefile", "GNUmakefile")
_COMPAT_SHIM = Path(__file__).resolve().parent / "cpp_compat_shim.h"
_BUILD_EXECUTABLE_MANIFEST = ".ahl-native-executable.json"
NATIVE_BUILD_METADATA = ".ahl-native-build.json"
_NATIVE_BUILD_POLICY_VERSION = 5
_MAX_SDK_OVERLAY_RETRIES = 16
_CMAKE_BUILD_DIR = ".ahl-cmake-build"
_SOURCE_BUILD_DIR = ".ahl-source-build"
SOURCE_BUILD_POLICY_VERSION = 2
CPP_STANDARDS = ("c++17", "c++14", "gnu++17")
_C_STANDARD = "gnu11"
_NATIVE_SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx"}
_HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx"}
_SOURCE_IGNORED_PARTS = {
    ".git",
    ".hg",
    ".svn",
    _CMAKE_BUILD_DIR,
    _SOURCE_BUILD_DIR,
    "CMakeFiles",
    "__pycache__",
    "public_sdk_cpp",
    "public_sdk_cpp_compat",
}
_STRATEGY_OWNED_SDK_NAMES = {
    "Action.cpp",
    "Snakego.h",
    "UCT.h",
    "ai-sample.cpp",
    "astar.h",
    "conio.h",
    "copy.cpp",
    "main.cpp",
}
_NON_EXECUTABLE_SUFFIXES = {
    ".a",
    ".cmake",
    ".d",
    ".dll",
    ".dylib",
    ".lib",
    ".o",
    ".obj",
    ".so",
}


@dataclass(frozen=True)
class ExecutableSnapshot:
    """Executable file metadata captured before a build starts."""

    files: tuple[tuple[str, int, int, int], ...]

    @classmethod
    def capture(cls, root: str | Path) -> ExecutableSnapshot:
        build_root = Path(root)
        if not build_root.exists():
            return cls(())
        states: list[tuple[str, int, int, int]] = []
        for candidate in _executable_candidates(build_root):
            stat = candidate.stat()
            states.append(
                (
                    candidate.relative_to(build_root).as_posix(),
                    stat.st_mode,
                    stat.st_size,
                    stat.st_mtime_ns,
                )
            )
        return cls(tuple(states))

    def as_dict(self) -> dict[str, tuple[int, int, int]]:
        return {
            relative: (mode, size, mtime_ns)
            for relative, mode, size, mtime_ns in self.files
        }


@dataclass(frozen=True)
class SourceBuildAttempt:
    phase: str
    standard: str
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(root: Path) -> str:
    """Hash source content while excluding Python caches and built executables."""

    try:
        validate_build_tree(root)
    except BuildSandboxError as exc:
        raise CppBuildSandboxError(f"build_sandbox_error: {exc}") from exc
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CppBuildError(f"runtime tree contains a symlink: {path}")
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.name in {"main", "main.exe"} and os.access(path, os.X_OK):
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


_COMPILER_MISSING = re.compile(
    r"fatal error:\s*([^:\n]+):\s*No such file or directory",
    re.IGNORECASE,
)
_COMPILER_MISSING_WITH_INCLUDER = re.compile(
    r"^(.+?):\d+(?::\d+)?:\s*fatal error:\s*([^:\n]+):"
    r"\s*No such file or directory",
    re.IGNORECASE | re.MULTILINE,
)
_BUILD_CWD = re.compile(r"\[(?:make|fallback) cwd=(.+?)\]")
_MAKE_MISSING = re.compile(
    r"No rule to make target\s+['`]([^'`]+)['`]",
    re.IGNORECASE,
)
_CMAKE_MISSING = re.compile(
    r"Cannot find source file:\s*(?:\n\s*)?([^\n]+)",
    re.IGNORECASE,
)


def parse_missing_build_path(diagnostic: str) -> str | None:
    """Extract one exact missing path from supported native build diagnostics."""

    for pattern in (_COMPILER_MISSING, _MAKE_MISSING, _CMAKE_MISSING):
        match = pattern.search(diagnostic)
        if match is not None:
            candidate = match.group(1).strip()
            return candidate or None
    return None


def _safe_staging_relative(path: Path, staging: Path) -> str | None:
    """Return one normalized POSIX path only when it stays inside staging."""

    staging_root = staging.resolve()
    try:
        relative = path.resolve(strict=False).relative_to(staging_root)
    except ValueError:
        return None
    if not relative.parts or "." in relative.parts or ".." in relative.parts:
        return None
    return PurePosixPath(*relative.parts).as_posix()


def _contextual_missing_destination(
    diagnostic: str,
    staging: Path,
    requested_path: str,
) -> str | None:
    """Resolve a compiler include relative to the file that requested it."""

    for match in _COMPILER_MISSING_WITH_INCLUDER.finditer(diagnostic):
        if match.group(2).strip() != requested_path:
            continue
        includer = Path(match.group(1).strip())
        if includer.is_absolute():
            includers = (includer,)
        else:
            build_cwds = tuple(
                Path(cwd.group(1).strip()) for cwd in _BUILD_CWD.finditer(diagnostic)
            )
            includers = tuple(cwd / includer for cwd in build_cwds) + (
                staging / includer,
            )
        requested = Path(*PurePosixPath(requested_path).parts)
        for candidate in includers:
            if not candidate.is_file():
                continue
            destination = _safe_staging_relative(
                candidate.parent / requested, staging
            )
            if destination is not None:
                return destination
    return None


def _catalog_resolution_for_source(
    catalog: NativeSdkCatalog,
    requested_path: str,
) -> SdkResolution | None:
    """Resolve an exact SDK path, or one unambiguous pinned file by basename."""

    exact = catalog.resolve_missing(requested_path)
    if exact is not None:
        return exact
    basename = PurePosixPath(requested_path).name
    matches: dict[tuple[Path, str], SdkResolution] = {}
    for bundle in catalog.bundles:
        for item in bundle.files:
            if PurePosixPath(item.requested_path).name != basename:
                continue
            resolution = SdkResolution(
                bundle_id=bundle.bundle_id,
                bundle_fingerprint=bundle.fingerprint_sha256,
                requested_path=item.requested_path,
                source_path=item.source_path,
                sha256=item.sha256,
            )
            matches.setdefault((item.source_path, item.sha256), resolution)
    return next(iter(matches.values())) if len(matches) == 1 else None


def _resolve_sdk_overlay(
    diagnostic: str,
    staging: Path,
    catalog: NativeSdkCatalog,
    requested_path: str,
) -> SdkResolution | None:
    destination = _contextual_missing_destination(
        diagnostic, staging, requested_path
    )
    if destination is None:
        return catalog.resolve_missing(requested_path)
    source = catalog.resolve_missing(destination)
    if source is None:
        source = _catalog_resolution_for_source(catalog, requested_path)
    if source is None:
        return None
    return replace(source, requested_path=destination)


def _transitive_sdk_overlays(
    staging: Path,
    catalog: NativeSdkCatalog,
    root_resolution: SdkResolution,
    applied_destinations: set[str],
) -> list[AppliedOverlay]:
    """Stage quoted SDK includes beside their includer proactively.

    A system image can contain a header with the same basename as a game SDK
    header (jsoncpp is a common example).  Waiting for a compiler "missing
    header" diagnostic then silently selects the host header and produces
    plausible-but-wrong semantics.  Walking the small SDK include graph keeps
    builds deterministic while still copying only catalog-pinned files.
    """
    # Copy one include layer per compiler retry.  This preserves the global
    # retry bound for pathological include chains while avoiding host-header
    # collisions in the common two-level SDK layout.
    pending = [root_resolution]
    overlays: list[AppliedOverlay] = []
    seen: set[str] = set()
    include_pattern = re.compile(r"^\s*#\s*include\s*\"([^\"]+)\"", re.MULTILINE)
    while pending:
        resolution = pending.pop()
        if resolution.requested_path in seen:
            continue
        seen.add(resolution.requested_path)
        destination = staging / Path(*PurePosixPath(resolution.requested_path).parts)
        try:
            text = destination.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for include in include_pattern.findall(text):
            include_path = PurePosixPath(resolution.requested_path).parent / include
            normalized = PurePosixPath(include_path).as_posix()
            catalog_path = normalized
            child = catalog.resolve_missing(catalog_path)
            if child is None:
                catalog_path = PurePosixPath(include).as_posix()
                child = catalog.resolve_missing(catalog_path)
            if child is None:
                continue
            if normalized in applied_destinations:
                continue
            child = replace(child, requested_path=normalized)
            applied_destinations.add(normalized)
            applied = apply_sdk_overlay(staging, child)
            if applied.copied:
                overlays.append(applied)
                # Deliberately do not recurse here; the next compile attempt
                # will expose another missing/colliding include if needed.
    return overlays


@lru_cache(maxsize=1)
def _compiler_identity_items() -> tuple[tuple[str, str], ...]:
    identities: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="ahl-toolchain-") as temporary:
        for executable in ("gcc", "g++", "make", "cmake"):
            completed = _run_build_command(
                (executable, "--version"), cwd=Path(temporary), timeout=60,
            )
            output = completed.stdout or completed.stderr
            first_line = output.splitlines()[0] if output else "unavailable"
            identities.append((executable, first_line))
    return tuple(identities)


def _run_build_command(
    command: tuple[str, ...],
    *,
    cwd: Path,
    timeout: float,
    build_root: Path | None = None,
    readonly_paths: tuple[Path, ...] = (),
) -> subprocess.CompletedProcess[str]:
    try:
        return run_isolated_build(
            command, cwd=cwd, build_root=build_root,
            readonly_paths=readonly_paths, timeout=timeout,
        )
    except BuildSandboxError as exc:
        raise CppBuildSandboxError(f"build_sandbox_error: {exc}") from exc


def _compiler_identity() -> dict[str, str]:
    return dict(_compiler_identity_items())


def _sdk_overlay_metadata(applied: AppliedOverlay) -> dict[str, object]:
    return {
        "bundle_id": applied.bundle_id,
        "requested_path": applied.requested_path,
        "source_path": str(applied.source_path),
        "destination_path": applied.requested_path,
        "sha256": applied.sha256,
        "copied": applied.copied,
    }


def _runtime_overlay_metadata(applied: AppliedRuntimeFile) -> dict[str, object]:
    return {
        "requested_path": applied.requested_path,
        "source_path": str(applied.source_path),
        "destination_path": applied.requested_path,
        "sha256": applied.sha256,
        "copied": applied.copied,
    }


def _is_build_executable(path: Path, root: Path) -> bool:
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        return False
    relative = path.relative_to(root)
    if "CMakeFiles" in relative.parts:
        return False
    if path.name in {_BUILD_EXECUTABLE_MANIFEST, ".aa_arena-complete"}:
        return False
    return path.suffix.lower() not in _NON_EXECUTABLE_SUFFIXES


def _executable_candidates(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    return tuple(
        candidate
        for candidate in sorted(root.rglob("*"))
        if _is_build_executable(candidate, root)
    )


def _one_candidate(candidates: list[Path], *, stage: str) -> Path | None:
    unique = sorted(set(candidates))
    if len(unique) > 1:
        rendered = ", ".join(str(path) for path in unique)
        raise CppBuildError(f"ambiguous {stage} executable outputs: {rendered}")
    return unique[0] if unique else None


def discover_built_executable(
    root: str | Path,
    before: ExecutableSnapshot,
    declared_candidates: tuple[Path, ...] | list[Path],
) -> Path | None:
    """Find one build output without guessing between equally valid files."""

    build_root = Path(root).resolve()
    declared: list[Path] = []
    for raw_candidate in declared_candidates:
        candidate = (
            raw_candidate
            if raw_candidate.is_absolute()
            else build_root / raw_candidate
        ).resolve(strict=False)
        try:
            candidate.relative_to(build_root)
        except ValueError as exc:
            raise CppBuildError(
                f"declared executable is outside build root: {raw_candidate}"
            ) from exc
        if _is_build_executable(candidate, build_root):
            declared.append(candidate)
    exact = _one_candidate(declared, stage="declared")
    if exact is not None:
        return exact

    previous = before.as_dict()
    current = _executable_candidates(build_root)
    new = [
        candidate
        for candidate in current
        if candidate.relative_to(build_root).as_posix() not in previous
    ]
    created = _one_candidate(new, stage="new")
    if created is not None:
        return created

    changed: list[Path] = []
    for candidate in current:
        relative = candidate.relative_to(build_root).as_posix()
        old_state = previous.get(relative)
        if old_state is None:
            continue
        stat = candidate.stat()
        if (stat.st_mode, stat.st_size, stat.st_mtime_ns) != old_state:
            changed.append(candidate)
    return _one_candidate(changed, stage="changed")


def _looks_like_cpp_makefile(makefile: Path) -> bool:
    try:
        text = makefile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lowered = text.lower()
    if "sphinx" in lowered or "sphinx-build" in lowered:
        return False
    return any(
        marker in text
        for marker in ("g++", "clang++", "CXX", "CXXFLAGS", ".cpp", ".cc", ".cxx")
    )


def find_cpp_makefile_dir(root: Path) -> Path:
    """Locate the shallowest Makefile that actually builds C++ code."""

    for name in _MAKEFILE_NAMES:
        direct = root / name
        if direct.is_file() and _looks_like_cpp_makefile(direct):
            return root
    candidates = [
        path
        for name in _MAKEFILE_NAMES
        for path in sorted(root.rglob(name))
        if _looks_like_cpp_makefile(path)
    ]
    if not candidates:
        raise CppBuildError(f"no C++ Makefile found under {root}")
    candidates.sort(key=lambda item: (len(item.relative_to(root).parts), item.as_posix()))
    return candidates[0].parent


def _makefile_in(directory: Path) -> Path | None:
    for name in _MAKEFILE_NAMES:
        path = directory / name
        if path.is_file() and _looks_like_cpp_makefile(path):
            return path
    return None


def _find_cmake_dir(root: Path) -> Path | None:
    direct = root / "CMakeLists.txt"
    if direct.is_file():
        return root
    candidates = sorted(
        (path for path in root.rglob("CMakeLists.txt") if path.is_file()),
        key=lambda path: (len(path.relative_to(root).parts), path.as_posix()),
    )
    return candidates[0].parent if candidates else None


def _native_sources(root: Path) -> tuple[Path, ...]:
    sources: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _NATIVE_SOURCE_SUFFIXES:
            continue
        relative = path.relative_to(root)
        if _SOURCE_IGNORED_PARTS & set(relative.parts):
            continue
        sources.append(path)
    return tuple(sorted(sources))


_COMMENT_OR_LITERAL = re.compile(
    r"//[^\n]*|/\*.*?\*/|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'",
    re.DOTALL,
)
_MAIN_FUNCTION = re.compile(r"\bmain\s*\(")
_IMPLEMENTATION_INCLUDE = re.compile(
    r"^\s*#\s*include\s*\"([^\"\n]+\.(?:c|cc|cpp|cxx))\"",
    re.MULTILINE | re.IGNORECASE,
)


def _without_comments_and_literals(text: str) -> str:
    return _COMMENT_OR_LITERAL.sub(
        lambda match: "\n" * match.group(0).count("\n"),
        text,
    )


def _contains_main_function(source: Path) -> bool:
    text = source.read_text(encoding="utf-8", errors="replace")
    return _MAIN_FUNCTION.search(_without_comments_and_literals(text)) is not None


def _translation_units(root: Path) -> tuple[Path, ...]:
    sources = _native_sources(root)
    source_set = {path.resolve() for path in sources}
    textually_included: set[Path] = set()
    for source in sources:
        text = source.read_text(encoding="utf-8", errors="replace")
        for relative in _IMPLEMENTATION_INCLUDE.findall(text):
            included = (source.parent / relative).resolve()
            if included in source_set:
                textually_included.add(included)
    return tuple(source for source in sources if source.resolve() not in textually_included)


def _header_directories(root: Path) -> tuple[Path, ...]:
    directories = {root}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _HEADER_SUFFIXES:
            continue
        relative = path.relative_to(root)
        if _SOURCE_IGNORED_PARTS & set(relative.parts):
            continue
        directories.add(path.parent)
    return tuple(
        sorted(
            directories,
            key=lambda path: (
                len(path.relative_to(root).parts),
                path.relative_to(root).as_posix(),
            ),
        )
    )


def _parse_makefile_compiler_args(makefile: Path) -> list[str] | None:
    text = makefile.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^\s*(g\+\+|clang\+\+)\s+(.*)$", text, re.MULTILINE)
    return shlex.split(match.group(2)) if match is not None else None


def _output_from_compiler_args(args: list[str], *, cwd: Path) -> tuple[Path, ...]:
    outputs: list[Path] = []
    for index, token in enumerate(args):
        if token == "-o" and index + 1 < len(args):
            outputs.append(cwd / args[index + 1])
        elif token.startswith("-o") and len(token) > 2:
            outputs.append(cwd / token[2:])
    return tuple(outputs)


def _make_declared_candidates(make_dir: Path) -> tuple[Path, ...]:
    makefile = _makefile_in(make_dir)
    if makefile is None:
        return ()
    args = _parse_makefile_compiler_args(makefile)
    return _output_from_compiler_args(args, cwd=make_dir) if args is not None else ()


def _cmake_declared_candidates(cmake_dir: Path, build_dir: Path) -> tuple[Path, ...]:
    text = (cmake_dir / "CMakeLists.txt").read_text(
        encoding="utf-8", errors="replace"
    )
    names = re.findall(
        r"\badd_executable\s*\(\s*([A-Za-z0-9_.+-]+)",
        text,
        re.IGNORECASE,
    )
    return tuple(build_dir / name for name in names)


def _strip_std_and_output(args: list[str]) -> list[str]:
    cleaned: list[str] = []
    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token == "-o":
            skip_next = True
            continue
        if token.startswith("-std="):
            continue
        cleaned.append(token)
    return cleaned


def _compile_with_makefile(
    build_dir: Path, timeout: float
) -> tuple[subprocess.CompletedProcess[str], Path]:
    make_dir = find_cpp_makefile_dir(build_dir)
    completed = _run_build_command(
        ("make", "-B", "-C", str(make_dir), f"-j{max(1, min(os.cpu_count() or 1, 8))}"),
        cwd=make_dir,
        build_root=build_dir,
        timeout=timeout,
    )
    return completed, make_dir


def _compile_with_cmake(
    source_dir: Path,
    build_dir: Path,
    timeout: float,
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str] | None]:
    cmake = "/usr/bin/cmake"
    if not Path(cmake).is_file():
        raise CppBuildError("cmake executable is unavailable")
    try:
        configure = _run_build_command(
            (cmake, "-S", str(source_dir), "-B", str(build_dir)),
            cwd=source_dir,
            build_root=build_dir.parent,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CppBuildError("cmake executable is unavailable") from exc
    if configure.returncode != 0:
        return configure, None
    build = _run_build_command(
        (
            cmake,
            "--build",
            str(build_dir),
            "--parallel",
            str(max(1, min(os.cpu_count() or 1, 8))),
        ),
        cwd=source_dir,
        build_root=build_dir.parent,
        timeout=timeout,
    )
    return configure, build


def _run_source_command(
    command: tuple[str, ...],
    *,
    cwd: Path,
    phase: str,
    standard: str,
    timeout: float,
) -> SourceBuildAttempt:
    try:
        completed = _run_build_command(
            command,
            cwd=cwd,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CppBuildError(f"native compiler is unavailable: {command[0]}") from exc
    return SourceBuildAttempt(
        phase=phase,
        standard=standard,
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout[-4000:],
        stderr=completed.stderr[-4000:],
    )


def _compile_source_set(
    source_root: Path,
    sources: tuple[Path, ...],
    output_dir: Path,
    *,
    timeout: float,
) -> tuple[Path | None, tuple[SourceBuildAttempt, ...]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    has_cpp = any(source.suffix.lower() != ".c" for source in sources)
    standards = CPP_STANDARDS if has_cpp else (_C_STANDARD,)
    include_args = tuple(
        argument
        for directory in _header_directories(source_root)
        for argument in ("-I", str(directory))
    )
    attempts: list[SourceBuildAttempt] = []
    for standard_index, standard in enumerate(standards):
        standard_dir = output_dir / f"{standard_index:02d}-{standard}"
        standard_dir.mkdir(parents=True, exist_ok=True)
        objects: list[Path] = []
        failed = False
        for source in sources:
            relative = source.relative_to(source_root).as_posix()
            object_name = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16] + ".o"
            object_path = standard_dir / object_name
            is_c = source.suffix.lower() == ".c"
            compiler = "gcc" if is_c else "g++"
            language_standard = _C_STANDARD if is_c else standard
            command = (
                compiler,
                f"-std={language_standard}",
                "-O2",
                "-pthread",
                *include_args,
                "-c",
                str(source),
                "-o",
                str(object_path),
            )
            attempt = _run_source_command(
                command,
                cwd=source_root,
                phase=f"compile:{relative}",
                standard=standard,
                timeout=timeout,
            )
            attempts.append(attempt)
            if attempt.returncode != 0:
                failed = True
                break
            objects.append(object_path)
        if failed:
            continue

        executable = standard_dir / EXECUTABLE_NAME
        linker = "g++" if has_cpp else "gcc"
        link_command = (
            linker,
            "-pthread",
            *(str(object_path) for object_path in objects),
            "-o",
            str(executable),
        )
        attempt = _run_source_command(
            link_command,
            cwd=source_root,
            phase="link",
            standard=standard,
            timeout=timeout,
        )
        attempts.append(attempt)
        if (
            attempt.returncode == 0
            and executable.is_file()
            and os.access(executable, os.X_OK)
        ):
            return executable, tuple(attempts)
    return None, tuple(attempts)


def _source_attempt_diagnostic(attempts: list[SourceBuildAttempt]) -> str:
    sections: list[str] = []
    for attempt in attempts:
        output = attempt.stderr or attempt.stdout
        sections.append(
            f"[standard={attempt.standard} phase={attempt.phase} "
            f"returncode={attempt.returncode}]\n"
            f"command={attempt.command!r}\n{output}"
        )
    return "\n".join(sections)


def _compile_source_only(source_root: Path, work: Path, *, timeout: float) -> Path:
    sources = _translation_units(work)
    if not sources:
        raise CppBuildError(f"no native source files found under {source_root}")
    main_sources = tuple(source for source in sources if _contains_main_function(source))
    if not main_sources:
        raise CppBuildError(f"no_entry_point: no main function found under {source_root}")

    all_attempts: list[SourceBuildAttempt] = []
    executable, attempts = _compile_source_set(
        work,
        sources,
        work / _SOURCE_BUILD_DIR / "all",
        timeout=timeout,
    )
    all_attempts.extend(attempts)
    if executable is not None:
        return executable
    if len(main_sources) == 1:
        raise CppBuildError(
            f"source-only build failed for {source_root.name}:\n"
            f"{_source_attempt_diagnostic(all_attempts)}"
        )

    helpers = tuple(source for source in sources if source not in main_sources)
    successes: list[Path] = []
    for index, main_source in enumerate(main_sources):
        executable, attempts = _compile_source_set(
            work,
            (main_source, *helpers),
            work / _SOURCE_BUILD_DIR / f"candidate-{index:03d}",
            timeout=timeout,
        )
        all_attempts.extend(attempts)
        if executable is not None:
            successes.append(executable)
    if len(successes) > 1:
        rendered = ", ".join(
            source.relative_to(work).as_posix() for source in main_sources
        )
        raise CppBuildError(f"ambiguous source-only main candidates: {rendered}")
    if successes:
        return successes[0]
    raise CppBuildError(
        f"source-only build failed for {source_root.name}:\n"
        f"{_source_attempt_diagnostic(all_attempts)}"
    )


def _compile_fallback(
    make_dir: Path, timeout: float, *, build_root: Path
) -> subprocess.CompletedProcess[str] | None:
    makefile = _makefile_in(make_dir)
    if makefile is None:
        return None
    args = _parse_makefile_compiler_args(makefile)
    if args is None:
        return None
    base = _strip_std_and_output(args)
    attempts: list[subprocess.CompletedProcess[str]] = []
    for standard in ("-std=c++17", "-std=c++14", "-std=gnu++17"):
        completed = _run_build_command(
            ("g++", standard, *base, "-o", EXECUTABLE_NAME),
            cwd=make_dir,
            build_root=build_root,
            timeout=timeout,
        )
        attempts.append(completed)
        if completed.returncode == 0 and (make_dir / EXECUTABLE_NAME).is_file():
            return completed

    missing_random_shuffle = any(
        re.search(
            r"(?:random_shuffle.{0,120}(?:not (?:a member|declared)|no member|undeclared)"
            r"|(?:no member(?: named)?|not (?:a member|declared)|undeclared)"
            r".{0,120}random_shuffle)",
            attempt.stderr or attempt.stdout,
            re.IGNORECASE,
        )
        is not None
        for attempt in attempts
    )
    if missing_random_shuffle and _COMPAT_SHIM.is_file():
        for standard in ("-std=c++17", "-std=gnu++17"):
            completed = _run_build_command(
                (
                    "g++",
                    standard,
                    "-include",
                    str(_COMPAT_SHIM),
                    *base,
                    "-o",
                    EXECUTABLE_NAME,
                ),
                cwd=make_dir,
                build_root=build_root,
                readonly_paths=(_COMPAT_SHIM,),
                timeout=timeout,
            )
            attempts.append(completed)
            if completed.returncode == 0 and (make_dir / EXECUTABLE_NAME).is_file():
                return completed

    last = attempts[-1]
    stdout = "\n".join(
        f"[{attempt.args!r}]\n{attempt.stdout}" for attempt in attempts if attempt.stdout
    )
    stderr = "\n".join(
        f"[{attempt.args!r}]\n{attempt.stderr}" for attempt in attempts if attempt.stderr
    )
    return subprocess.CompletedProcess(last.args, last.returncode, stdout, stderr)


def _read_built_executable_manifest(build_dir: Path) -> Path | None:
    path = build_dir / _BUILD_EXECUTABLE_MANIFEST
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        relative = Path(payload["executable"])
        if relative.is_absolute() or ".." in relative.parts:
            return None
        executable = (build_dir / relative).resolve()
        executable.relative_to(build_dir.resolve())
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return executable if _is_build_executable(executable, build_dir.resolve()) else None


def _write_built_executable_manifest(build_dir: Path, executable: Path) -> None:
    relative = executable.relative_to(build_dir).as_posix()
    (build_dir / _BUILD_EXECUTABLE_MANIFEST).write_text(
        json.dumps({"schema_version": "1.0", "executable": relative}, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _built_executable(build_dir: Path) -> Path | None:
    manifested = _read_built_executable_manifest(build_dir)
    if manifested is not None:
        return manifested
    return discover_built_executable(build_dir, ExecutableSnapshot(()), ())


def native_build_root(executable: str | Path) -> Path:
    """Return the published staging root that owns a native executable."""

    path = Path(executable).resolve()
    for candidate in (path.parent, *path.parents):
        if (candidate / NATIVE_BUILD_METADATA).is_file():
            return candidate
    raise CppBuildError(f"native executable has no build metadata: {path}")


def _compile_staged(source_root: Path, work: Path, *, timeout: float) -> Path:
    try:
        make_dir = find_cpp_makefile_dir(work)
    except CppBuildError:
        make_dir = None
    if make_dir is not None:
        before = ExecutableSnapshot.capture(work)
        completed, make_dir = _compile_with_makefile(work, timeout)
        if completed.returncode == 0:
            executable = discover_built_executable(
                work,
                before,
                list(_make_declared_candidates(make_dir)),
            )
            if executable is not None:
                return executable
        fallback = _compile_fallback(make_dir, timeout, build_root=work)
        if fallback is not None and fallback.returncode == 0:
            executable = discover_built_executable(
                work,
                before,
                [make_dir / EXECUTABLE_NAME],
            )
            if executable is not None:
                return executable
        diagnostic = (completed.stdout + "\n" + completed.stderr)[-8000:]
        fallback_diagnostic = ""
        if fallback is not None:
            fallback_diagnostic = (fallback.stdout + "\n" + fallback.stderr)[-8000:]
        recipe_error = (
            f"C++ build failed for {source_root.name}:\n"
            f"[make cwd={make_dir}]\n{diagnostic}\n"
            f"[fallback cwd={make_dir}]\n{fallback_diagnostic}"
        )
        requested_path = parse_missing_build_path(recipe_error)
        requested_name = (
            PurePosixPath(requested_path.replace("\\", "/"))
            if requested_path is not None
            else None
        )
        if (
            requested_name is not None
            and requested_name.name not in _STRATEGY_OWNED_SDK_NAMES
            and requested_name.suffix.lower() not in _NATIVE_SOURCE_SUFFIXES
        ):
            # Preserve recipe include semantics long enough for the outer,
            # catalog-aware retry loop to apply a missing-only SDK overlay.
            # The generic source compiler deliberately has broader -I paths
            # and must not silently bypass a recoverable recipe diagnostic.
            raise CppBuildError(recipe_error)
        try:
            return _compile_source_only(source_root, work, timeout=timeout)
        except CppBuildError as source_error:
            raise CppBuildError(
                f"C++ build failed for {source_root.name}:\n"
                f"[source-only fallback]\n{source_error}\n"
                f"{recipe_error}"
            ) from source_error

    cmake_dir = _find_cmake_dir(work)
    if cmake_dir is None:
        return _compile_source_only(source_root, work, timeout=timeout)
    before = ExecutableSnapshot.capture(work)
    cmake_build_dir = work / _CMAKE_BUILD_DIR
    cmake_error = ""
    try:
        configure, build = _compile_with_cmake(cmake_dir, cmake_build_dir, timeout)
    except CppBuildSandboxError:
        raise
    except CppBuildError as exc:
        configure = None
        build = None
        cmake_error = str(exc)
    if (
        configure is not None
        and configure.returncode == 0
        and build is not None
        and build.returncode == 0
    ):
        executable = discover_built_executable(
            work,
            before,
            list(_cmake_declared_candidates(cmake_dir, cmake_build_dir)),
        )
        if executable is not None:
            return executable
    configure_diagnostic = (
        (configure.stderr or configure.stdout)[-4000:]
        if configure is not None
        else cmake_error
    )
    build_diagnostic = ""
    if build is not None:
        build_diagnostic = (build.stderr or build.stdout)[-4000:]
    recipe_error = (
        f"CMake build failed for {source_root.name}:\n"
        f"[configure]\n{configure_diagnostic}\n[build]\n{build_diagnostic}"
    )
    requested_path = parse_missing_build_path(recipe_error)
    requested_name = (
        PurePosixPath(requested_path.replace("\\", "/"))
        if requested_path is not None
        else None
    )
    if (
        requested_name is not None
        and requested_name.name not in _STRATEGY_OWNED_SDK_NAMES
        and requested_name.suffix.lower() not in _NATIVE_SOURCE_SUFFIXES
    ):
        raise CppBuildError(recipe_error)
    try:
        return _compile_source_only(source_root, work, timeout=timeout)
    except CppBuildError as source_error:
        raise CppBuildError(
            f"CMake build failed for {source_root.name}:\n"
            f"[source-only fallback]\n{source_error}\n"
            f"{recipe_error}"
        ) from source_error


def compile_cpp_package(
    source_root: Path,
    build_dir: Path,
    *,
    timeout: float = 300.0,
    sdk_catalog: NativeSdkCatalog | None = None,
    runtime_files: Mapping[str, Path] | None = None,
    build_metadata: Mapping[str, object] | None = None,
) -> Path:
    """Build a source package through an atomically published content cache."""

    source_root = Path(source_root).resolve()
    build_dir = Path(build_dir).resolve()
    try:
        validate_build_tree(source_root)
    except BuildSandboxError as exc:
        raise CppBuildError(f"build_sandbox_error: {exc}") from exc
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    with published_build_dir(build_dir, source=source_root, ignore=ignore) as (work, reused):
        if reused:
            executable = _built_executable(work)
            if executable is not None:
                return executable
            raise CppBuildError(f"published build has no executable: {work}")
        applied_runtime: list[AppliedRuntimeFile] = []
        for requested_path, runtime_source in sorted((runtime_files or {}).items()):
            try:
                applied_runtime.append(
                    apply_runtime_overlay(work, requested_path, runtime_source)
                )
            except NativeSdkCatalogError as exc:
                raise CppBuildError(f"runtime_overlay_error: {exc}") from exc

        applied_sdk: list[AppliedOverlay] = []
        applied_destinations: set[str] = set()
        while True:
            try:
                built = _compile_staged(source_root, work, timeout=timeout)
                break
            except CppBuildSandboxError:
                raise
            except CppBuildError as build_error:
                diagnostic = str(build_error)
                requested_path = parse_missing_build_path(diagnostic)
                if requested_path is None or sdk_catalog is None:
                    raise
                if len(applied_sdk) >= _MAX_SDK_OVERLAY_RETRIES:
                    raise CppBuildError(
                        "SDK recovery retry limit reached "
                        f"({_MAX_SDK_OVERLAY_RETRIES}) for {source_root.name}"
                    ) from build_error
                if PurePosixPath(requested_path).name in _STRATEGY_OWNED_SDK_NAMES:
                    raise CppBuildError(
                        "sdk_resolution_error: strategy-owned file is not recoverable: "
                        f"{requested_path}"
                    ) from build_error
                resolution = _resolve_sdk_overlay(
                    diagnostic, work, sdk_catalog, requested_path
                )
                if resolution is None:
                    raise CppBuildError(
                        "sdk_resolution_error: unknown, unsafe, or ambiguous path: "
                        f"{requested_path}"
                    ) from build_error
                if resolution.requested_path in applied_destinations:
                    raise CppBuildError(
                        f"sdk_resolution_error: no progress for {requested_path!r}"
                    ) from build_error
                applied_destinations.add(resolution.requested_path)
                try:
                    applied = apply_sdk_overlay(work, resolution)
                except NativeSdkCatalogError as exc:
                    raise CppBuildError(f"sdk_resolution_error: {exc}") from build_error
                if not applied.copied:
                    raise CppBuildError(
                        f"sdk_resolution_error: no progress for {requested_path!r}"
                    ) from build_error
                applied_sdk.append(applied)
                try:
                    applied_sdk.extend(
                        _transitive_sdk_overlays(
                            work, sdk_catalog, resolution, applied_destinations
                        )
                    )
                except NativeSdkCatalogError as exc:
                    raise CppBuildError(f"sdk_resolution_error: {exc}") from build_error

        metadata = dict(build_metadata or {})
        metadata.update(
            {
                "schema_version": "1.0",
                "native_build_policy_version": _NATIVE_BUILD_POLICY_VERSION,
                "build_sandbox_policy_version": BUILD_SANDBOX_POLICY_VERSION,
                "source_build_policy_version": SOURCE_BUILD_POLICY_VERSION,
                "executable": built.relative_to(work).as_posix(),
                "applied_sdk_files": [
                    _sdk_overlay_metadata(applied) for applied in applied_sdk
                ],
                "runtime_files": [
                    _runtime_overlay_metadata(applied) for applied in applied_runtime
                ],
            }
        )
        (work / NATIVE_BUILD_METADATA).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _write_built_executable_manifest(work, built)
        relative = built.relative_to(work)
    executable = build_dir / relative
    if not executable.is_file():
        raise CppBuildError(f"published build lost its executable: {executable}")
    return executable


def has_cpp_build(package_root: Path) -> bool:
    has_recipe = any(
        _looks_like_cpp_makefile(makefile)
        for name in _MAKEFILE_NAMES
        for makefile in package_root.rglob(name)
    )
    return has_recipe or _find_cmake_dir(package_root) is not None or bool(
        _native_sources(package_root)
    )


def build_player(source_root: Path, build_root: Path) -> Path:
    package_hash = tree_sha256(source_root)
    return compile_cpp_package(
        source_root,
        build_root / f"player-v{_NATIVE_BUILD_POLICY_VERSION}-{package_hash[:16]}",
    )


def build_cpp_player(
    *,
    player_dir: str | Path,
    game_dir: str | Path,
    cache_root: str | Path,
    runtime_files: Mapping[str, Path] | None = None,
    timeout: float = 300.0,
) -> Path:
    """Build an immutable player with game SDK and policy-aware cache identity."""

    player_root = Path(player_dir).resolve()
    game_root = Path(game_dir).resolve()
    cache = Path(cache_root).resolve()
    catalog = NativeSdkCatalog.load(game_root)
    source_hash = tree_sha256(player_root)
    compiler = _compiler_identity()
    runtime_identity = [
        {
            "requested_path": requested_path,
            "source_path": str(Path(source).resolve()),
            "sha256": sha256_file(Path(source).resolve()),
        }
        for requested_path, source in sorted((runtime_files or {}).items())
    ]
    identity_payload = {
        "build_sandbox_policy_version": BUILD_SANDBOX_POLICY_VERSION,
        "native_build_policy_version": _NATIVE_BUILD_POLICY_VERSION,
        "source_build_policy_version": SOURCE_BUILD_POLICY_VERSION,
        "source_sha256": source_hash,
        "sdk_catalog_identity": catalog.identity,
        "compiler_identity": compiler,
        "runtime_files": runtime_identity,
    }
    canonical = json.dumps(
        identity_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    cache_identity = hashlib.sha256(canonical).hexdigest()
    metadata = {
        **identity_payload,
        "cache_identity": cache_identity,
        "game": game_root.name,
        "player_source": str(player_root),
    }
    return compile_cpp_package(
        player_root,
        cache / f"player-{cache_identity[:24]}",
        timeout=timeout,
        sdk_catalog=catalog,
        runtime_files=runtime_files,
        build_metadata=metadata,
    )

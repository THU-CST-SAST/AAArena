"""Regression coverage for the formal read-only resource bootstrap."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from aa_arena.benchmark.service import BenchmarkService
from aa_arena.core.build_sandbox import run_isolated_build
from aa_arena.core.buildcache import published_build_dir

GAMES = (
    "dorado",
    "lota",
    "monecraft",
    "pacman",
    "generals",
    "snakego",
    "antwar",
    "antwar2",
    "aquawar",
    "lostspace",
    "miracle",
    "rollman",
)


def _tree_state(root: Path) -> dict[str, tuple[int, str | None]]:
    return {
        str(path.relative_to(root)): (
            stat.S_IMODE(path.stat().st_mode),
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in (root, *sorted(root.rglob("*")))
    }


@pytest.mark.parametrize("game", GAMES)
def test_real_service_preflight_from_read_only_public_starter(tmp_path: Path, game: str) -> None:
    # No mocked bundle, compiler, or preflight: exercise the exact formal boot.
    with BenchmarkService(
        tmp_path / "run", game=game, model_profile="test-no-model", workers=1
    ) as service:
        service.initialize_workspace()
        strategy = service.workspace / "strategy"
        resources = service.workspace / "resources"
        assert strategy.stat().st_mode & 0o200
        # Exercise compilation of a frozen public copy while normal working
        # strategy files are writable for a non-root coding agent.
        for path in (strategy, *strategy.rglob("*")):
            if not path.is_symlink():
                path.chmod(0o555 if path.is_dir() else 0o444)
        original_strategy = _tree_state(strategy)
        original_resources = _tree_state(resources)
        assert any(mode == 0o444 for mode, _ in original_strategy.values())
        assert any(mode == 0o555 for mode, _ in original_strategy.values())
        before = service.ledger.state()
        service._preflight()
        service._preflight()  # Cached inspection must keep the same boundary.
        assert _tree_state(strategy) == original_strategy
        assert _tree_state(resources) == original_resources
        after = service.ledger.state()
        assert after["small_used"] == before["small_used"] == 0
        assert after["large_used"] == before["large_used"] == 0
        assert after["token_usage"] == before["token_usage"]
        assert service.ledger.submissions() == []


@pytest.mark.parametrize("output_name", ["main", "player.so"])
@pytest.mark.parametrize("output_mode", [0o444, 0o555])
def test_disposable_copy_can_replace_read_only_prebuilt_output(
    tmp_path: Path,
    output_name: str,
    output_mode: int,
) -> None:
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (nested / "main.cpp").write_text('#include "public.hpp"\nint main() { return VALUE; }\n')
    (nested / "main.cpp").chmod(0o444)
    output = nested / output_name
    output.write_bytes(b"old frozen prebuilt artifact")
    output.chmod(output_mode)
    nested.chmod(0o555)
    source.chmod(0o555)
    sdk = tmp_path / "declared-sdk"
    sdk.mkdir()
    (sdk / "public.hpp").write_text("#define VALUE 0\n")
    (sdk / "public.hpp").chmod(0o444)
    sdk.chmod(0o555)
    before = _tree_state(source)
    sdk_before = _tree_state(sdk)
    target = tmp_path / "published"
    with published_build_dir(target, source=source) as (work, reused):
        assert not reused
        command = ["g++", "-I", str(sdk), "main.cpp", "-o", output_name]
        if output_name == "player.so":
            command += ["-shared", "-fPIC"]
        result = run_isolated_build(
            command,
            cwd=work / "nested",
            build_root=work,
            readonly_paths=(sdk,),
        )
        assert result.returncode == 0, result.stderr
        assert (work / "nested" / output_name).read_bytes().startswith(b"\x7fELF")
        assert stat.S_IMODE((work / "nested/main.cpp").stat().st_mode) == 0o644
        assert stat.S_IMODE(work.stat().st_mode) == 0o755
        assert stat.S_IMODE((work / "nested").stat().st_mode) == 0o755
        assert os.access(work / "nested" / output_name, os.X_OK)
    assert _tree_state(source) == before
    assert _tree_state(sdk) == sdk_before
    assert (target / ".aa_arena-complete").is_file()

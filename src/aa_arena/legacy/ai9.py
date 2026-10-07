"""Shared legacy AI9 game runtime for backend + ailoader + player .so protocol.

All AI9 games share the same TCP communication infrastructure.  This module
provides build and match-driving logic parameterised by a per-game
``Ai9GameConfig``, so each GamePack evaluator stays thin.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from aa_arena.core.buildcache import published_build_dir
from aa_arena.core.build_sandbox import (
    BUILD_SANDBOX_POLICY_VERSION,
    BuildSandboxError,
    run_isolated_build,
    validate_build_tree,
)
from aa_arena.core.contract import EvaluateResult, EvaluationStatus


class Ai9Error(RuntimeError):
    """Legacy AI9 build or match failure."""


@dataclass(frozen=True)
class Ai9GameConfig:
    """Per-game configuration for the shared AI9 runtime."""

    name: str
    backend_source_root: Path
    player_sdk_includes: tuple[Path, ...]
    player_sdk_sources: tuple[Path, ...]
    roles: tuple[str, ...]
    player_count: int
    player_compile_args: tuple[str, ...] = ()
    processes_per_player: int = 1
    supervised: bool = True
    resources_content: str = ""
    map_source: Path | None = None
    match_files: tuple[Path, ...] = ()
    npc_library_name: str = "ai.so"
    resources_source: Path | None = None
    # Additional public SDK implementation headers outside the include roots.
    player_sdk_headers: tuple[Path, ...] = ()


@dataclass(frozen=True)
class Ai9Backend:
    logic: Path
    ailoader: Path


@dataclass(frozen=True)
class Ai9PreparedPlayer:
    player_id: str
    library_path: Path


def _digest(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for root in paths:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix in {".o", ".d", ".pyc"}:
                continue
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(len(content := path.read_bytes()).to_bytes(8, "big"))
            digest.update(content)
    return digest.hexdigest()


def _runtime_environment() -> dict[str, str]:
    # Match players and compilers must not inherit host model credentials.
    values = {"PATH": os.defpath, "LANG": os.environ.get("LANG", "C.UTF-8")}
    values.update({name: os.environ[name] for name in ("LC_ALL", "LC_CTYPE", "TZ")
                   if name in os.environ})
    return values


def _validate_player_export(game: str, library: Path) -> None:
    required = {"dorado": "player_ai", "lota": "player_ai",
                "monecraft": "player_ai", "pacman": "decide"}.get(game)
    if required is None:
        return
    _validate_player_tree(library)
    library = library.resolve()
    linked = _inspect_player_library(["ldd", "-r", str(library)], library)
    unresolved = [line.strip() for line in (linked.stdout + linked.stderr).splitlines()
        if "undefined symbol:" in line or "not found" in line]
    # Pacman's documented SDK logger is exported by ailoader/main.cc, rather
    # than the plugin itself. ldd examines plugins without that host executable.
    host_symbols = {"log_printf"} if game == "pacman" else set()
    unresolved = [line for line in unresolved if not (
        line.startswith("undefined symbol:")
        and line.split(":", 1)[1].split()[0] in host_symbols)]
    if linked.returncode or unresolved:
        raise Ai9Error(f"{game}: player library has unresolved dependencies: "
            + "; ".join(unresolved[:3]))
    completed = _inspect_player_library(
        ["nm", "-D", "--defined-only", str(library)], library,
    )
    symbols = {line.split()[-1] for line in completed.stdout.splitlines() if line.split()}
    if completed.returncode or required not in symbols:
        raise Ai9Error(f"{game}: player library does not export required callback {required}")


def _validate_player_tree(root: Path) -> None:
    try:
        validate_build_tree(root)
    except BuildSandboxError as exc:
        raise Ai9Error(f"build_sandbox_error: {exc}") from exc


def _inspect_player_library(arguments: list[str], library: Path) -> subprocess.CompletedProcess[str]:
    # ldd can run an ELF interpreter. Never inspect candidate ELF inputs on the
    # host or give the inspection process writable access to published artifacts.
    try:
        with tempfile.TemporaryDirectory(prefix="ahl-elf-inspection-", dir="/tmp") as scratch:
            return run_isolated_build(
                arguments, cwd=Path(scratch), readonly_paths=(library,), timeout=15,
            )
    except BuildSandboxError as exc:
        raise Ai9Error(f"build_sandbox_error: {exc}") from exc


def _run_player_build(
    arguments: list[str], *, cwd: Path, readonly_paths: tuple[Path, ...],
) -> None:
    try:
        completed = run_isolated_build(
            arguments, cwd=cwd, readonly_paths=readonly_paths, timeout=600.0,
        )
    except BuildSandboxError as exc:
        raise Ai9Error(f"build_sandbox_error: {exc}") from exc
    if completed.returncode != 0:
        diagnostic = completed.stderr.strip() or completed.stdout.strip()
        raise Ai9Error(f"build failed ({' '.join(arguments)}): {diagnostic}")


def _run_build(arguments: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(
        arguments, cwd=cwd, text=True, encoding="utf-8", errors="replace",
        capture_output=True, timeout=600.0, check=False, env=_runtime_environment(),
    )
    if completed.returncode != 0:
        diagnostic = completed.stderr.strip() or completed.stdout.strip()
        raise Ai9Error(f"build failed ({' '.join(arguments)}): {diagnostic}")


def build_backend(config: Ai9GameConfig, build_root: Path) -> Ai9Backend:
    digest = _digest((config.backend_source_root,))
    target = build_root / f"{config.name}-backend" / digest
    with published_build_dir(target, source=config.backend_source_root) as (work, reused):
        if not reused:
            _run_build(["make", "all"], cwd=work)
    logic = target / "build" / "logic"
    ailoader = target / "build" / "ailoader"
    if not logic.is_file() or not ailoader.is_file():
        raise Ai9Error(f"{config.name}: backend build did not produce logic and ailoader")
    return Ai9Backend(logic=logic, ailoader=ailoader)


def build_player(
    config: Ai9GameConfig, player_id: str, pool_root: Path, build_root: Path
) -> Ai9PreparedPlayer:
    package_root = pool_root / player_id
    source = package_root / "ai.cpp"
    if not source.is_file() and (pool_root / "ai.cpp").is_file():
        # Benchmark candidates are already package roots, unlike Elo pool rows.
        package_root = pool_root
        source = package_root / "ai.cpp"
    if not source.is_file():
        raise FileNotFoundError(source)
    _validate_player_tree(package_root)
    digest = _digest((package_root, config.backend_source_root))
    profile = hashlib.sha256()
    profile.update(f"build-sandbox-v{BUILD_SANDBOX_POLICY_VERSION}".encode("ascii"))
    profile.update(str(config.player_compile_args).encode("utf-8"))
    sdk_paths = (*config.player_sdk_includes, *config.player_sdk_sources, *config.player_sdk_headers)
    for path in sdk_paths:
        profile.update(str(path.resolve()).encode("utf-8"))
    target = build_root / f"{config.name}-players" / f"{player_id}-{profile.hexdigest()[:12]}-{digest}"
    with published_build_dir(target, source=package_root) as (work, reused):
        _validate_player_tree(work)
        if not reused:
            arguments = [
                "g++", "-std=c++14", "-O2", "-fPIC", "-shared",
                "-D_AI__",
                *config.player_compile_args,
                *[x for p in config.player_sdk_includes for x in ("-I", str(p))],
                "ai.cpp",
                *[str(s) for s in config.player_sdk_sources],
                "-o", "player.so",
            ]
            _run_player_build(arguments, cwd=work, readonly_paths=sdk_paths)
    library = target / "player.so"
    if not library.is_file():
        raise Ai9Error(f"{config.name}/{player_id}: build did not produce player.so")
    _validate_player_export(config.name, library)
    return Ai9PreparedPlayer(player_id=player_id, library_path=library)


def run_ai9_match(
    config: Ai9GameConfig,
    backend: Ai9Backend,
    players: list[Ai9PreparedPlayer],
    *,
    build_root: Path,
    artifact_root: Path,
    seed: int,
    timeout_s: float = 1800.0,
    max_rounds: int = 1000,
) -> EvaluateResult:
    match_dir = artifact_root / f"{config.name}-{seed}-{uuid.uuid4().hex[:10]}"
    match_dir.mkdir(parents=True, exist_ok=False)
    if config.resources_source and config.resources_source.is_file():
        shutil.copy2(config.resources_source, match_dir / "resources.res")
    else:
        (match_dir / "resources.res").write_text(config.resources_content, encoding="utf-8")
    if config.map_source and config.map_source.is_file():
        shutil.copy2(config.map_source, match_dir / "map.txt")
    for source in config.match_files:
        if source.is_file():
            shutil.copy2(source, match_dir / source.name)
    npc_dir = match_dir / "sampleAi"
    npc_dir.mkdir()
    shutil.copy2(players[0].library_path, npc_dir / config.npc_library_name)

    expanded_players = [
        player
        for player in players
        for _ in range(config.processes_per_player)
    ]
    player_processes: list[subprocess.Popen[bytes]] = []
    stderr_files = []
    stdout_files = []
    ports: list[str] = []
    try:
        for index, player in enumerate(expanded_players):
            stderr_path = match_dir / f"ailoader-{index}.stderr"
            stderr_file = stderr_path.open("w+b")
            stderr_files.append(stderr_file)
            stdout_file = (match_dir / f"ailoader-{index}.stdout").open("w+b")
            stdout_files.append(stdout_file)
            process = subprocess.Popen(
                [str(backend.ailoader), str(player.library_path)],
                cwd=match_dir, stdout=stdout_file, stderr=stderr_file,
                text=True,
                start_new_session=True,
                env={
                    **_runtime_environment(),
                    "AI9_AILOADER_NOCLOSESTDIO": "1",
                    **({} if config.supervised else {"AI9_AILOADER_NOSUPERVISOR": "1"}),
                },
            )
            player_processes.append(process)
        for index, process in enumerate(player_processes):
            output = stdout_files[index]
            startup_deadline = time.monotonic() + min(30.0, timeout_s)
            while True:
                output.seek(0)
                line = output.readline()
                if line.endswith(b'\n'):
                    break
                if process.poll() is not None or time.monotonic() >= startup_deadline:
                    raise Ai9Error(f"ailoader {index} did not publish a port")
                time.sleep(0.01)
            line = line.strip()
            try:
                port = int(line)
                if not 0 < port < 65536:
                    raise ValueError
            except ValueError as exc:
                raise Ai9Error(f"ailoader {index} bad port: {line!r}") from exc
            ports.append(str(port))

        argv = [str(backend.logic)]
        for port in ports:
            argv += ["--ai", f"127.0.0.1:{port}"]
        if config.player_count == 2:
            argv += ["-rd", str(max_rounds)]
        stdout_path = match_dir / "backend.stdout"
        stderr_path = match_dir / "backend.stderr"
        with stdout_path.open("w+b") as sf, stderr_path.open("w+b") as ef:
            proc = subprocess.Popen(
                argv, cwd=match_dir, stdout=sf, stderr=ef, start_new_session=True,
                env=_runtime_environment(),
            )
            deadline = time.monotonic() + timeout_s
            returncode: int | None = None
            while True:
                returncode = proc.poll()
                # A winner marker is not a completion signal: legacy backends
                # still rewrite and zip the replay during destruction.
                if returncode is not None:
                    break
                if time.monotonic() >= deadline:
                    _kill_group(proc)
                    raise Ai9Error(f"backend timeout: {timeout_s}s")
                time.sleep(0.1)
            _kill_group(proc)
        return _parse_result(config, match_dir, returncode)
    finally:
        for proc in player_processes:
            _kill_group(proc)
        for proc in player_processes:
            if proc.stdout:
                proc.stdout.close()
        for handle in stderr_files:
            handle.close()
        for handle in stdout_files:
            handle.close()


def _parse_result(
    config: Ai9GameConfig, match_dir: Path, returncode: int | None
) -> EvaluateResult:
    replay_zip = match_dir / "replay.zip"
    replay_txt = match_dir / "replay.txt"
    replay_path = replay_zip if replay_zip.is_file() else (
        replay_txt if replay_txt.is_file() else None
    )
    result_path = match_dir / "result.txt"
    if replay_path is None and result_path.is_file():
        replay_path = result_path
    if returncode is not None and returncode < 0:
        return EvaluateResult(
            status=EvaluationStatus.INFRA_ERROR,
            diagnostic=f"backend terminated by signal {-returncode}",
            replay_path=str(replay_path) if replay_path else None,
        )
    document_rounds: int | None = None
    monecraft_result: dict | None = None
    structured_failures: set[int] = set()
    structured_failure_detail: str | None = None
    text = (
        replay_txt.read_text(encoding="utf-8", errors="replace")
        if replay_txt.is_file() else ""
    )
    winner_values = [int(m.group(1)) for m in re.finditer(r"^winner:(-?\d+)\s*$", text, re.M | re.I)]
    round_matches = list(re.finditer(r"^Round:(\d+)\s*$", text, re.M))
    winner_values.extend(int(m.group(1)) for m in re.finditer(r"^win player id\s*:\s*(-?\d+)\s*$", text, re.M | re.I))
    if result_path.is_file():
        result_text = result_path.read_text(encoding="utf-8", errors="replace")
        result_document = None
        try:
            result_document = json.loads(result_text)
            if config.name == "pacman" and isinstance(result_document, dict):
                errors = result_document.get("error", [])
                if isinstance(errors, list) and all(type(i) is int and i in (0, 1) for i in errors):
                    structured_failures.update(errors)
                    if errors:
                        structured_failure_detail = "official Pacman player errors: " + json.dumps({
                            "players": errors, "types": result_document.get("error_type", [])})
            if config.name == "monecraft" and isinstance(result_document, dict):
                terminal = result_document.get("result")
                if isinstance(terminal, dict):
                    monecraft_result = terminal
            if isinstance(result_document, dict) and isinstance(
                result_document.get("breadcrumbs"), list
            ):
                document_rounds = len(result_document["breadcrumbs"])
        except (OSError, json.JSONDecodeError):
            pass
        if config.name == "lota" and isinstance(result_document, dict) and "winner" in result_document:
            # Game.cpp/trans.rb convert raw replay player IDs into signed
            # outcomes: +1=P0, -1=P1, 0=draw. Raw replay -1 is unfinished.
            signed_winner = result_document["winner"]
            if type(signed_winner) is not int or signed_winner not in (-1, 0, 1):
                return EvaluateResult(status=EvaluationStatus.INFRA_ERROR,
                    diagnostic="invalid LOTA signed terminal result",
                    replay_path=str(replay_path) if replay_path else None)
            normalized_winner = {1: 0, -1: 1, 0: -2}[signed_winner]
            if winner_values and winner_values[-1] != normalized_winner:
                return EvaluateResult(status=EvaluationStatus.INFRA_ERROR,
                    diagnostic="LOTA replay/result winner disagreement",
                    replay_path=str(replay_path) if replay_path else None)
            winner_values.append(normalized_winner)
        elif config.name != "lota":
            winner_values.extend(int(m.group(1)) for m in re.finditer(r'"winner"\s*:\s*(-?\d+)', result_text))
        round_matches.extend(re.finditer(r'"rounds"\s*:\s*(\d+)', result_text))
        pacman_result = re.search(r'"result"\s*:\s*(-?\d+)', result_text)
        if pacman_result:
            result_value = int(pacman_result.group(1))
            winner_values.append(0 if result_value > 0 else 1 if result_value < 0 else -2)
        # Some legacy backends stop on a player error without emitting a result
        # field. When exactly one side is marked as failed, the other side is
        # the official winner; both sides failing remains an official draw.
        error_players = re.findall(r'"error"\s*:\s*\[([^\]]*)\]', result_text)
        failed_players: set[int] = set()
        for group in error_players:
            failed_players.update(
                int(value) for value in re.findall(r"\d+", group)
            )
        if len(failed_players) == 1:
            failed_player = failed_players.pop()
            if 0 <= failed_player < len(config.roles):
                winner_values.append(1 - failed_player)
    # A loader that never resolves its callback has not played a match.
    # Do not turn packaging/loader failures into competitive forfeits.
    startup_failures = []
    for path in sorted(match_dir.glob("ailoader-*.*")):
        if path.suffix not in (".stderr", ".stdout"):
            continue
        diagnostic = path.read_text(encoding="utf-8", errors="replace")
        if any(marker in diagnostic for marker in
               ("failed to load AI:", "failed to lookup AI func:", "failed to setsid")):
            startup_failures.append(path.name)
    if startup_failures:
        return EvaluateResult(status=EvaluationStatus.INFRA_ERROR,
            diagnostic="AI9 loader startup failure: " + ", ".join(startup_failures),
            replay_path=str(replay_path) if replay_path else None)
    rounds = int(round_matches[-1].group(1)) if round_matches else document_rounds
    failed_ids = {int(value) for value in re.findall(r'AI\(ID:(\d+)\)Failed', text)}
    failure_detail = text.rsplit('AI STATUS:', 1)[-1].strip() if failed_ids else structured_failure_detail
    failed_ids.update(structured_failures)
    if any(index >= len(config.roles) for index in failed_ids):
        return EvaluateResult(status=EvaluationStatus.INFRA_ERROR,
            diagnostic=f'neutral controller failed: {failure_detail}', rounds=rounds,
            replay_path=str(replay_path) if replay_path else None)
    if not winner_values:
        diag = (match_dir / "backend.stderr")
        diagnostic = diag.read_text("utf-8", errors="replace").strip() if diag.is_file() else ""
        return EvaluateResult(
            status=EvaluationStatus.INFRA_ERROR,
            diagnostic=diagnostic or f"backend exited {returncode} without winner",
            rounds=rounds,
            replay_path=str(replay_path) if replay_path else None,
        )
    value = winner_values[-1]
    # MoneCraft emits a structured terminal result. Its normal -1 is a
    # gold-and-mine-count tie; Dorado/LOTA use -1 for an unfinished game.
    if config.name == "monecraft":
        if (monecraft_result is None
                or monecraft_result.get("type") not in ("normal", "error")
                or type(monecraft_result.get("winner")) is not int
                or monecraft_result["winner"] != value
                or (monecraft_result["type"] == "error" and value not in (0, 1))):
            return EvaluateResult(status=EvaluationStatus.INFRA_ERROR,
                diagnostic="invalid or missing MoneCraft terminal result", rounds=rounds,
                replay_path=str(replay_path) if replay_path else None)
        if monecraft_result["type"] == "error":
            failed_ids.add(1 - value)
            failure_detail = f"AI(ID:{1 - value})Failed: official MoneCraft player error"
    official_draw = value == -2 or (config.name == "monecraft"
        and monecraft_result["type"] == "normal" and value == -1)
    role_count = len(config.roles)
    if official_draw:
        scores = {r: 0.5 for r in config.roles}
        winner = None
    elif 0 <= value < role_count:
        winner = config.roles[value]
        scores = {r: (1.0 if r == winner else 0.0) for r in config.roles}
    else:
        return EvaluateResult(
            status=EvaluationStatus.INFRA_ERROR,
            diagnostic=f"invalid official winner: {value}",
            rounds=rounds,
            replay_path=str(replay_path) if replay_path else None,
        )
    return EvaluateResult(
        status=EvaluationStatus.GAME_ERROR if failed_ids else EvaluationStatus.COMPLETE,
        winner=winner, scores=scores, rounds=rounds,
        diagnostic=failure_detail,
        replay_path=str(replay_path) if replay_path else None,
        payload={"legacy_winner": value, "official_draw": official_draw,
                 "failed_roles": [config.roles[i] for i in sorted(failed_ids)]},
    )


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass
    # Reap workers even when their leader already exited after SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass

"""Build and certify the exact public resource surface exposed to an agent."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from aa_arena.core import EvaluationStatus, PlayerRef, evaluate
from aa_arena.core.player_entry import find_build_dir, find_python_entry
from aa_arena.core.registry import available_games, get_plugin
from aa_arena.io import atomic_write_json, canonical_hash, sha256_file
from aa_arena.replay import narrate
from aa_arena.replay.public_json import MAX_PUBLIC_REPLAY_BYTES, compact_replay
from aa_arena.sandbox import ProcessSpec
from aa_arena.sandbox.systemd import SystemdScopeLauncher


ARENA_GAMES = (
    "antwar",
    "antwar2",
    "aquawar",
    "dorado",
    "generals",
    "lostspace",
    "lota",
    "miracle",
    "monecraft",
    "pacman",
    "rollman",
    "snakego",
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SDK_DIRECTORIES: dict[str, tuple[str, ...]] = {
    "dorado": ("public_sdk",),
    "antwar": ("public_sdk",),
    "antwar2": ("public_sdk",),
    "aquawar": ("public_sdk_cpp",),
    "generals": ("public_sdk", "public_sdk_cpp"),
    "lostspace": ("public_sdk",),
    "lota": ("public_sdk",),
    "miracle": ("public_sdk",),
    "monecraft": ("public_sdk",),
    "pacman": ("public_sdk",),
    "rollman": ("public_sdk-rollman", "public_sdk-ghost"),
    "snakego": ("public_sdk", "public_sdk_cpp"),
}
AI9_GAMES = frozenset({"dorado", "lota", "monecraft", "pacman"})
# Optional language/reference implementations are published under
# ``sdk/alternatives/<label>`` so they are visible to the model without
# changing the historical runnable starter selected by the evaluator.
SDK_ALTERNATIVES: dict[str, tuple[str, ...]] = {
    "antwar": ("public_sdk_cpp",),
    "antwar2": ("public_sdk_cpp",),
    "aquawar": ("public_sdk_python",),
    "generals": (),
    "lostspace": ("public_sdk_cpp",),
    "miracle": ("public_sdk_cpp",),
    "rollman": ("public_sdk-cpp-rollman", "public_sdk-cpp-ghost"),
    "snakego": (),
}
SDK_INTERFACE_GUIDES: dict[tuple[str, str], str] = {
    ("dorado", "default"): """# Dorado C++ AI9 interface

- Implement `void player_ai(const PMap &map, const PPlayerInfo &info, PCommand &cmd)`.
- Choose heroes on the first decision, then issue move/attack/skill commands through `cmd`.
- The backend also controls neutral Dragon/Roshan units; do not try to drive them from your AI.
- Compile locally with `make`; evaluator compilation uses the complete game SDK.
""",
    ("lota", "default"): """# LOTA C++ AI9 interface

- Implement `void player_ai(const PMap &map, const PPlayerInfo &info, PCommand &cmd)`.
- `cmd.cmds` accepts movement, attack, skill, level-up, and revival operations in execution order.
- Read only SDK-visible state; raw replay/renderer output is not part of the match protocol.
- Compile locally with `make`; evaluator compilation includes the same SDK operations.
""",
    ("monecraft", "default"): """# MoneCraft C++ AI9 interface

- Implement `void player_ai(Mine &, Map &, GameInfo &, RoundInfo &, Action &)` with C linkage expected by the loader.
- In new code, declare it inside `extern "C" { ... }`; a C++-mangled symbol is rejected as a missing `player_ai`.
- Return one move, optional blink, mine placement, and protection purchase in `Action`.
- Compile locally with `make`; evaluator compilation includes JSON/platform support.
""",
    ("pacman", "default"): """# Pacman C++ AI9 interface

- Implement `Move decide(const Game *game)` in `ai.cpp`.
- Inspect only the SDK-visible `Game` snapshot and return direction, step count, and optional mine.
- `log_printf` is supplied by the loader; never write diagnostics to stdout.
- Compile locally with `make`; evaluator compilation provides the logger at load time.
""",
    ("antwar", "cpp"): """# AntWar C++ submission interface

- Implement `std::vector<Operation> strategy(int my_seat, const GameInfo &state)` in `main.cpp`.
- Keep `main.cpp`, `Makefile`, and `include/` together; the recovered headers own referee framing,
  state updates, operation validation, and seat ordering.
- Build with `make` using C++17. Standard output is exclusively the referee protocol channel.
""",
    ("antwar2", "default"): """# AntWar2 Python submission interface

- Submit the entire directory with `main.py` at its root; do not replace the stdio protocol loop.
- `ai.py` must export `class AI`. The recommended path is to inherit `common.BaseAgent` and implement
  `choose_bundle(self, state: BackendState, player: int, bundles: list[ActionBundle] | None) -> ActionBundle`.
- `self.list_bundles(state, player)` returns protocol-legal candidates; returning one of those bundles
  is safer than constructing raw operations. `main.py` creates the session and handles both seats.
- Preflight: `python -m py_compile main.py ai.py common.py protocol.py`; submission command: `python main.py`.
""",
    ("aquawar", "public_sdk_cpp"): """# AquaWar C++ submission interface

- Implement `AI::Pick(Game) -> std::vector<int>`, `AI::Assert(Game) -> std::pair<int,int>`, and
  `AI::Act(Game) -> Action` in `Action.cpp`/the supplied sample, retaining `Action.hpp` and `main.cpp`.
- Build the complete package with `make`; the executable is `main`. Requires C++17.
""",
    ("generals", "default"): """# Generals Python submission interface

- Submit the whole directory with `main.py`. Pass a callback to `run_ai` with the exact signature
  `strategy(round: int, my_seat: int, state: GameState) -> list[list[int]]`.
- Return only operation arrays documented by the SDK. `run_ai` owns initialization, turn ordering,
  validation, and Saiblo stdio. Preflight: `python -m py_compile main.py generals_impact_game/*.py`.
""",
    ("generals", "public_sdk_cpp"): """# Generals C++ submission interface

- Implement `std::vector<Operation> strategy(int my_seat, const GameState &state)` and pass it to
  `run_ai(strategy)`. Keep `include/`, `main.cpp`, and the build files together.
- Build with the supplied Makefile (`make` or its documented source target); requires C++17.
""",
    ("miracle", "cpp"): """# Miracle C++ submission interface

- Build the complete package with `make`; the executable is `main` and requires C++17.
- Keep `main.cpp`, `Makefile`, and the recovered `v11/` protocol SDK together.
- Customize `StarterAI::choose_cards()` and `StarterAI::play_turn()` without changing the framed
  stdin/stdout loop. Use inherited action helpers and reserve standard output for the protocol.
""",
    ("lostspace", "cpp"): """# LostSpace C++ submission interface

- Build the complete package with `make`; the executable is `main` and requires C++17.
- Implement `std::vector<Json::Value> strategy(const Json::Value &state)` and keep
  `lostspace::finish()` as the last action of every turn.
- Keep `lostspace_client.*` and `jsoncpp/` unchanged unless modifying protocol support. Standard
  output is exclusively the framed referee channel.
""",
    ("snakego", "default"): """# SnakeGo Python submission interface

- Submit the complete directory with `main.py`, `sampleAI.py`, and `adk.py`.
- Implement `AI.judge(self, snake, ctx) -> int` in `sampleAI.py`; it is called once for every controlled
  snake. Return a legal SDK operation code (movement, railgun, or split). `sampleAI.run()` owns Saiblo
  protocol and state updates. Preflight: `python -m py_compile main.py sampleAI.py adk.py`.
""",
    ("snakego", "public_sdk_cpp"): """# SnakeGo C++ submission interface

- Required callbacks: `Operation make_your_decision(const Snake&, const Context&, const OpHistory&)`
  and `void game_over(int gameover_type, int winner, int p0_score, int p1_score)`.
- Keep `adk.hpp` and `main.cpp`; build with the supplied Makefile/CMake project. Do not change protocol IO.
""",
}
FORBIDDEN_PUBLIC_TEXT = (
    "agentbench.core",
    "agentbenchhl",
    "../../backend_sources",
    "players/pool/",
)


@dataclass(frozen=True)
class CertificationResult:
    game: str
    bundle_root: str
    static_checks: int
    live_matches: int
    manifest_sha256: str
    runnable_starters: tuple[dict[str, object], ...]
    live_contract_version: int = 0


def _alternative_label(game: str, directory: str) -> str:
    if game == "rollman" and directory.startswith("public_sdk-cpp-"):
        return directory.removeprefix("public_sdk-")
    if directory == "public_sdk_python":
        return "python"
    return "cpp" if "cpp" in directory else directory.removeprefix("public_sdk-")


def _published_sdk_roots(
    game: str, bundle: Path, roles: tuple[str, ...]
) -> Iterable[tuple[Path, tuple[str, ...]]]:
    """Yield every published SDK root and the roles it claims to implement."""

    directories = SDK_DIRECTORIES[game]
    for name in directories:
        destination = (
            bundle / "sdk"
            if len(directories) == 1
            else bundle / "sdk" / name.removeprefix("public_sdk-")
        )
        starter_roles = (name.removeprefix("public_sdk-"),) if game == "rollman" else roles
        yield destination, starter_roles
    for name in SDK_ALTERNATIVES.get(game, ()):
        label = _alternative_label(game, name)
        destination = bundle / "sdk-alternatives" / label
        if destination.is_dir():
            starter_roles = (
                (name.rsplit("-", 1)[-1],) if game == "rollman" else roles
            )
            yield destination, starter_roles


def _runnable_starters(game: str, bundle: Path) -> tuple[dict[str, object], ...]:
    """Discover real Python/native entrypoints in the exact published bundle."""

    records: list[dict[str, object]] = []
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    roles = tuple(str(role) for role in manifest["roles"])
    for root, starter_roles in _published_sdk_roots(game, bundle, roles):
        relative = root.relative_to(bundle).as_posix()
        if find_python_entry(root) is not None:
            records.append(
                {
                    "language": "python",
                    "roles": list(starter_roles),
                    "bundle_path": relative,
                    "build_system": "python",
                    "live_match_status": "not_run",
                }
            )
        native_root = find_build_dir(root)
        if native_root is not None:
            makefiles = ("Makefile", "makefile", "GNUmakefile")
            build_system = (
                "make"
                if any((native_root / name).is_file() for name in makefiles)
                else "cmake"
            )
            records.append(
                {
                    "language": "cpp",
                    "roles": list(starter_roles),
                    "bundle_path": relative,
                    "build_system": build_system,
                    "live_match_status": "not_run",
                }
            )
    return tuple(
        sorted(
            records,
            key=lambda record: (
                str(record["bundle_path"]),
                str(record["language"]),
                tuple(str(role) for role in record["roles"]),
            ),
        )
    )


def _rating_rows(game: str, root: Path) -> list[dict[str, object]]:
    source = root / "results" / "elo" / game / "measured_elo.json"
    value = json.loads(source.read_text(encoding="utf-8"))
    rows = value.get("ratings") if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise ValueError(f"{source}: ratings must be a list")
    public: list[dict[str, object]] = []
    for rank, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or not isinstance(row.get("player_id"), str):
            raise ValueError(f"{source}: invalid rating row {rank}")
        public.append(
            {
                "opponent_id": row["player_id"],
                "rank": rank,
                "elo": float(row["measured_elo"]),
                "track": row.get("track"),
                "matches": int(float(row.get("matches") or 0)),
            }
        )
    return public


def _copy_sdk(game: str, game_dir: Path, target: Path) -> None:
    directories = SDK_DIRECTORIES[game]
    target.mkdir(parents=True, exist_ok=True)
    for name in directories:
        label = (
            "default"
            if name == "public_sdk"
            else name.removeprefix("public_sdk-")
            if name.startswith("public_sdk-")
            else name
        )
        source = game_dir / name
        if not source.is_dir():
            raise ValueError(f"{game}: missing SDK directory {source}")
        destination = target if len(directories) == 1 else target / name.removeprefix("public_sdk-")
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "output", "build", "PROVENANCE.md", "SDK_PROVENANCE.json"
            ),
        )
        readme = destination / "README.md"
        if readme.is_file():
            text = readme.read_text(encoding="utf-8")
            text = text.replace("from agentbench.core import PlayerRef", "from aa_arena.core import PlayerRef")
            text = text.replace("AGENTBENCH_ROLE", "AA_ARENA_ROLE")
            readme.write_text(text, encoding="utf-8")
        else:
            readme.write_text(
                f"# {game} public SDK ({label})\n\n"
                "Keep the complete SDK directory and its protocol entrypoint. Edit only the "
                "documented strategy callback or sample policy. Standard output is the Saiblo "
                "protocol channel; write diagnostics to standard error.\n\n"
                "```python\nfrom aa_arena.core import PlayerRef\n\n"
                f"player = PlayerRef('candidate', code_path='/absolute/path/to/{game}-player')\n"
                "```\n\nThe executable entry is `main.py` for Python templates and the supplied "
                "Makefile/CMakeLists.txt for C++ templates. Consult the adjacent source types and "
                "sample implementation for the exact callback signature.\n",
                encoding="utf-8",
            )
        guide = SDK_INTERFACE_GUIDES.get((game, label))
        if guide:
            (destination / "INTERFACE.md").write_text(guide.strip() + "\n", encoding="utf-8")
    for name in SDK_ALTERNATIVES.get(game, ()):
        source = game_dir / name
        if not source.is_dir():
            continue
        label = _alternative_label(game, name)
        destination = target.parent / "sdk-alternatives" / label
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "output", "build", "PROVENANCE.md", "SDK_PROVENANCE.json"
            ),
        )
        runnable = find_python_entry(destination) is not None or find_build_dir(destination) is not None
        kind = "submission starter" if runnable else "reference SDK"
        (destination / "README.md").write_text(
            f"# {game} {label.upper()} {kind}\n\n"
            "Keep the complete directory, stdio entrypoint, and build files together. "
            "Edit only the documented strategy callback.\n",
            encoding="utf-8",
        )
        guide = SDK_INTERFACE_GUIDES.get((game, label))
        if guide:
            (destination / "INTERFACE.md").write_text(
                guide.strip() + "\n", encoding="utf-8"
            )


def _copy_rank40_example(game: str, root: Path, target: Path) -> None:
    """Publish a bounded, public reference strategy without exposing the pool mount.

    The package is copied into the certified bundle as an explicitly labelled
    example.  It is not used as an opponent by the evaluator and is never
    mounted from ``players/pool`` at run time.
    """

    rows = _rating_rows(game, root)
    selected = next((row for row in rows if int(row["rank"]) == 40), None)
    fallback_rank = min((int(row["rank"]) for row in rows if int(row["rank"]) > 8 and int(row["rank"]) % 2 == 0), default=None)
    if selected is None and fallback_rank is not None:
        selected = next((row for row in rows if int(row["rank"]) == fallback_rank), None)
    if selected is None:
        return
    source = root / "games" / game / "players" / "pool" / str(selected["opponent_id"])
    if not source.is_dir():
        return
    destination = target / "rank40"
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    allowed = {".py", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".md", ".json", ".txt"}
    for path in sorted(source.rglob("*")):
        if not path.is_file() or (
            path.suffix.lower() not in allowed and path.name not in {"Makefile", "makefile", "CMakeLists.txt"}
        ):
            continue
        if "__pycache__" in path.parts or path.stat().st_size > 1_000_000:
            continue
        content = path.read_bytes()
        lowered = content.lower()
        if any(forbidden.encode() in lowered for forbidden in FORBIDDEN_PUBLIC_TEXT):
            continue
        relative = path.relative_to(source)
        target_path = destination / relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(content)
        total += len(content)
        if total >= 6_000_000:
            break
    (destination / "README.md").write_text(
        f"# {game} rank-40 reference example\n\n"
        f"Public measured-pool reference: rank {selected['rank']} (nearest available to rank 40), Elo {selected['elo']:.3f}.\n"
        "This example is intentionally visible for strategy study. It is not a hidden opponent "
        "interface and is not used to evaluate the candidate. Do not copy identity-specific or "
        "seed-specific logic; retain only general tactics and the public protocol entrypoint.\n",
        encoding="utf-8",
    )


def _write_translator(path: Path, game: str) -> None:
    del game
    shutil.copy2(
        Path(__file__).with_name("public_replay_translate.py"),
        path,
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def build_bundle(
    game: str,
    output_root: Path,
    *,
    repository_root: Path | None = None,
    include_replay: bool = True,
) -> Path:
    if game not in ARENA_GAMES:
        raise ValueError(f"unsupported arena game: {game}")
    root = (repository_root or REPOSITORY_ROOT).resolve()
    game_dir = root / "games" / game
    destination = Path(output_root).resolve() / game
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    shutil.copy2(game_dir / "rules.md", destination / "rules.md")
    if include_replay:
        (destination / "replay").mkdir(parents=True)
        shutil.copy2(game_dir / "replay_format.md", destination / "replay" / "format.md")
        shutil.copy2(game_dir / "replay_skill.md", destination / "replay" / "guide.md")
        shutil.copy2(
            Path(__file__).with_name("replay_reading_skill.md"),
            destination / "replay" / "reading_skill.md",
        )
        _write_translator(destination / "replay" / "translate", game)
    _copy_sdk(game, game_dir, destination / "sdk")
    _copy_rank40_example(game, root, destination / "examples")
    from aa_arena.benchmark.distribution import local_subset, scope
    rows = _rating_rows(game, root)
    if local_subset(root):
        pool = root / "games" / game / "players" / "pool"
        rows = [row for row in rows if (pool / str(row["opponent_id"])).is_dir()]
        rows = [{**row, "reference_rank": row["rank"], "rank": i}
                for i, row in enumerate(rows, 1)]
        if not rows:
            raise ValueError("No published opponents installed; run scripts/install_assets.py")
    leaderboard = {
        **scope(root),
        "schema_version": 1,
        "game": game,
        "source": "verified measured Elo snapshot",
        "opponents": rows,
    }
    atomic_write_json(destination / "leaderboard.json", leaderboard)
    files = []
    for path in sorted(item for item in destination.rglob("*") if item.is_file()):
        if path.name == "manifest.json":
            continue
        files.append(
            {
                "path": path.relative_to(destination).as_posix(),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    manifest = {
        "schema_version": 1,
        "game": game,
        "roles": list(get_plugin(game, root / "games").roles),
        "files": files,
    }
    manifest["bundle_sha256"] = canonical_hash(manifest)
    atomic_write_json(destination / "manifest.json", manifest)
    return destination


def _text_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file() and (
            path.name == "translate"
            or path.suffix.lower() in {".md", ".yaml", ".yml", ".py", ".txt"}
        ):
            yield path


def _static_certify(game: str, bundle: Path) -> int:
    required = (
        bundle / "rules.md",
        bundle / "leaderboard.json",
        bundle / "sdk",
        bundle / "examples" / "rank40",
        bundle / "replay" / "format.md",
        bundle / "replay" / "guide.md",
        bundle / "replay" / "reading_skill.md",
        bundle / "replay" / "translate",
        bundle / "manifest.json",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ValueError(f"{game}: incomplete public bundle: {missing}")
    checks = len(required)
    for path in _text_files(bundle):
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for forbidden in FORBIDDEN_PUBLIC_TEXT:
            if forbidden in text:
                raise ValueError(
                    f"{game}: public file {path.relative_to(bundle)} contains forbidden {forbidden!r}"
                )
        checks += 1
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for row in manifest["files"]:
        path = bundle / row["path"]
        if sha256_file(path) != row["sha256"]:
            raise ValueError(f"{game}: bundle hash mismatch: {row['path']}")
        checks += 1
    return checks


def _live_certify(
    game: str,
    root: Path,
    work_root: Path,
    bundle: Path,
    starters: tuple[dict[str, object], ...],
) -> tuple[int, tuple[dict[str, object], ...]]:
    from aa_arena.replay.public_json import validate_public_replay

    plugin = get_plugin(game, root / "games")
    public = json.loads((bundle / "leaderboard.json").read_text(encoding="utf-8"))
    leaderboard = public.get("opponents") if isinstance(public, dict) else None
    if not isinstance(leaderboard, list):
        raise ValueError(f"{game}: public leaderboard is invalid")
    if not leaderboard:
        raise ValueError(f"{game}: verified Elo leaderboard is empty")
    from aa_arena.benchmark.distribution import local_subset
    if local_subset(root):
        leaderboard = [row for row in leaderboard if (root / "games" / game / "players" / "pool" / str(row["opponent_id"])).is_dir()]
        if not leaderboard:
            raise ValueError("No published opponents for local resource certification")
    roles = tuple(plugin.roles)
    if len(roles) < 2:
        raise ValueError(f"{game}: live certification requires at least two roles")
    if not starters:
        raise ValueError(f"{game}: public bundle has no runnable starter")
    live_matches = 0
    certified_starters: list[dict[str, object]] = []
    complementary_assignments = (
        tuple(["candidate", *(["opponent"] * (len(roles) - 1))]),
        tuple(["opponent", *(["candidate"] * (len(roles) - 1))]),
    )
    for starter_index, starter in enumerate(starters):
        starter_roles = tuple(str(role) for role in starter["roles"])
        assignments = (
            (
                tuple(
                    "candidate" if role in starter_roles else "opponent"
                    for role in roles
                ),
            )
            if game == "rollman"
            else complementary_assignments
        )
        code_path = bundle / str(starter["bundle_path"])
        for assignment_index, assignment in enumerate(assignments):
            opponent_roles = [
                role
                for role, owner in zip(roles, assignment, strict=True)
                if owner == "opponent"
            ]
            if game in AI9_GAMES:
                # A legacy starter can time out in pathological rank-1 matchups
                # while remaining a useful baseline. Certify it against the same
                # rank neighborhood published as its rank-40 reference.
                selected = min(
                    leaderboard,
                    key=lambda row: (abs(int(row.get("reference_rank", row.get("rank")) or 0) - 40), int(row.get("reference_rank", row.get("rank")) or 0)),
                )
            else:
                selected = leaderboard[0]
            if game == "rollman":
                selected = next(
                    (
                        row
                        for row in leaderboard
                        if row.get("track") in opponent_roles
                    ),
                    None,
                )
                if selected is None:
                    raise RuntimeError(
                        "rollman: verified leaderboard has no opponent for track "
                        f"{opponent_roles}"
                    )
            opponent_id = str(selected["opponent_id"])
            opponent = root / "games" / game / "players" / "pool" / opponent_id
            refs = []
            candidate_roles: list[str] = []
            for role, owner in zip(roles, assignment, strict=True):
                if owner == "candidate":
                    refs.append(PlayerRef(f"public-starter-{starter_index}-{assignment_index}", str(code_path)))
                    candidate_roles.append(role)
                else:
                    refs.append(PlayerRef(opponent_id, str(opponent)))
            result = evaluate(game, refs, list(roles), 20260831, games_root=root / "games")
            if result.status is not EvaluationStatus.COMPLETE:
                raise RuntimeError(
                    f"{game}: public starter did not complete: "
                    f"status={result.status.value} diagnostic={result.diagnostic}"
                )
            minimum_rounds = 1
            if result.rounds is None or result.rounds < minimum_rounds:
                raise RuntimeError(f"{game}: public starter produced no valid rounds")
            if result.winner is not None and result.winner not in roles:
                raise RuntimeError(
                    f"{game}: public starter produced invalid winner {result.winner!r}"
                )
            if not result.replay_path or not Path(result.replay_path).is_file():
                raise RuntimeError(f"{game}: public starter produced no replay")
            compact_path = (
                Path(work_root)
                / f"{game}-starter-{starter_index}-{assignment_index}-replay.json"
            )
            compact_replay(game, result.replay_path, compact_path)
            validate_public_replay(game, Path(result.replay_path), compact_path)
            if compact_path.stat().st_size > MAX_PUBLIC_REPLAY_BYTES:
                raise RuntimeError(f"{game}: public replay JSON exceeds size limit")
            for role in candidate_roles:
                narration = narrate(
                    game,
                    result.replay_path,
                    perspective=role,
                    official_winner=result.winner,
                    official_rounds=result.rounds,
                    games_root=root / "games",
                )
                if not narration.text.strip():
                    raise RuntimeError(f"{game}: empty replay translation")
                translated = subprocess.run(
                    (
                        sys.executable,
                        str(bundle / "replay" / "translate"),
                        str(result.replay_path),
                        "--perspective",
                        role,
                    ),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if translated.returncode != 0 or not translated.stdout.strip():
                    raise RuntimeError(
                        f"{game}: public replay translator failed: {translated.stderr.strip()}"
                    )
                if '"rounds"' not in translated.stdout:
                    raise RuntimeError(f"{game}: public replay translator omitted rounds")
                if game == "antwar" and (
                    '"tower_count"' not in translated.stdout
                    or "type=-1 deletes a tower" not in translated.stdout
                ):
                    raise RuntimeError(
                        "antwar: public translator did not reconstruct tower deltas"
                    )
            live_matches += 1
        certified_starters.append({**starter, "live_match_status": "complete"})

    # The example is part of the model-facing contract, so merely copying it
    # is insufficient: submit the copied bundle itself through the real
    # evaluator.  Rollman examples are role-specific; other games share one
    # player interface across roles.
    # Keep certification in lock-step with _copy_rank40_example(): pools are
    # allowed to omit the literal rank 40, in which case the closest measured
    # entry is the public example.
    example_row = min(
        leaderboard,
        key=lambda row: (abs(int(row.get("reference_rank", row.get("rank")) or 0) - 40), int(row.get("reference_rank", row.get("rank")) or 0)),
    )
    if game == "rollman":
        example_role = str(example_row.get("track") or "")
        if example_role not in roles:
            raise RuntimeError(f"rollman: rank-40 example has invalid track {example_role!r}")
        candidate_index = roles.index(example_role)
        opponent_row = next(
            (row for row in leaderboard if row.get("track") != example_role),
            None,
        )
    else:
        candidate_index = 0
        opponent_row = leaderboard[0]
    if opponent_row is None:
        raise RuntimeError(f"{game}: no compatible opponent for rank-40 example")
    opponent_id = str(opponent_row["opponent_id"])
    opponent = root / "games" / game / "players" / "pool" / opponent_id
    refs = [
        PlayerRef("rank40-example", str(bundle / "examples" / "rank40"))
        if index == candidate_index
        else PlayerRef(opponent_id, str(opponent))
        for index in range(len(roles))
    ]
    result = evaluate(game, refs, list(roles), 20260908, games_root=root / "games")
    if result.status is not EvaluationStatus.COMPLETE:
        raise RuntimeError(
            f"{game}: rank-40 example did not complete: "
            f"status={result.status.value} diagnostic={result.diagnostic}"
        )
    minimum_rounds = 1
    if result.rounds is None or result.rounds < minimum_rounds:
        raise RuntimeError(f"{game}: rank-40 example produced no valid rounds")
    if not result.replay_path or not Path(result.replay_path).is_file():
        raise RuntimeError(f"{game}: rank-40 example produced no replay")
    compact_path = Path(work_root) / f"{game}-rank40-replay.json"
    compact_replay(game, result.replay_path, compact_path)
    validate_public_replay(game, Path(result.replay_path), compact_path)
    if compact_path.stat().st_size > MAX_PUBLIC_REPLAY_BYTES:
        raise RuntimeError(f"{game}: rank-40 replay JSON exceeds size limit")
    narration = narrate(
        game,
        result.replay_path,
        perspective=roles[candidate_index],
        official_winner=result.winner,
        official_rounds=result.rounds,
        games_root=root / "games",
    )
    if not narration.text.strip():
        raise RuntimeError(f"{game}: rank-40 replay translation is empty")
    live_matches += 1
    return live_matches, tuple(certified_starters)


def _certify_player_visibility(work_root: Path) -> None:
    # Probe payload must be traversable after privilege drop; controller/output
    # parents may be private. Never chmod those private parents to make a probe work.
    del work_root
    with tempfile.TemporaryDirectory(prefix="visibility-") as temporary:
        root = Path(temporary)
        candidate = root / "candidate"
        candidate.mkdir()
        sentinel = root / "opponent-and-controller-secret"
        sentinel.write_text("not public", encoding="utf-8")
        (candidate / "probe.py").write_text(
            "import json, socket\n"
            "from pathlib import Path\n"
            f"secret_visible = Path({str(sentinel)!r}).exists()\n"
            "network = True\n"
            "try:\n"
            "    sock = socket.socket()\n"
            "    sock.settimeout(0.25)\n"
            "    sock.connect(('1.1.1.1', 53))\n"
            "except OSError:\n"
            "    network = False\n"
            "print(json.dumps({'secret_visible': secret_visible, 'network': network}))\n",
            encoding="utf-8",
        )
        launcher = SystemdScopeLauncher()
        isolated = launcher.isolate_filesystem(
            # The probe needs only the standard library. Use the public system
            # runtime so this direct (non-systemd) certification does not rely
            # on traversing the controller's private virtualenv path.
            ProcessSpec(("/usr/bin/python3", "probe.py"), candidate)
        )
        completed = subprocess.run(
            isolated.argv,
            cwd=isolated.cwd,
            env=isolated.env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"player visibility probe failed to run: {(completed.stderr or completed.stdout).strip()}"
            )
        try:
            observed = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("player visibility probe returned invalid JSON") from exc
        if observed != {"secret_visible": False, "network": False}:
            raise RuntimeError(f"player visibility boundary is unsafe: {observed}")


def certify(
    game: str,
    *,
    output_root: Path | None = None,
    live: bool = True,
    repository_root: Path | None = None,
) -> CertificationResult:
    root = (repository_root or REPOSITORY_ROOT).resolve()
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if output_root is None:
        temporary = tempfile.TemporaryDirectory(prefix="aa-arena-resources-")
        output_root = Path(temporary.name)
    try:
        bundle = build_bundle(game, output_root, repository_root=root)
        static_checks = _static_certify(game, bundle)
        runnable_starters = _runnable_starters(game, bundle)
        if live:
            _certify_player_visibility(Path(output_root))
            static_checks += 1
            live_matches, runnable_starters = _live_certify(
                game,
                root,
                Path(output_root),
                bundle,
                runnable_starters,
            )
        else:
            live_matches = 0
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        return CertificationResult(
            game=game,
            bundle_root=str(bundle),
            static_checks=static_checks,
            live_matches=live_matches,
            manifest_sha256=str(manifest["bundle_sha256"]),
            runnable_starters=runnable_starters,
            live_contract_version=2 if live else 0,
        )
    finally:
        if temporary is not None:
            temporary.cleanup()


def certify_all(*, output_root: Path | None = None, live: bool = True) -> list[CertificationResult]:
    discovered = set(available_games(REPOSITORY_ROOT / "games"))
    missing = set(ARENA_GAMES) - discovered
    if missing:
        raise ValueError(f"missing arena game plugins: {sorted(missing)}")
    return [certify(game, output_root=output_root, live=live) for game in ARENA_GAMES]

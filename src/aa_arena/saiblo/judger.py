"""One game-independent Saiblo judger for historical stdio players.

The control flow is a subprocess-pipe port of the official local judger. It deliberately remains
fully asynchronous: logic frames and every AI output are pumped independently, which supports
games that request multiple operations during one logical state.
"""

from __future__ import annotations

import json
import os
import queue
import struct
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO

from aa_arena.sandbox import (
    DirectLauncher,
    ManagedProcess,
    ProcessLauncher,
    ProcessSpec,
    SandboxInfrastructureError,
    SystemdScopeLauncher,
)

from .protocol import (
    AiErrorType,
    AuxiliaryMessage,
    GameOver,
    RoundConfig,
    RoundInfo,
    encode_ai_error,
    encode_ai_message,
    encode_init,
    parse_logic_message,
)

DEFAULT_MATCH_TIMEOUT_S = 1800.0
TERMINAL_GRACE_S = 5.0
MAX_FRAME_SIZE = 64 * 1024 * 1024
UPSTREAM_OUTPUT_LIMIT = 2048


class SaibloJudgerError(RuntimeError):
    """The shared transport could not obtain a valid backend terminal frame."""


@dataclass(frozen=True)
class JudgerResult:
    end_message: Mapping[str, Any]
    scores: tuple[Any, ...]
    player_count: int
    final_state: int
    process_returncodes: tuple[int, ...]
    stderr_tails: tuple[str, ...]


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError(f"unexpected EOF after {count - remaining}/{count} bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write(stream: BinaryIO, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(stream.fileno(), view)
        if written < 1:
            raise BrokenPipeError("process pipe accepted zero bytes")
        view = view[written:]


def _append_event(path: Path, kind: str, **details: object) -> None:
    with path.open("a", encoding="utf-8") as event_log:
        event_log.write(json.dumps({"kind": kind, **details}, ensure_ascii=False) + "\n")


def _cleanup_players(
    players: Sequence[ManagedProcess],
    events_path: Path,
) -> list[str]:
    failures: list[str] = []
    for index, process in enumerate(players):
        outcome = process.cleanup()
        _append_event(
            events_path,
            "sandbox_cleanup",
            player=index,
            unit_name=process.metadata.unit_name,
            clean=outcome.clean,
            detail=outcome.detail,
        )
        if not outcome.clean:
            failures.append(
                f"player={index} unit={process.metadata.unit_name}: "
                f"{outcome.detail or 'cleanup did not complete'}"
            )
    return failures


def _backend_reader(stream: BinaryIO, events: queue.Queue[tuple[Any, ...]]) -> None:
    try:
        while True:
            size = struct.unpack(">I", _read_exact(stream, 4))[0]
            target = struct.unpack(">i", _read_exact(stream, 4))[0]
            if size > MAX_FRAME_SIZE:
                raise ValueError(f"logic frame exceeds {MAX_FRAME_SIZE} bytes: {size}")
            events.put(("logic_frame", target, _read_exact(stream, size)))
    except EOFError:
        events.put(("logic_eof",))
    except BaseException as exc:  # noqa: BLE001 - transferred to controller thread
        events.put(("logic_error", exc))


def _player_reader(
    ai_id: int,
    stream: BinaryIO,
    events: queue.Queue[tuple[Any, ...]],
    output_limit: int,
) -> None:
    try:
        while True:
            size = struct.unpack(">I", _read_exact(stream, 4))[0]
            if size > output_limit:
                events.put(("ai_ole", ai_id, size))
                return
            events.put(("ai_frame", ai_id, _read_exact(stream, size)))
    except EOFError:
        events.put(("ai_eof", ai_id))
    except BaseException as exc:  # noqa: BLE001 - transferred to controller thread
        events.put(("ai_error", ai_id, exc))


def _drain_stderr(stream: BinaryIO, destination: Path, tail: bytearray) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        while True:
            try:
                chunk = stream.read(65536)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            handle.write(chunk)
            handle.flush()
            tail.extend(chunk)
            if len(tail) > 8192:
                del tail[:-8192]


def run_stdio_match(
    *,
    backend: ProcessSpec,
    players: Sequence[ProcessSpec],
    config: object,
    replay_path: Path,
    events_path: Path,
    timeout_s: float = DEFAULT_MATCH_TIMEOUT_S,
    player_list: Sequence[int] | None = None,
    player_launcher: ProcessLauncher | None = None,
) -> JudgerResult:
    """Run one Saiblo match with a generic backend and N stdio AI processes."""

    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    specs = tuple(players)
    if not specs:
        raise ValueError("at least one player is required")
    active_players = tuple(1 for _ in specs) if player_list is None else tuple(player_list)
    if len(active_players) != len(specs):
        raise ValueError("player_list length must match players")

    replay_path = Path(replay_path).resolve()
    events_path = Path(events_path).resolve()
    replay_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text("", encoding="utf-8")

    launcher = player_launcher if player_launcher is not None else SystemdScopeLauncher()
    launcher.preflight()
    match_id = events_path.parent.name

    # The official local judger launches logic only after every AI is connected.
    started_players: list[ManagedProcess] = []
    try:
        for index, spec in enumerate(specs):
            process = launcher.start(spec, match_id=match_id, player_index=index)
            started_players.append(process)
            _append_event(events_path, "sandbox_launch", **asdict(process.metadata))
    except BaseException as exc:
        cleanup_failures = _cleanup_players(started_players, events_path)
        if cleanup_failures:
            raise SandboxInfrastructureError(
                f"player sandbox startup failed: {exc}; cleanup: {'; '.join(cleanup_failures)}"
            ) from exc
        raise
    player_processes = tuple(started_players)

    backend_launcher = DirectLauncher()
    try:
        backend_process = backend_launcher.start(
            backend,
            match_id=match_id,
            player_index=-1,
        )
    except BaseException as exc:
        cleanup_failures = _cleanup_players(player_processes, events_path)
        if cleanup_failures:
            raise SandboxInfrastructureError(
                f"backend startup failed: {exc}; player cleanup: {'; '.join(cleanup_failures)}"
            ) from exc
        raise
    processes = (backend_process, *player_processes)
    event_queue: queue.Queue[tuple[Any, ...]] = queue.Queue()
    stderr_tails = tuple(bytearray() for _ in processes)

    threads = [
        threading.Thread(
            target=_backend_reader,
            args=(backend_process.stdout, event_queue),
            daemon=True,
        )
    ]
    threads.extend(
        threading.Thread(
            target=_player_reader,
            args=(index, process.stdout, event_queue, UPSTREAM_OUTPUT_LIMIT),
            daemon=True,
        )
        for index, process in enumerate(player_processes)
    )
    labels = ("backend", *(f"p{index}" for index in range(len(player_processes))))
    threads.extend(
        threading.Thread(
            target=_drain_stderr,
            args=(
                process.stderr,
                events_path.with_suffix(f".{labels[index]}.stderr.log"),
                stderr_tails[index],
            ),
            daemon=True,
        )
        for index, process in enumerate(processes)
    )
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + float(timeout_s)
    current_state = -1
    round_begin = time.monotonic()
    round_time_limit_s = 3.0
    round_deadline: float | None = None
    listen_targets: tuple[int, ...] = ()
    awaiting_replies: set[int] = set()
    reported_errors: set[int] = set()
    pending_ai: dict[int, list[bytes]] = {}
    players_with_input: set[int] = set()
    game_over: GameOver | None = None
    backend_input_closed = False

    try:
        with events_path.open("a", encoding="utf-8") as event_log:

            def record(kind: str, **details: object) -> None:
                event_log.write(json.dumps({"kind": kind, **details}, ensure_ascii=False) + "\n")
                event_log.flush()

            def report_ai_error(ai_id: int, error_type: AiErrorType, detail: str) -> None:
                nonlocal backend_input_closed, round_deadline
                if ai_id in reported_errors or game_over is not None:
                    return
                reported_errors.add(ai_id)
                awaiting_replies.discard(ai_id)
                if not awaiting_replies:
                    round_deadline = None
                try:
                    _write(
                        backend_process.stdin,
                        encode_ai_error(ai_id, current_state, error_type),
                    )
                except (BrokenPipeError, ValueError) as exc:
                    backend_input_closed = True
                    record("backend_input_closed", detail=str(exc))
                    return
                record(
                    "ai_error",
                    player=ai_id,
                    state=current_state,
                    error=error_type.code,
                    error_log=error_type.label,
                    detail=detail,
                )

            def forward_ai_reply(ai_id: int, body: bytes) -> None:
                nonlocal backend_input_closed
                if backend_input_closed:
                    return
                try:
                    content = body.decode("utf-8")
                except UnicodeDecodeError as exc:
                    report_ai_error(ai_id, AiErrorType.RUN_ERROR, str(exc))
                    return
                elapsed_ms = int(1000 * (time.monotonic() - round_begin))
                try:
                    _write(
                        backend_process.stdin,
                        encode_ai_message(ai_id, content, elapsed_ms),
                    )
                except (BrokenPipeError, ValueError) as exc:
                    # A backend may close its input immediately after deciding the match while
                    # its terminal frame is still queued behind AI output. Stop feeding stale
                    # replies, but keep consuming logic events so GameOver remains authoritative.
                    backend_input_closed = True
                    record("backend_input_closed", detail=str(exc))
                    return
                # A listen/state frame can request several operations (SnakeGo).
                # The official judger keeps its state timer armed after replies;
                # only a new listen/state, a terminal frame, or an error ends it.
                # Clearing it here lets an AI answer once and then hang forever.
                record("ai_reply", player=ai_id, size=len(body), time=elapsed_ms)

            _write(
                backend_process.stdin,
                encode_init(active_players, config, replay_path),
            )
            record("send_init", players=len(specs), replay=str(replay_path), config=config)

            while game_over is None:
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    raise TimeoutError(
                        f"match timed out after {timeout_s:.3f}s; "
                        f"returncodes={[process.poll() for process in processes]}"
                    )
                wait_s = remaining
                if round_deadline is not None and awaiting_replies:
                    wait_s = min(wait_s, max(0.0, round_deadline - now))
                if wait_s <= 0 and awaiting_replies:
                    timed_out_ai = next(
                        ai_id for ai_id in listen_targets if ai_id in awaiting_replies
                    )
                    report_ai_error(
                        timed_out_ai,
                        AiErrorType.TIMEOUT_ERROR,
                        f"AI {timed_out_ai} exceeded the {round_time_limit_s:g}s "
                        f"round limit in state {current_state}",
                    )
                    continue
                try:
                    event = event_queue.get(timeout=wait_s)
                except queue.Empty as exc:
                    if (
                        round_deadline is not None
                        and awaiting_replies
                        and time.monotonic() >= round_deadline
                    ):
                        timed_out_ai = next(
                            ai_id for ai_id in listen_targets if ai_id in awaiting_replies
                        )
                        report_ai_error(
                            timed_out_ai,
                            AiErrorType.TIMEOUT_ERROR,
                            f"AI {timed_out_ai} exceeded the {round_time_limit_s:g}s "
                            f"round limit in state {current_state}",
                        )
                        continue
                    raise TimeoutError(
                        f"match stalled; returncodes={[process.poll() for process in processes]}"
                    ) from exc

                kind = event[0]
                if kind == "logic_error":
                    raise SaibloJudgerError(f"logic reader failed: {event[1]}") from event[1]
                if kind == "logic_eof":
                    raise SaibloJudgerError(
                        "logic closed before game over; "
                        f"returncodes={[process.poll() for process in processes]}"
                    )
                if kind == "ai_eof":
                    ai_id = int(event[1])
                    report_ai_error(
                        ai_id,
                        AiErrorType.RUN_ERROR,
                        f"AI {ai_id} exited with returncode={player_processes[ai_id].poll()}",
                    )
                    continue
                if kind == "ai_error":
                    ai_id = int(event[1])
                    report_ai_error(ai_id, AiErrorType.RUN_ERROR, str(event[2]))
                    continue
                if kind == "ai_ole":
                    ai_id, size = int(event[1]), int(event[2])
                    report_ai_error(
                        ai_id,
                        AiErrorType.OUTPUT_LIMIT_ERROR,
                        f"AI {ai_id} declared {size} bytes (limit={UPSTREAM_OUTPUT_LIMIT})",
                    )
                    outcome = player_processes[ai_id].cleanup()
                    if not outcome.clean:
                        raise SandboxInfrastructureError(
                            f"player {ai_id} scope cleanup failed after OLE: {outcome.detail}"
                        )
                    continue
                if kind == "ai_frame":
                    ai_id, body = int(event[1]), event[2]
                    if ai_id in reported_errors:
                        continue
                    if ai_id not in listen_targets:
                        # The upstream implementation has one asyncio loop for logic and sockets,
                        # so a burst of consecutive logic frames is parsed before a response to
                        # the first frame can normally arrive. Separate pipe-reader threads can
                        # invert that order. Hold one state-local burst until the next RoundInfo
                        # establishes its listen set; this is an ordering barrier, not a game
                        # adapter. Initial binary SDK replies may also race the first
                        # RoundInfo after direct initialization frames. Unsolicited
                        # output before any backend input is still ignored.
                        if current_state >= 0 or ai_id in players_with_input:
                            queued = pending_ai.setdefault(ai_id, [])
                            if len(queued) < 16:
                                queued.append(body)
                                record("defer_ai_output", player=ai_id, size=len(body))
                            else:
                                report_ai_error(
                                    ai_id,
                                    AiErrorType.OUTPUT_LIMIT_ERROR,
                                    "too many deferred AI frames",
                                )
                        else:
                            record("unexpected_ai_output", player=ai_id, size=len(body))
                        continue
                    forward_ai_reply(ai_id, body)
                    continue

                if kind != "logic_frame":
                    raise SaibloJudgerError(f"unknown internal event: {kind}")
                target, body = int(event[1]), event[2]
                record("logic_frame", target=target, size=len(body))
                if target != -1:
                    if 0 <= target < len(player_processes):
                        try:
                            _write(player_processes[target].stdin, body)
                            players_with_input.add(target)
                        except (BrokenPipeError, OSError, ValueError) as exc:
                            report_ai_error(target, AiErrorType.RUN_ERROR, str(exc))
                    else:
                        record("invalid_logic_target", target=target)
                    continue

                try:
                    message = parse_logic_message(body)
                except ValueError as exc:
                    raise SaibloJudgerError(f"invalid logic control message: {exc}") from exc
                if isinstance(message, GameOver):
                    game_over = message
                    record("game_over", scores=list(message.scores), message=dict(message.message))
                    break
                if isinstance(message, AuxiliaryMessage):
                    record("auxiliary", message=dict(message.message))
                    continue
                if isinstance(message, RoundConfig):
                    round_time_limit_s = float(message.time)
                    record(
                        "round_config",
                        state=message.state,
                        time=message.time,
                        length=message.length,
                    )
                    continue
                if not isinstance(message, RoundInfo):
                    raise SaibloJudgerError(f"unsupported logic message: {message!r}")
                if current_state != message.state:
                    current_state = message.state
                    round_begin = time.monotonic()
                listen_targets = message.listen
                awaiting_replies = {
                    ai_id for ai_id in message.listen if ai_id not in reported_errors
                }
                round_deadline = (
                    round_begin + round_time_limit_s if awaiting_replies else None
                )
                record(
                    "round",
                    state=message.state,
                    listen=list(message.listen),
                    players=list(message.player),
                )
                for ai_id, content in zip(message.player, message.content, strict=True):
                    if not 0 <= ai_id < len(player_processes):
                        record("invalid_logic_target", target=ai_id)
                        continue
                    if ai_id in reported_errors:
                        continue
                    try:
                        _write(player_processes[ai_id].stdin, content.encode("utf-8"))
                        players_with_input.add(ai_id)
                    except (BrokenPipeError, OSError, ValueError) as exc:
                        report_ai_error(ai_id, AiErrorType.RUN_ERROR, str(exc))
                for ai_id in message.listen:
                    if ai_id in reported_errors:
                        pending_ai.pop(ai_id, None)
                        continue
                    for deferred in pending_ai.pop(ai_id, []):
                        forward_ai_reply(ai_id, deferred)

        try:
            backend_process.wait(timeout=TERMINAL_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
        returncodes = tuple(
            int(process.returncode) if process.poll() is not None else -1 for process in processes
        )
        tails = tuple(bytes(tail).decode("utf-8", errors="replace") for tail in stderr_tails)
        assert game_over is not None
        return JudgerResult(
            end_message=dict(game_over.message),
            scores=game_over.scores,
            player_count=len(specs),
            final_state=current_state,
            process_returncodes=returncodes,
            stderr_tails=tails,
        )
    finally:
        backend_process.cleanup()
        cleanup_failures = _cleanup_players(player_processes, events_path)
        if cleanup_failures:
            raise SandboxInfrastructureError(
                f"player sandbox cleanup failed: {'; '.join(cleanup_failures)}"
            )

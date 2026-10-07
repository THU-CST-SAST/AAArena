from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from aa_arena.saiblo import ProcessSpec, SaibloJudgerError, run_stdio_match
from aa_arena.sandbox import (
    CleanupOutcome,
    DirectLauncher,
    LaunchMetadata,
    SandboxInfrastructureError,
)


def _script(path: Path, source: str) -> ProcessSpec:
    path.write_text(source, encoding="utf-8")
    return ProcessSpec((sys.executable, str(path)), path.parent)


class _ProcessProxy:
    def __init__(
        self,
        process: Any,
        metadata: LaunchMetadata,
        *,
        cleanup_clean: bool,
    ) -> None:
        self._process = process
        self.metadata = metadata
        self._cleanup_clean = cleanup_clean

    def __getattr__(self, name: str) -> Any:
        return getattr(self._process, name)

    def cleanup(self) -> CleanupOutcome:
        underlying = self._process.cleanup()
        if not underlying.clean:
            return underlying
        if not self._cleanup_clean:
            return CleanupOutcome(False, "injected cleanup failure")
        return CleanupOutcome(True)


class RecordingLauncher:
    def __init__(self, *, cleanup_clean: bool = True) -> None:
        self.delegate = DirectLauncher()
        self.cleanup_clean = cleanup_clean
        self.preflight_calls = 0
        self.calls: list[tuple[str, int]] = []

    def preflight(self) -> None:
        self.preflight_calls += 1

    def start(
        self,
        spec: ProcessSpec,
        *,
        match_id: str,
        player_index: int,
    ) -> _ProcessProxy:
        self.calls.append((match_id, player_index))
        process = self.delegate.start(
            spec,
            match_id=match_id,
            player_index=player_index,
        )
        return _ProcessProxy(
            process,
            LaunchMetadata(
                launcher="recording_scope",
                match_id=match_id,
                player_index=player_index,
                unit_name=f"aa-arena-test-p{player_index}.scope",
                control_group=f"/system.slice/aa-arena-test-p{player_index}.scope",
                requested_cpu_quota_percent=100,
                requested_period_usec=100_000,
                observed_cpu_max="100000 100000",
            ),
            cleanup_clean=self.cleanup_clean,
        )


def _run_terminal_match(
    tmp_path: Path,
    *,
    player_launcher: Any | None = None,
):
    backend = _script(
        tmp_path / "terminal_backend.py",
        r'''
import json, struct, sys
n = struct.unpack(">I", sys.stdin.buffer.read(4))[0]
json.loads(sys.stdin.buffer.read(n))
body = json.dumps({"state": -1, "end_info": "{\"0\":1,\"1\":0}"}).encode()
sys.stdout.buffer.write(struct.pack(">Ii", len(body), -1) + body)
sys.stdout.buffer.flush()
''',
    )
    players = (
        _script(tmp_path / "terminal_p0.py", "import sys; sys.stdin.buffer.read()\n"),
        _script(tmp_path / "terminal_p1.py", "import sys; sys.stdin.buffer.read()\n"),
    )
    keyword_arguments = {}
    if player_launcher is not None:
        keyword_arguments["player_launcher"] = player_launcher
    return run_stdio_match(
        backend=backend,
        players=players,
        config={},
        replay_path=tmp_path / "terminal_replay.json",
        events_path=tmp_path / "terminal_events.jsonl",
        timeout_s=5,
        **keyword_arguments,
    )


def test_judger_uses_one_injected_launch_per_player_and_records_policy(
    tmp_path: Path,
) -> None:
    launcher = RecordingLauncher()

    result = _run_terminal_match(tmp_path, player_launcher=launcher)

    assert result.player_count == 2
    assert launcher.preflight_calls == 1
    assert launcher.calls == [(tmp_path.name, 0), (tmp_path.name, 1)]
    events = [
        json.loads(line)
        for line in (tmp_path / "terminal_events.jsonl").read_text().splitlines()
    ]
    launches = [event for event in events if event["kind"] == "sandbox_launch"]
    cleanups = [event for event in events if event["kind"] == "sandbox_cleanup"]
    assert len(launches) == 2
    assert {event["observed_cpu_max"] for event in launches} == {"100000 100000"}
    assert len(cleanups) == 2
    assert all(event["clean"] is True for event in cleanups)


def test_cleanup_failure_overrides_a_completed_result(tmp_path: Path) -> None:
    with pytest.raises(SandboxInfrastructureError, match="injected cleanup failure"):
        _run_terminal_match(
            tmp_path,
            player_launcher=RecordingLauncher(cleanup_clean=False),
        )


def test_omitted_launcher_uses_strict_systemd_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aa_arena.saiblo import judger

    launcher = RecordingLauncher()
    monkeypatch.setattr(judger, "SystemdScopeLauncher", lambda: launcher)

    _run_terminal_match(tmp_path)

    assert launcher.calls == [(tmp_path.name, 0), (tmp_path.name, 1)]


def test_shared_judger_routes_direct_and_round_messages(tmp_path: Path) -> None:
    backend = _script(
        tmp_path / "backend.py",
        r'''
import json, struct, sys

def exact(n):
    data = b""
    while len(data) < n:
        chunk = sys.stdin.buffer.read(n - len(data))
        if not chunk: raise EOFError
        data += chunk
    return data

def read_json():
    return json.loads(exact(struct.unpack(">I", exact(4))[0]))

def send(target, value):
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    sys.stdout.buffer.write(struct.pack(">Ii", len(body), target) + body)
    sys.stdout.buffer.flush()

init = read_json()
assert init["player_num"] == 2
assert init["config"] == {"random_seed": 9}
send(0, b"D")
send(-1, {"state": 4, "listen": [0], "player": [0], "content": ["R"]})
reply = read_json()
assert reply["player"] == 0
assert reply["content"] == "ok"
assert isinstance(reply["time"], int)
send(-1, {"state": -1, "end_info": "{\"0\":1,\"1\":0}"})
''',
    )
    player0 = _script(
        tmp_path / "player0.py",
        r'''
import struct, sys
assert sys.stdin.buffer.read(2) == b"DR"
body = b"ok"
sys.stdout.buffer.write(struct.pack(">I", len(body)) + body)
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
''',
    )
    player1 = _script(tmp_path / "player1.py", "import sys; sys.stdin.buffer.read()\n")

    result = run_stdio_match(
        backend=backend,
        players=(player0, player1),
        config={"random_seed": 9},
        replay_path=tmp_path / "replay.json",
        events_path=tmp_path / "events.jsonl",
        timeout_s=5,
        player_launcher=DirectLauncher(),
    )

    assert result.end_message["state"] == -1
    assert json.loads(result.end_message["end_info"]) == {"0": 1, "1": 0}


def test_player_runtime_error_is_reported_to_backend_and_match_finishes(tmp_path: Path) -> None:
    backend = _script(
        tmp_path / "backend_re.py",
        r'''
import json, struct, sys

def exact(n):
    data = b""
    while len(data) < n:
        chunk = sys.stdin.buffer.read(n - len(data))
        if not chunk: raise EOFError
        data += chunk
    return data

def read_json():
    return json.loads(exact(struct.unpack(">I", exact(4))[0]))

def send(value):
    body = json.dumps(value).encode()
    sys.stdout.buffer.write(struct.pack(">Ii", len(body), -1) + body)
    sys.stdout.buffer.flush()

read_json()
send({"state": 1, "listen": [0], "player": [0], "content": ["go\n"]})
outer = read_json()
assert outer["player"] == -1
error = json.loads(outer["content"])
assert error == {"player": 0, "state": 1, "error": 0, "error_log": "runError"}
send({"state": -1, "end_info": "{\"0\":0,\"1\":1}"})
''',
    )
    failed_player = _script(
        tmp_path / "failed.py",
        "import sys; sys.stdin.buffer.readline(); print('boom', file=sys.stderr); raise SystemExit(17)\n",
    )
    survivor = _script(tmp_path / "survivor.py", "import sys; sys.stdin.buffer.read()\n")

    result = run_stdio_match(
        backend=backend,
        players=(failed_player, survivor),
        config={},
        replay_path=tmp_path / "replay.json",
        events_path=tmp_path / "events.jsonl",
        timeout_s=5,
        player_launcher=DirectLauncher(),
    )

    assert json.loads(result.end_message["end_info"]) == {"0": 0, "1": 1}
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert any(event["kind"] == "ai_error" and event["player"] == 0 for event in events)


def test_round_timeout_is_reported_to_backend_and_match_finishes(tmp_path: Path) -> None:
    backend = _script(
        tmp_path / "backend_tle.py",
        r'''
import json, struct, sys

def exact(n):
    data = b""
    while len(data) < n:
        chunk = sys.stdin.buffer.read(n - len(data))
        if not chunk: raise EOFError
        data += chunk
    return data

def read_json():
    return json.loads(exact(struct.unpack(">I", exact(4))[0]))

def send(value):
    body = json.dumps(value).encode()
    sys.stdout.buffer.write(struct.pack(">Ii", len(body), -1) + body)
    sys.stdout.buffer.flush()

read_json()
send({"state": 0, "time": 1, "length": 2048})
send({"state": 1, "listen": [0], "player": [0], "content": ["go\n"]})
outer = read_json()
assert outer["player"] == -1
error = json.loads(outer["content"])
assert error == {"player": 0, "state": 1, "error": 1, "error_log": "timeOutError"}
send({"state": -1, "end_info": "{\"0\":0,\"1\":1}"})
''',
    )
    stalled_player = _script(
        tmp_path / "stalled.py",
        "import sys, time; sys.stdin.buffer.readline(); time.sleep(10)\n",
    )
    survivor = _script(tmp_path / "survivor.py", "import sys; sys.stdin.buffer.read()\n")

    result = run_stdio_match(
        backend=backend,
        players=(stalled_player, survivor),
        config={},
        replay_path=tmp_path / "replay.json",
        events_path=tmp_path / "events.jsonl",
        timeout_s=1.5,
        player_launcher=DirectLauncher(),
    )

    assert json.loads(result.end_message["end_info"]) == {"0": 0, "1": 1}
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert any(
        event["kind"] == "ai_error"
        and event["player"] == 0
        and event["error_log"] == "timeOutError"
        for event in events
    )


@pytest.mark.parametrize("initial_binary_input", (False, True))
def test_pipe_scheduler_preserves_reply_that_races_with_next_listen(tmp_path: Path, initial_binary_input: bool) -> None:
    backend = _script(
        tmp_path / "backend_race.py",
        r'''
import json, struct, sys, time

def exact(n):
    data = b""
    while len(data) < n:
        chunk = sys.stdin.buffer.read(n - len(data))
        if not chunk: raise EOFError
        data += chunk
    return data

def read_json():
    return json.loads(exact(struct.unpack(">I", exact(4))[0]))

def send(value):
    body = json.dumps(value).encode()
    sys.stdout.buffer.write(struct.pack(">Ii", len(body), -1) + body)
    sys.stdout.buffer.flush()

read_json()
# The AI reacts to this broadcast immediately. The next listen is deliberately delayed so the
# pipe reader observes the AI frame first, unlike the official single asyncio loop.
send({"state": 1, "listen": [], "player": [0], "content": ["act\n"]})
time.sleep(0.05)
send({"state": 2, "listen": [0], "player": [], "content": []})
reply = read_json()
assert reply["player"] == 0 and reply["content"] == "ready"
send({"state": -1, "end_info": "{\"0\":1,\"1\":0}"})
''',
    )
    if initial_binary_input:
        path = tmp_path / "backend_race.py"
        path.write_text(path.read_text().replace(
            'send({"state": 1, "listen": [], "player": [0], "content": ["act\\n"]})',
            'sys.stdout.buffer.write(struct.pack(">Ii", 4, 0) + b"act\\n"); sys.stdout.buffer.flush()'))
    eager_player = _script(
        tmp_path / "eager.py",
        r'''
import struct, sys
sys.stdin.buffer.readline()
body = b"ready"
sys.stdout.buffer.write(struct.pack(">I", len(body)) + body)
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
''',
    )
    idle_player = _script(tmp_path / "idle.py", "import sys; sys.stdin.buffer.read()\n")

    result = run_stdio_match(
        backend=backend,
        players=(eager_player, idle_player),
        config={},
        replay_path=tmp_path / "replay.json",
        events_path=tmp_path / "events.jsonl",
        timeout_s=2,
        player_launcher=DirectLauncher(),
    )

    assert result.end_message["state"] == -1


def test_backend_terminal_frame_wins_over_queued_ai_output(tmp_path: Path) -> None:
    """A backend-closing race must return its terminal result, not leak EPIPE."""
    backend = _script(
        tmp_path / "backend_terminal_race.py",
        r'''
import json, struct, sys

def exact(n):
    data = b""
    while len(data) < n:
        chunk = sys.stdin.buffer.read(n - len(data))
        if not chunk: raise EOFError
        data += chunk
    return data

def read_json():
    return json.loads(exact(struct.unpack(">I", exact(4))[0]))

def send(value):
    body = json.dumps(value).encode()
    sys.stdout.buffer.write(struct.pack(">Ii", len(body), -1) + body)
    sys.stdout.buffer.flush()

read_json()
send({"state": 1, "listen": [0], "player": [0], "content": ["go\n"]})
read_json()
send({"state": -1, "end_info": "{\"0\":0,\"1\":1}"})
''',
    )
    flooding_player = _script(
        tmp_path / "flooding.py",
        r'''
import struct, sys
sys.stdin.buffer.readline()
frame = struct.pack(">I", 1) + b"x"
sys.stdout.buffer.write(frame * 10000)
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
''',
    )
    idle_player = _script(tmp_path / "idle.py", "import sys; sys.stdin.buffer.read()\n")

    result = run_stdio_match(
        backend=backend,
        players=(flooding_player, idle_player),
        config={},
        replay_path=tmp_path / "replay.json",
        events_path=tmp_path / "events.jsonl",
        timeout_s=5,
        player_launcher=DirectLauncher(),
    )

    assert json.loads(result.end_message["end_info"]) == {"0": 0, "1": 1}
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert any(event["kind"] == "backend_input_closed" for event in events)


def test_backend_eof_without_terminal_frame_remains_an_error(tmp_path: Path) -> None:
    backend = _script(
        tmp_path / "backend_crash.py",
        r'''
import struct, sys
n = struct.unpack(">I", sys.stdin.buffer.read(4))[0]
sys.stdin.buffer.read(n)
''',
    )
    players = tuple(
        _script(tmp_path / f"crash_p{index}.py", "import sys; sys.stdin.buffer.read()\n")
        for index in range(2)
    )

    with pytest.raises(SaibloJudgerError, match="logic closed before game over"):
        run_stdio_match(
            backend=backend,
            players=players,
            config={},
            replay_path=tmp_path / "crash_replay.json",
            events_path=tmp_path / "crash_events.jsonl",
            timeout_s=5,
            player_launcher=DirectLauncher(),
        )


def test_shared_judger_accepts_arbitrary_player_count(tmp_path: Path) -> None:
    backend = _script(
        tmp_path / "backend_n.py",
        r'''
import json, struct, sys
n = struct.unpack(">I", sys.stdin.buffer.read(4))[0]
init = json.loads(sys.stdin.buffer.read(n))
assert init["player_num"] == 4
body = json.dumps({"state": -1, "end_info": "{\"0\":4,\"1\":3,\"2\":2,\"3\":1}"}).encode()
sys.stdout.buffer.write(struct.pack(">Ii", len(body), -1) + body)
sys.stdout.buffer.flush()
''',
    )
    players = tuple(
        _script(tmp_path / f"p{index}.py", "import sys; sys.stdin.buffer.read()\n")
        for index in range(4)
    )

    result = run_stdio_match(
        backend=backend,
        players=players,
        config={},
        replay_path=tmp_path / "replay.json",
        events_path=tmp_path / "events.jsonl",
        timeout_s=5,
        player_launcher=DirectLauncher(),
    )


    assert result.player_count == 4


@pytest.mark.parametrize("replies", (1, 3))
def test_round_timeout_survives_partial_multi_operation_reply(tmp_path: Path, replies: int) -> None:
    """SnakeGo requests several operations under a single listen/state frame."""
    backend = _script(tmp_path / "multi_backend.py", r'''
import json, struct, sys
def read():
    n = struct.unpack(">I", sys.stdin.buffer.read(4))[0]
    return json.loads(sys.stdin.buffer.read(n))
def send(target, value):
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    sys.stdout.buffer.write(struct.pack(">Ii", len(body), target) + body)
    sys.stdout.buffer.flush()
read()
send(-1, {"state": 0, "time": 1, "length": 2048})
send(-1, {"state": 7, "listen": [0], "player": [0], "content": ["go\n"]})
for _ in range(REPLIES):
    reply = read()
    assert reply["player"] == 0 and reply["content"] == "ok"
    send(0, b"next\n")
error = read()
assert error["player"] == -1
detail = json.loads(error["content"])
assert detail["player"] == 0 and detail["state"] == 7 and detail["error"] == 1
send(-1, {"state": -1, "end_info": "{\"0\":0,\"1\":1}"})
'''.replace("REPLIES", str(replies)))
    player = _script(tmp_path / "multi_player.py", r'''
import struct, sys
for _ in range(REPLIES):
    sys.stdin.buffer.readline()
    sys.stdout.buffer.write(struct.pack(">I", 2) + b"ok")
    sys.stdout.buffer.flush()
sys.stdin.buffer.read()
'''.replace("REPLIES", str(replies)))
    result = run_stdio_match(
        backend=backend, players=(player,), config={},
        replay_path=tmp_path / "multi_replay.json",
        events_path=tmp_path / "multi_events.jsonl",
        timeout_s=2, player_launcher=DirectLauncher(),
    )
    assert json.loads(result.end_message["end_info"]) == {"0": 0, "1": 1}
    events = [json.loads(line) for line in (tmp_path / "multi_events.jsonl").read_text().splitlines()]
    assert sum(e["kind"] == "ai_reply" for e in events) == replies
    assert sum(e["kind"] == "ai_error" for e in events) == 1

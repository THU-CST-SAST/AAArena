from __future__ import annotations

from types import SimpleNamespace

from aa_arena.benchmark.runtime import CodexArenaRuntime, GOAL_TEMPLATE, _normalized_usage
from aa_arena.resources import ARENA_GAMES


class _Ledger:
    def __init__(self) -> None:
        self.usage: dict[str, int] = {}

    def update_runtime(self, *, token_usage: dict[str, int]) -> None:
        self.usage = token_usage


class _CompactClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.messages = [
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "tokenUsage": {
                        "total": {"inputTokens": 100, "totalTokens": 120},
                        "last": {"inputTokens": 40, "totalTokens": 45},
                    }
                },
            },
            {
                "method": "item/completed",
                "params": {"item": {"type": "contextCompaction"}},
            },
        ]

    def request(self, method: str, params: dict[str, str]) -> dict[str, object]:
        self.requests.append((method, params))
        return {}

    def next_message(self, timeout: float) -> dict[str, object]:
        assert timeout > 0
        return self.messages.pop(0)


def test_normalized_usage_restores_numeric_ledger_values() -> None:
    assert _normalized_usage({"total_tokens": 123.0, "input_tokens": 100, "bad": "7"}) == {
        "total_tokens": 123,
        "input_tokens": 100,
    }
    assert _normalized_usage(None) == {}


def test_all_eight_games_receive_the_same_replay_learning_goal() -> None:
    required = (
        "replay.json",
        "replay.md",
        "蒸馏/模仿",
        "从规则推导",
        "增量/事件型回放",
        "JSON 与 narrate 不一致",
    )
    for game in ARENA_GAMES:
        objective = GOAL_TEMPLATE.format(game=game, small_budget=128, large_budget=16)
        assert all(fragment in objective for fragment in required)


def test_native_compact_updates_usage_and_keeps_same_thread() -> None:
    ledger = _Ledger()
    runtime = object.__new__(CodexArenaRuntime)
    runtime.client = _CompactClient()
    runtime.thread_id = "persistent-thread"
    runtime.service = SimpleNamespace(ledger=ledger)
    runtime.token_usage = {}
    runtime._diagnostic = lambda _message: None

    runtime._native_compact(timeout_s=1)

    assert runtime.client.requests == [
        ("thread/compact/start", {"threadId": "persistent-thread"})
    ]
    assert ledger.usage["total_tokens"] == 120
    assert ledger.usage["last_input_tokens"] == 40


class _RunLedger:
    def __init__(self) -> None:
        self.status = "running"

    def budgets(self):
        return SimpleNamespace(
            small_total=128,
            small_used=0,
            small_remaining=128,
            large_total=16,
            large_used=0,
            large_remaining=16,
        )

    def state(self) -> dict[str, object]:
        return {"status": self.status, "thread_id": None}

    def update_runtime(self, **_values: object) -> None:
        return None

    def undelivered_results(self) -> list[object]:
        return []

    def begin_final_review(self) -> bool:
        return False

    def mark_complete(self) -> None:
        self.status = "complete"


def test_run_never_forces_compaction_at_large_phase_boundaries() -> None:
    ledger = _RunLedger()
    restored = []
    runtime = object.__new__(CodexArenaRuntime)
    runtime.client = object()
    runtime.service = SimpleNamespace(
        ledger=ledger,
        game="antwar",
        restore_frozen_strategy=lambda: restored.append(True),
    )
    runtime.thread_id = None
    runtime._compact_after_turn = True
    runtime._start_thread = lambda *_args, **_kwargs: "persistent-thread"
    runtime._freeze_workspace = lambda: None
    runtime._native_compact = lambda: (_ for _ in ()).throw(
        AssertionError("formal run forced an early compact")
    )
    runtime._run_turn = lambda *_args, **_kwargs: setattr(ledger, "status", "finalizing")

    runtime.run()

    assert ledger.status == "complete"
    assert restored == [True]


def test_production_dispatch_preserves_nonmatch_results_and_match_budget(tmp_path, monkeypatch):
    import json
    from aa_arena.benchmark.service import BenchmarkService
    monkeypatch.setattr('aa_arena.benchmark.service.rebuild_report', lambda _: None)
    class Client:
        def respond(self, _id, response):
            self.response = response
    with BenchmarkService(tmp_path / 'run', game='antwar', model_profile='test-model-a',
                          small_budget=2, large_budget=1, workers=1) as service:
        service.initialize_workspace()
        runtime = object.__new__(CodexArenaRuntime)
        runtime.service, runtime.client = service, Client()
        runtime._diagnostic = lambda _: None
        monkeypatch.setattr(service, '_preflight', lambda: None)
        monkeypatch.setattr(service, '_execute_pending', lambda pending, snapshot: {
            'kind': pending['kind'], 'rank': 10, 'elo': 1200., 'wins': 1, 'losses': 1,
            'candidate_errors': 0})
        opponent = service.matches.opponents[0].opponent_id
        for name, arguments in [('workspace_manifest', {}), ('list_opponents', {'limit': 2}),
                                ('workspace_shell', {'command': 'pwd'}),
                                ('small_match', {'opponent_ids': [opponent]}), ('large_match', {})]:
            response = runtime._dispatch_tool({'id': 1, 'params': {'tool': name, 'arguments': arguments}})
            assert response['success'], response
            result = json.loads(response['contentItems'][0]['text'])
            if name == 'workspace_shell':
                assert result['exit_code'] == 0 and '/workspace' in result['output']
            if name.endswith('_match'):
                assert (service.workspace / result['raw_result_path']).is_file()
        assert service.ledger.budgets().small_used == 1
        assert service.ledger.budgets().large_used == 1
        assert all(s['delivered_at'] for s in service.ledger.submissions())


def test_repeated_tool_fault_stops_and_doctor_uses_real_dispatch():
    import pytest
    from aa_arena.benchmark.runtime import ToolInfrastructureError
    runtime = object.__new__(CodexArenaRuntime)
    runtime._diagnostic = lambda _: None
    runtime.client = SimpleNamespace(respond=lambda *_: None)
    runtime.service = SimpleNamespace(model_tool_call=lambda *_: (_ for _ in ()).throw(ValueError('broken result')))
    msg = {'id': 1, 'params': {'tool': 'workspace_manifest', 'arguments': {}}}
    assert not runtime._dispatch_tool(msg)['success']
    assert not runtime._dispatch_tool(msg)['success']
    with pytest.raises(ToolInfrastructureError):
        runtime._dispatch_tool(msg)
    with pytest.raises(ToolInfrastructureError):
        runtime._dispatch_tool(msg, probe=True)


def test_no_progress_turns_are_bounded():
    import pytest
    from aa_arena.benchmark.runtime import ToolInfrastructureError
    runtime = object.__new__(CodexArenaRuntime)
    ledger = _RunLedger()
    runtime.client = object()
    runtime.service = SimpleNamespace(ledger=ledger, game="antwar")
    runtime._start_thread = lambda *_args, **_kwargs: "same-thread"
    turns = []
    runtime._run_turn = lambda prompt: turns.append(prompt)
    with pytest.raises(ToolInfrastructureError, match="eight completed turns"):
        runtime.run()
    assert len(turns) == 8


def test_doctor_exception_never_reaches_formal_thread_permissions():
    class Client:
        def __init__(self):
            self.requests = []
        def request(self, method, params):
            self.requests.append((method, params))
            return {"thread": {"id": "test-thread"}} if method == "thread/start" else {}
    runtime = object.__new__(CodexArenaRuntime)
    runtime.client = Client()
    runtime.profile = SimpleNamespace(model="test", model_provider="test")
    runtime._start_thread("formal", tools=[])
    runtime._start_thread("doctor", tools=[], doctor=True)
    formal, doctor = [p for method, p in runtime.client.requests if method == "thread/start"]
    assert formal['developerInstructions'].startswith('Never attempt network access')
    assert 'doctor-network-probe.py' not in formal['developerInstructions']
    assert 'sole authorized isolation test' in doctor['developerInstructions']
    for key in ('permissions', 'approvalPolicy', 'runtimeWorkspaceRoots'):
        assert formal[key] == doctor[key]

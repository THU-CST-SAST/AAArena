from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from aa_arena.benchmark.service import BenchmarkService


def test_match_results_expose_budget_and_verified_snapshot_chain(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("aa_arena.benchmark.service.rebuild_report", lambda _: None)
    run = tmp_path / "run"
    with BenchmarkService(
        run,
        game="antwar",
        model_profile="test-model-a",
        small_budget=2,
        large_budget=1,
        workers=1,
    ) as service:
        service.initialize_workspace()
        monkeypatch.setattr(service, "_preflight", lambda: None)

        def fake_execute(pending, snapshot):
            if pending["kind"] == "small":
                return {"kind": "small", "wins": 1, "draws": 0, "losses": 1}
            return {
                "kind": "large",
                "elo": 1000.0,
                "elo_ci_low": 900.0,
                "elo_ci_high": 1100.0,
                "rank": 10,
                "pool_win_rate": 0.5,
                "wins": 1,
                "draws": 0,
                "losses": 1,
                "candidate_errors": 0,
            }

        monkeypatch.setattr(service, "_execute_pending", fake_execute)
        opponent_id = service.matches.opponents[0].opponent_id
        before = service.tool_call("budget_status", {})
        assert before["budget"] == {
            "small_total": 2,
            "small_used": 0,
            "small_remaining": 2,
            "large_total": 1,
            "large_used": 0,
            "large_remaining": 1,
        }

        second_opponent_id = service.matches.opponents[1].opponent_id
        small = service.tool_call(
            "small_match", {"opponent_ids": [opponent_id, second_opponent_id]}
        )
        assert small["budget"]["small_remaining"] == 0
        first = service.snapshots.verify(small["snapshot_id"])
        assert small["snapshot_integrity"]["archive_sha256"] == first.archive_sha256

        strategy_file = next((service.workspace / "strategy").rglob("main.py"))
        strategy_file.write_text(
            strategy_file.read_text(encoding="utf-8") + "\n# checkpoint two\n",
            encoding="utf-8",
        )
        large = service.tool_call("large_match", {})
        assert large["budget"]["large_remaining"] == 0
        second = service.snapshots.verify(large["snapshot_id"])
        assert second.parent_snapshot_id == first.snapshot_id
        assert second.strategy_hash != first.strategy_hash
        assert second.changed_files == 1

        events = service.trajectory.read()
        assert [event["snapshot"]["snapshot_id"] for event in events] == [
            first.snapshot_id,
            second.snapshot_id,
        ]
        assert all(event["snapshot"]["manifest_sha256"] for event in events)
        final = service.tool_call("budget_status", {})
        assert final["run_status"] == "finalizing"
        assert final["official_strategy_frozen"] is True
        assert final["budget"]["small_remaining"] == 0


def test_public_workspace_manifest_and_opponent_list_are_no_cost(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("aa_arena.benchmark.service.rebuild_report", lambda _: None)
    with BenchmarkService(
        tmp_path / "run",
        game="antwar",
        model_profile="test-model-a",
        small_budget=2,
        large_budget=1,
        workers=1,
    ) as service:
        service.initialize_workspace()
        before = service.ledger.budgets().as_dict()
        manifest = service.tool_call("workspace_manifest", {})
        opponents = service.tool_call(
            "list_opponents", {"rank_min": 1, "rank_max": 5, "limit": 5}
        )
        assert "resources/replay/reading_skill.md" in manifest["read_only"]
        assert manifest["replay_contract"]["raw_saiblo_replay_visible"] is False
        assert len(opponents["opponents"]) == 5
        assert opponents["opponents"][0]["rank"] == 1
        assert service.ledger.budgets().as_dict() == before


def test_execute_pending_removes_transient_storage_after_success(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("aa_arena.benchmark.service.rebuild_report", lambda _: None)
    with BenchmarkService(
        tmp_path / "run",
        game="antwar",
        model_profile="test-model-a",
        small_budget=0,
        large_budget=1,
        workers=1,
    ) as service:
        service.initialize_workspace()
        snapshot = service._snapshot()
        submission_id = "storage-success"

        def fake_large(strategy: Path, observed_id: str) -> dict[str, object]:
            assert observed_id == submission_id
            assert strategy.is_dir()
            raw = service.matches.hidden_root / submission_id / "raw.bin"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"large transient payload")
            return {"kind": "large"}

        monkeypatch.setattr(service.matches, "large_match", fake_large)
        result = service._execute_pending(
            {"submission_id": submission_id, "kind": "large", "request": {}},
            snapshot,
        )

        assert result == {"kind": "large"}
        assert not (service.controller / "evaluation" / submission_id).exists()
        assert not (service.matches.hidden_root / submission_id).exists()


def test_execute_pending_removes_transient_storage_after_failure(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("aa_arena.benchmark.service.rebuild_report", lambda _: None)
    with BenchmarkService(
        tmp_path / "run",
        game="antwar",
        model_profile="test-model-a",
        small_budget=0,
        large_budget=1,
        workers=1,
    ) as service:
        service.initialize_workspace()
        snapshot = service._snapshot()
        submission_id = "storage-failure"

        def fake_large(strategy: Path, observed_id: str) -> dict[str, object]:
            assert strategy.is_dir()
            raw = service.matches.hidden_root / observed_id / "raw.bin"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"failed transient payload")
            raise RuntimeError("synthetic evaluation failure")

        monkeypatch.setattr(service.matches, "large_match", fake_large)
        with pytest.raises(RuntimeError, match="synthetic evaluation failure"):
            service._execute_pending(
                {"submission_id": submission_id, "kind": "large", "request": {}},
                snapshot,
            )

        assert not (service.controller / "evaluation" / submission_id).exists()
        assert not (service.matches.hidden_root / submission_id).exists()


def test_match_is_rejected_before_budget_reservation_when_disk_is_low(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("aa_arena.benchmark.service.rebuild_report", lambda _: None)
    monkeypatch.setattr(
        "aa_arena.benchmark.service.shutil.disk_usage",
        lambda _: SimpleNamespace(total=100, used=99, free=8 * 1024**3 - 1),
    )
    with BenchmarkService(
        tmp_path / "run",
        game="antwar",
        model_profile="test-model-a",
        small_budget=0,
        large_budget=1,
        workers=1,
    ) as service:
        service.initialize_workspace()
        monkeypatch.setattr(service.matches, "preflight_candidate", lambda _: None)
        monkeypatch.setattr(service, "_execute_pending", lambda *_: {"kind": "large"})

        with pytest.raises(RuntimeError, match="requires at least 8 GiB free"):
            service.large_match()

        assert service.ledger.budgets().large_used == 0


def test_service_extension_reuses_workspace_session_and_only_one_extra_budget(tmp_path, monkeypatch):
    monkeypatch.setattr("aa_arena.benchmark.service.rebuild_report", lambda _: None)
    run = tmp_path / "extension-run"
    with BenchmarkService(run, game="antwar", model_profile="test-model-a", small_budget=2, large_budget=1, workers=1) as service:
        service.initialize_workspace()
        (service.workspace / "notes" / "learned.md").write_text("keep this learning")
        service.ledger.update_runtime(thread_id="persistent", token_usage={"total_tokens": 123})
        monkeypatch.setattr(service, "_preflight", lambda: None)
        monkeypatch.setattr(service, "_execute_pending", lambda *_: {"kind": "large", "rank": 15, "elo": 1200, "wins": 1, "losses": 1})
        result = service.model_tool_call("large_match", {})
        service.restore_frozen_strategy()
        service.ledger.begin_final_review()
        service.ledger.mark_complete()
        champion = service.ledger.state()["metadata"]["champion"]
    for _ in range(2):
        with BenchmarkService(run, game="antwar", model_profile="test-model-a", small_budget=2, large_budget=1, workers=1, extend_budget_once=True) as service:
            service.initialize_workspace()
            assert service.ledger.state()["thread_id"] == "persistent"
            assert service.ledger.state()["metadata"]["champion"] == champion
            assert service.ledger.budgets().large_used == 1
            assert service.ledger.budgets().large_total == 2
            assert service.ledger.budgets().small_remaining == 2
            assert len(service.ledger.submissions()) == 1
            assert (service.workspace / "notes" / "learned.md").read_text() == "keep this learning"
            assert (service.workspace / result["raw_result_path"]).exists()


def test_model_replay_paths_are_readable_inside_workspace(tmp_path):
    import json
    with BenchmarkService(tmp_path/'run', game='antwar', model_profile='test-model-a', workers=1) as service:
        service.initialize_workspace()
        replay = service.workspace / 'replays' / 'one.json'
        replay.write_text('{"rounds": [{"round": 1}]}')
        result = service.agent_result({'kind': 'small', 'match_id': 'one', 'seats': [
            {'outcome': 'loss', 'replay_path': str(replay)}]})
        assert result['priority_replay_paths'] == ['replays/one.json']
        raw = json.loads((service.workspace/result['raw_result_path']).read_text())
        assert raw['seats'][0]['replay_path'] == 'replays/one.json'
        opened = service.model_tool_call('workspace_shell', {'command': 'cat replays/one.json'})
        assert opened['exit_code'] == 0
        assert json.loads(opened['output'])['rounds'][0]['round'] == 1


def test_champion_comparison_keeps_zero_elo():
    assert BenchmarkService._official_better({'rank': 20, 'elo': 0}, {'rank': 20, 'elo': -1})
    assert not BenchmarkService._official_better({'rank': 20, 'elo': -1}, {'rank': 20, 'elo': 0})


def test_workspace_shell_accepts_source_literals_and_preserves_isolation(tmp_path):
    import socket
    with BenchmarkService(tmp_path/'run', game='lostspace', model_profile='test-model-a', workers=1) as service:
        service.initialize_workspace()
        code = "values: tuple[int, ...] = (1,)\nmask = ~0\nnote = '/root/ /home/ ../ curl wget'\n"
        result = service.model_tool_call('workspace_shell', {'command': "cat > strategy/ellipsis.py <<'SRC'\n" + code + "SRC\npython3 -m py_compile strategy/ellipsis.py"})
        assert result['exit_code'] == 0, result
        assert (service.workspace/'strategy/ellipsis.py').read_text() == code
        # A private host sentinel stays invisible via absolute, traversal and symlink paths.
        sentinel = service.controller/'hidden-sentinel'
        sentinel.write_text('private-proof-value')
        for command in (f'cat {sentinel}', 'cat ../controller/hidden-sentinel', f'ln -s {sentinel} strategy/link; cat strategy/link'):
            denied = service.model_tool_call('workspace_shell', {'command': command})
            assert denied['exit_code'] != 0
            assert 'private-proof-value' not in denied['output']
        rules = service.workspace/'resources/rules.md'
        original = rules.read_bytes()
        denied = service.model_tool_call('workspace_shell', {'command': 'chmod u+w resources/rules.md && echo changed > resources/rules.md'})
        assert denied['exit_code'] != 0
        assert rules.read_bytes() == original
        with socket.socket() as host_listener:
            host_listener.bind(('127.0.0.1', 0))
            host_listener.listen()
            port = host_listener.getsockname()[1]
            denied = service.model_tool_call('workspace_shell', {'command': f"python3 -c 'import socket; socket.create_connection((\"127.0.0.1\", {port}), timeout=1)'"})
            assert denied['exit_code'] != 0


def test_workspace_shell_caps_utf8_bytes_and_refuses_redirected_write_root(tmp_path):
    with BenchmarkService(tmp_path/'run', game='antwar', model_profile='test-model-a', workers=1) as service:
        service.initialize_workspace()
        result = service.model_tool_call('workspace_shell', {'command': "python3 -c 'print(\"回\" * 20000)'"})
        assert result['exit_code'] == 0
        assert result['truncated']
        assert len(result['output'].encode('utf-8')) <= 32768
        notes = service.workspace/'notes'
        notes.rmdir()
        notes.symlink_to(service.controller, target_is_directory=True)
        with pytest.raises(ValueError, match='real directories'):
            service.model_tool_call('workspace_shell', {'command': 'ls notes'})

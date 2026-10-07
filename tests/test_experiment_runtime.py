from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aa_arena.benchmark.experiment import ExperimentConfig
from aa_arena.benchmark.ledger import BudgetError, RunLedger
from aa_arena.benchmark.runtime import (
    CodexArenaRuntime,
    DYNAMIC_TOOLS,
    GOAL_TEMPLATE,
    TOKEN_EFFICIENCY_INSTRUCTIONS,
    experiment_tools,
)
from aa_arena.benchmark.service import BenchmarkService
from aa_arena.benchmark.snapshot import SnapshotStore


class Client:
    def __init__(self):
        self.requests = []

    def request(self, method, params):
        self.requests.append((method, params))
        return {"thread": {"id": params.get("threadId", "persistent-trial")}}


def runtime_for(service):
    runtime = object.__new__(CodexArenaRuntime)
    runtime.service = service
    runtime.client = Client()
    runtime.profile = SimpleNamespace(model="test", model_provider="test", reasoning_effort="low")
    runtime.thread_id = None
    return runtime


def prompt_runtime(experiment=ExperimentConfig()):
    return runtime_for(SimpleNamespace(
        game="antwar", experiment=experiment,
        ledger=SimpleNamespace(
            budgets=lambda: SimpleNamespace(small_total=32 if experiment.is_clone else 128,
                                             large_total=0 if experiment.is_clone else 16),
            state=lambda: {"metadata": {}},
        ),
    ))


def test_default_goal_and_tools_are_unchanged():
    runtime = prompt_runtime()
    assert runtime._objective() == GOAL_TEMPLATE.format(
        game="antwar", small_budget=128, large_budget=16
    ) + "\n\n" + TOKEN_EFFICIENCY_INSTRUCTIONS
    assert experiment_tools(ExperimentConfig()) == DYNAMIC_TOOLS
    copied = experiment_tools(ExperimentConfig())
    copied[0]["inputSchema"]["properties"].clear()
    assert DYNAMIC_TOOLS[0]["inputSchema"]["properties"]


@pytest.mark.parametrize("policy", ["model", "ladder", "random", "top5", "clone"])
def test_binary_goals_and_small_tools_do_not_request_hidden_evidence(policy):
    config = ExperimentConfig(opponent_policy=policy, feedback="binary",
                              clone_rank=5 if policy == "clone" else None)
    runtime = prompt_runtime(config)
    objective = runtime._objective()
    tools = experiment_tools(config)
    small = next(tool for tool in tools if tool["name"] == "small_match")
    manifest = next(tool for tool in tools if tool["name"] == "workspace_manifest")
    content = objective + json.dumps([small, manifest], ensure_ascii=False)
    for forbidden in ("replay.json", "replay.md", "replay-reading", "narrate", "raw evidence",
                      "Raw evidence", "阅读指南", "数值变化", "重建状态", "raw_result_path"):
        assert forbidden not in content
    assert "win" in content and "false" in content
    public_contract = {key: value for key, value in config.public_dict().items() if key != "fixed_small_batch" or value is not None}
    assert json.dumps(public_contract, ensure_ascii=False, sort_keys=True) in objective
    assert '"seed"' not in objective and str(config.seed) not in objective


def test_ladder_schema_allows_repeats_and_describes_numeric_progression():
    config = ExperimentConfig(opponent_policy="ladder")
    tool = next(tool for tool in experiment_tools(config) if tool["name"] == "small_match")
    ids = tool["inputSchema"]["properties"]["opponent_ids"]
    assert ids["uniqueItems"] is False and ids["maxItems"] == 8
    assert "rank 25" in tool["description"] and "decrease by 1 or 2" in tool["description"]
    assert "每批 4-8" not in prompt_runtime(config)._objective()


def test_random_schema_explains_controller_selection_and_top5_is_bounded():
    random_tool = next(tool for tool in experiment_tools(ExperimentConfig(opponent_policy="random"))
                       if tool["name"] == "small_match")
    assert "batch size only" in random_tool["description"]
    top5 = next(tool for tool in experiment_tools(ExperimentConfig(opponent_policy="top5"))
                if tool["name"] == "small_match")
    assert top5["inputSchema"]["properties"]["opponent_ids"]["maxItems"] == 5


@pytest.mark.parametrize("rank", [5, 15, 25, 35])
def test_clone_goal_is_one_independent_trial_without_official_tools(rank):
    config = ExperimentConfig(opponent_policy="clone", clone_rank=rank)
    runtime = prompt_runtime(config)
    goal = runtime._objective()
    assert f"rank {rank}" in goal and "32 次" in goal
    assert "本 trial 仅产出一个" in goal
    assert "最后一次策略学习" in goal and "后续独立测量" in goal
    assert "再用大对局确认" not in goal
    tools = {tool["name"]: tool for tool in experiment_tools(config)}
    assert "large_match" not in tools and "restore_champion" not in tools
    assert tools["small_match"]["inputSchema"]["properties"]["opponent_ids"]["maxItems"] == 1


def test_experiment_metadata_is_reinjected_after_compaction_and_on_resume(tmp_path):
    runtime = prompt_runtime(ExperimentConfig(opponent_policy="ladder", feedback="binary"))
    runtime.thread_id = "same-thread"
    runtime.turn_timeout_s = 1
    runtime._resume_active = lambda: None
    runtime._pause_active = lambda: None
    runtime.client.next_message = lambda _: {
        "method": "turn/completed", "params": {"turn": {"status": "completed"}}
    }
    runtime._run_turn_once("继续")
    text = runtime.client.requests[-1][1]["input"][0]["text"]
    assert '"opponent_policy": "ladder"' in text and '"feedback": "binary"' in text
    assert runtime._experiment_context(text) == text
    runtime._start_thread(runtime._objective(), tools=experiment_tools(runtime.experiment))
    start = next(params for method, params in runtime.client.requests if method == "thread/start")
    assert '"feedback": "binary"' in start["developerInstructions"]


def test_random_sampling_seed_stays_in_controller_config_only(tmp_path):
    config = ExperimentConfig(opponent_policy="random", seed=918273645)
    sealed = tmp_path / "controller" / "experiment.json"
    config.freeze(sealed)
    assert json.loads(sealed.read_text())["seed"] == config.seed
    assert json.loads(sealed.with_name("experiment.frozen.json").read_text())["seed"] == config.seed
    runtime = prompt_runtime(config)
    goal = runtime._objective()
    runtime._start_thread(goal, tools=experiment_tools(config))
    prompts = [goal, runtime._experiment_context("控制器已恢复。"),
               runtime._experiment_context("继续推进同一个持久目标。")]
    prompts.extend(json.dumps(params, ensure_ascii=False) for _, params in runtime.client.requests)
    for prompt in prompts:
        assert str(config.seed) not in prompt and '"seed"' not in prompt
    contract = json.loads(goal.split("不可变实验配置：", 1)[1].splitlines()[0])
    assert contract == {"opponent_policy": "random", "feedback": "detailed",
                        "initial_rank": 25, "clone_rank": None}


def test_early_phase_one_expiration_allows_exactly_sixteen_new_large(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "strategy").mkdir(parents=True)
    (workspace / "strategy" / "main.py").write_text("print('baseline')\n")
    snapshot = SnapshotStore(tmp_path / "snapshots").create(workspace)
    with RunLedger(tmp_path / "ledger.sqlite3") as ledger:
        ledger.initialize(game="antwar", model_profile="test", small_budget=128, large_budget=16)
        baseline, _ = ledger.reserve("baseline", 0, snapshot, {})
        ledger.complete(baseline, {"rank": 1})
        first, _ = ledger.reserve("large", 1, snapshot, {})
        ledger.complete(first, {"rank": 1})
        ledger.update_runtime(thread_id="same-thread", metadata={"champion": {
            "snapshot_id": snapshot.snapshot_id, "rank": 1,
        }})
        ledger.finalize_early(snapshot.snapshot_id, reason="rank 1 reached")
        ledger.mark_complete()
        ledger.extend_budget_once()
        before = ledger.state()
        assert before["large_used"] == 1 and before["small_used"] == 0
        assert before["metadata"]["budget_extension"]["first_stage"]["large_used"] == 1
        assert ledger.budgets().as_dict()["large_expired"] == 15
        assert ledger.budgets().large_remaining == 16 and ledger.budgets().small_remaining == 128
        runtime = runtime_for(SimpleNamespace(game="antwar", ledger=ledger))
        goal = runtime._objective()
        assert "即使首阶段 CHAMPION 或本阶段达到 rank 1" in goal
        assert "只有 rank 1 或 large 预算耗尽才结束" not in goal
        for index in range(16):
            match_id, _ = ledger.reserve("large", 1, snapshot, {})
            ledger.complete(match_id, {"rank": 1})
            assert ledger.state()["status"] == ("running" if index < 15 else "finalizing")
        assert ledger.state()["large_used"] == 17
        assert ledger.budgets().large_remaining == 0
        assert ledger.state()["thread_id"] == "same-thread"
        with pytest.raises(BudgetError):
            ledger.reserve("large", 1, snapshot, {})


@pytest.fixture
def clone_service(tmp_path, monkeypatch):
    root = tmp_path / "trial"
    (root / "controller").mkdir(parents=True)
    (root / "controller" / "experiment.json").write_text(json.dumps({
        "opponent_policy": "clone", "clone_rank": 5,
    }))
    monkeypatch.setattr("aa_arena.benchmark.service.rebuild_report", lambda _: None)
    with BenchmarkService(root, game="antwar", model_profile="test", small_budget=32,
                          large_budget=0, workers=1) as service:
        service.initialize_workspace()
        monkeypatch.setattr(service, "_preflight", lambda: None)
        opponent = next(row.opponent_id for row in service.matches.opponents if row.rank == 5)
        def evaluate(pending, snapshot):
            assert pending["kind"] == "small"
            assert pending["request"]["opponent_ids"] == [opponent]
            return {"kind": "small", "opponents": [opponent], "wins": 1, "losses": 1,
                    "seats": [], "candidate_errors": 0}
        monkeypatch.setattr(service, "_execute_pending", evaluate)
        yield service, opponent


def complete_clone_matches(service, opponent, count=32):
    for _ in range(count):
        service.small_match([opponent])


def test_real_service_final_round_is_adapted_before_runtime_freezes(clone_service):
    service, opponent = clone_service
    complete_clone_matches(service, opponent, 31)
    runtime = runtime_for(service)
    source = service.workspace / "strategy" / "last_learned.py"
    source.write_text("policy = 'before observation 32'\n")
    turns = []
    submitted = []
    def turn(prompt, *, final=False):
        turns.append(prompt)
        if final:
            assert service.ledger.state()["frozen_snapshot_id"] != submitted[0]
            assert "尚未进行独立全池评测" in prompt
            assert not source.stat().st_mode & 0o222
        elif service.ledger.budgets().small_remaining:
            result = service.small_match([opponent])
            submitted.append(result["snapshot_id"])
            assert service.ledger.state()["status"] == "running"
            assert service.ledger.state()["frozen_snapshot_id"] is None
        else:
            assert "第 32 次反馈" in prompt
            assert service.ledger.submissions()[-1]["submission_id"] in prompt
            source.write_text("policy = 'learned from observation 32'\n")
    runtime._run_turn = turn
    runtime.run()
    state = service.ledger.state()
    frozen = service.snapshots.verify(state["frozen_snapshot_id"])
    assert frozen.strategy_hash != service.snapshots.verify(submitted[0]).strategy_hash
    restored = service.controller / "verify-final"
    service.snapshots.materialize(frozen.snapshot_id, restored)
    assert (restored / "strategy" / "last_learned.py").read_text() == source.read_text()
    assert "learned from observation 32" in source.read_text()
    assert state["status"] == "complete"
    assert state["metadata"]["clone_adaptation_complete"] is True
    assert "champion" not in state["metadata"]
    assert (state["small_used"], state["large_used"]) == (32, 0)
    assert len(turns) == 3
    assert all(row["kind"] == "small" for row in service.ledger.submissions())


def test_real_service_pending_round32_recovers_once_then_adapts(clone_service, monkeypatch):
    service, opponent = clone_service
    complete_clone_matches(service, opponent, 31)
    execute = service._execute_pending
    monkeypatch.setattr(service, "_execute_pending", lambda *_: (_ for _ in ()).throw(RuntimeError("interrupted")))
    with pytest.raises(RuntimeError, match="interrupted"):
        service.small_match([opponent])
    assert service.ledger.budgets().small_used == 32
    pending = service.ledger.pending_submission()
    assert pending is not None
    runtime = runtime_for(service)
    with pytest.raises(RuntimeError, match="pending match"):
        runtime._finish_clone_learning()
    monkeypatch.setattr(service, "_execute_pending", execute)
    service.recover_pending()
    assert service.ledger.state()["status"] == "running"
    assert len(service.ledger.submissions()) == 32
    assert service.ledger.submissions()[-1]["submission_id"] == pending["submission_id"]
    source = service.workspace / "strategy" / "last_learned.py"
    def adapt(prompt, *, final=False):
        if not final:
            assert pending["submission_id"] in prompt
            source.write_text("policy = 'recovered final feedback'\n")
    runtime._run_turn = adapt
    runtime.run()
    assert service.ledger.state()["status"] == "complete"
    assert service.ledger.budgets().small_used == 32
    assert "recovered final feedback" in source.read_text()


def test_interrupted_clone_adaptation_retries_but_completed_adaptation_does_not(clone_service, monkeypatch):
    service, opponent = clone_service
    complete_clone_matches(service, opponent)
    runtime = runtime_for(service)
    runtime._run_turn = lambda *_: (_ for _ in ()).throw(RuntimeError("model interrupted"))
    with pytest.raises(RuntimeError, match="model interrupted"):
        runtime._finish_clone_learning()
    assert not service.ledger.state()["metadata"].get("clone_adaptation_complete")
    assert service.ledger.state()["frozen_snapshot_id"] is None
    adapted = []
    runtime._run_turn = lambda prompt: adapted.append(prompt)
    finalize = service.finalize_clone
    monkeypatch.setattr(service, "finalize_clone", lambda: (_ for _ in ()).throw(RuntimeError("freeze interrupted")))
    with pytest.raises(RuntimeError, match="freeze interrupted"):
        runtime._finish_clone_learning()
    assert service.ledger.state()["metadata"]["clone_adaptation_complete"] is True
    monkeypatch.setattr(service, "finalize_clone", finalize)
    runtime._finish_clone_learning()
    assert len(adapted) == 1
    assert service.ledger.state()["status"] == "finalizing"
    assert service.ledger.budgets().small_used == 32


def test_binary_treatment_preserves_non_evidence_goal_and_protocol_guidance():
    baseline = prompt_runtime()._objective()
    binary = prompt_runtime(ExperimentConfig(feedback="binary"))._objective()
    # Full default paragraphs for objective, budgets and champion policy remain
    # byte-identical; evidence-access and replay-learning paragraphs are treated.
    for paragraph in GOAL_TEMPLATE.format(game="antwar", small_budget=128, large_budget=16).split("\n\n"):
        if paragraph.startswith(("你正在", "小对局额度", "每次改动")):
            assert paragraph in binary
    for line in TOKEN_EFFICIENCY_INSTRUCTIONS.splitlines():
        if line.startswith(("- small_match", "- 控制器", "- 未达到", "- 正式运行")):
            assert line in binary
    for guidance in ("围绕一个主要可证伪假设", "先搜索，再定点读取", "从规则推导优先使用的战术",
                     "先在小对局验证，再用大对局确认", "假设、结果、失败原因和下一步"):
        assert guidance in baseline and guidance in binary
    assert "replay.json" in baseline and "replay.json" not in binary


def test_real_service_default_phase2_iterates_past_prior_rank1(tmp_path, monkeypatch):
    monkeypatch.setattr("aa_arena.benchmark.service.rebuild_report", lambda _: None)
    root = tmp_path / "default"
    def attach_evaluator(service):
        monkeypatch.setattr(service, "_preflight", lambda: None)
        monkeypatch.setattr(service, "_execute_pending", lambda pending, _: {
            "kind": pending["kind"], "rank": 1, "elo": 2000, "wins": 2, "losses": 0,
            "candidate_errors": 0,
        })
    with BenchmarkService(root, game="antwar", model_profile="test", workers=1) as service:
        service.initialize_workspace()
        attach_evaluator(service)
        service.baseline()
        service.large_match()
        assert service.ledger.state()["status"] == "finalizing"
        assert service.ledger.state()["large_used"] == 1
        runtime = runtime_for(service)
        runtime._run_turn = lambda prompt, **_: None
        runtime.run()
        first_snapshot = service.ledger.state()["frozen_snapshot_id"]
    with BenchmarkService(root, game="antwar", model_profile="test", workers=1,
                          extend_budget_once=True) as service:
        service.initialize_workspace()
        attach_evaluator(service)
        runtime = runtime_for(service)
        evaluations = []
        def turn(prompt, *, final=False):
            if not final:
                result = service.large_match()
                evaluations.append(result)
                assert result["stopped_early"] is False
        runtime._run_turn = turn
        runtime.run()
        assert len(evaluations) == 16
        assert service.ledger.state()["status"] == "complete"
        assert service.ledger.state()["frozen_snapshot_id"] == first_snapshot
        assert service.ledger.budgets().large_remaining == 0
        assert service.ledger.state()["large_used"] == 17
        assert service.ledger.state()["small_used"] == 0


def test_clone_final_review_is_claimed_once_on_resume(clone_service):
    service, opponent = clone_service
    complete_clone_matches(service, opponent)
    runtime = runtime_for(service)
    reviews = []
    def turn(prompt, *, final=False):
        if final:
            reviews.append(prompt)
            raise RuntimeError("review interrupted")
    runtime._run_turn = turn
    with pytest.raises(RuntimeError, match="review interrupted"):
        runtime.run()
    snapshot_id = service.ledger.state()["frozen_snapshot_id"]
    assert service.ledger.state()["status"] == "reviewing"
    runtime.run()
    assert len(reviews) == 1
    assert service.ledger.state()["status"] == "complete"
    assert service.ledger.state()["frozen_snapshot_id"] == snapshot_id
    assert service.ledger.budgets().small_used == 32


@pytest.mark.parametrize("previously_complete", [False, True])
def test_real_clone_repairs_invalid_final_code_before_freeze(clone_service, monkeypatch, previously_complete):
    import shutil

    service, opponent = clone_service
    complete_clone_matches(service, opponent)
    strategy = service.workspace / "strategy"
    shutil.rmtree(strategy)
    strategy.mkdir()
    source = strategy / "main.py"
    source.write_text("def broken(:\n")
    # Exercise the actual service and MatchService Python syntax preflight.
    monkeypatch.setattr(service, "_preflight", BenchmarkService._preflight.__get__(service))
    monkeypatch.setattr(service, "_ensure_storage_headroom", lambda: None)
    if previously_complete:
        metadata = service.ledger.state()["metadata"]
        metadata["clone_adaptation_complete"] = True
        service.ledger.update_runtime(metadata=metadata)
    runtime = runtime_for(service)
    repairs = []
    def turn(prompt, *, final=False):
        if "最终修复" in prompt:
            repairs.append(prompt)
            assert service.ledger.state()["metadata"]["clone_adaptation_complete"] is False
            assert service.ledger.state()["frozen_snapshot_id"] is None
            assert str(service.controller) not in prompt
            source.write_text("print('learned policy repaired after feedback 32')\n")
        elif not final:
            assert "第 32 次反馈" in prompt
    runtime._run_turn = turn
    runtime.run()
    state = service.ledger.state()
    assert len(repairs) == 1 and state["status"] == "complete"
    assert state["metadata"]["clone_final_repair_attempts"] == 1
    assert state["metadata"]["clone_adaptation_complete"] is True
    assert (state["small_used"], state["large_used"]) == (32, 0)
    assert len(service.ledger.submissions()) == 32
    restored = service.controller / "verify-repair"
    service.snapshots.materialize(state["frozen_snapshot_id"], restored)
    assert (restored / "strategy" / "main.py").read_text() == source.read_text()
    assert "repaired after feedback 32" in source.read_text()


def test_clone_final_repairs_are_bounded_across_resume(clone_service, monkeypatch):
    service, opponent = clone_service
    complete_clone_matches(service, opponent)
    runtime = runtime_for(service)
    turns = []
    runtime._run_turn = lambda prompt: turns.append(prompt)
    def bad_preflight():
        raise ValueError("candidate Python preflight failed: invalid syntax")
    monkeypatch.setattr(service, "_preflight", bad_preflight)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="after two final repair turns"):
            runtime._finish_clone_learning()
    assert len(turns) == 3  # One adaptation plus two repairs; resume grants none.
    state = service.ledger.state()
    assert state["metadata"]["clone_final_repair_attempts"] == 2
    assert state["metadata"]["clone_adaptation_complete"] is False
    assert state["status"] == "running" and state["frozen_snapshot_id"] is None
    assert (state["small_used"], state["large_used"]) == (32, 0)


def test_candidate_preflight_classification_excludes_infrastructure():
    from aa_arena.benchmark.runtime import _candidate_preflight_failure
    from aa_arena.core.cpp_build import CppBuildError, CppBuildSandboxError
    from aa_arena.legacy.ai9 import Ai9Error

    assert _candidate_preflight_failure(CppBuildError("C++ build failed: invalid syntax"))
    assert _candidate_preflight_failure(ValueError("strategy directory is missing"))
    assert _candidate_preflight_failure(Ai9Error("build failed (g++): invalid syntax"))
    for failure in (RuntimeError("benchmark requires at least 8 GiB free"),
                    CppBuildSandboxError("sandbox unavailable"),
                    CppBuildError("cmake executable is unavailable"),
                    CppBuildError("C++ build failed: fatal error: No space left on device"),
                    ValueError("unknown controller validation failure"),
                    OSError(28, "No space left on device")):
        assert not _candidate_preflight_failure(failure)
    wrapped = ValueError("candidate Python preflight failed: disk I/O error")
    wrapped.__cause__ = OSError(5, "Input/output error")
    assert not _candidate_preflight_failure(wrapped)


def test_final_clone_storage_failure_does_not_request_model_repair(clone_service, monkeypatch):
    service, opponent = clone_service
    complete_clone_matches(service, opponent)
    runtime = runtime_for(service)
    turns = []
    runtime._run_turn = lambda prompt: turns.append(prompt)
    def no_storage():
        raise RuntimeError("benchmark requires at least 8 GiB free")
    monkeypatch.setattr(service, "_preflight", no_storage)
    with pytest.raises(RuntimeError, match="8 GiB"):
        runtime._finish_clone_learning()
    state = service.ledger.state()
    assert len(turns) == 1
    assert not state["metadata"].get("clone_final_repair_attempts")
    assert not state["metadata"].get("clone_adaptation_complete")
    assert state["frozen_snapshot_id"] is None

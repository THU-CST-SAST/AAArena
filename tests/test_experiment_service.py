"""Controller policy boundaries using real ledgers, snapshots and recovery."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from aa_arena.benchmark.experiment import ExperimentConfig
from aa_arena.benchmark.matches import MatchInfrastructureError, MatchService, SeatResult
from aa_arena.benchmark.service import BenchmarkService


SENTINEL = "SYNTHETIC_PRIVATE_DETAIL_49317"


def detailed(ids):
    return {
        "kind": "small", "opponents": list(ids), "wins": 1, "draws": 1, "losses": 1,
        "diagnostic": SENTINEL, "arbitrary_nested": {"private": SENTINEL},
        "seats": [
            {"opponent_id": ids[0], "candidate_roles": [f"seat-{index}"],
             "outcome": outcome, "score": score, "rounds": 49317,
             "diagnostic": SENTINEL, "replay_path": SENTINEL,
             "narration_path": SENTINEL, "seed": 49317, "attempts": 3}
            for index, (outcome, score) in enumerate((("win", 1), ("draw", .5), ("loss", 0)))
        ],
    }


@pytest.fixture
def factory(tmp_path, monkeypatch):
    monkeypatch.setattr("aa_arena.benchmark.service.rebuild_report", lambda _: None)
    services = []

    def make(config=None, *, name="run", small=128, large=16, game="antwar"):
        root = tmp_path / name
        path = root / "controller" / "experiment.json"
        if config is not None and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(config))
        service = BenchmarkService(root, game=game, model_profile="test-model-a",
                                   small_budget=small, large_budget=large, workers=1)
        services.append(service)
        service.initialize_workspace()
        monkeypatch.setattr(service, "_preflight", lambda: None)
        monkeypatch.setattr(service, "_execute_pending", lambda pending, snapshot:
                            detailed(pending["request"]["opponent_ids"]))
        return service

    yield make
    for service in services:
        service.close()


def ids(service, *ranks):
    by_rank = {row.rank: row.opponent_id for row in service.matches.opponents}
    return [by_rank[rank] for rank in ranks]


@pytest.mark.parametrize("config", [
    {"opponent_policy": "unknown"}, {"feedback": "all"}, {"seed": True},
    {"seed": -1}, {"seed": 2**63}, {"opponent_policy": "ladder", "initial_rank": 0},
    {"clone_rank": 5}, {"opponent_policy": "clone"},
    {"opponent_policy": "clone", "clone_rank": 10}, {"surprise": "value"},
])
def test_invalid_config_rejected_before_ledger(tmp_path, config):
    path = tmp_path / "controller" / "experiment.json"
    path.parent.mkdir()
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        BenchmarkService(tmp_path, game="antwar", model_profile="test-model-a")
    assert not (path.parent / "ledger.sqlite3").exists()


def test_defaults_and_config_immutability(factory):
    service = factory()
    assert service.experiment == ExperimentConfig()
    assert service.matches.seed == 20260831
    with pytest.raises(FrozenInstanceError):
        service.experiment.seed = 10
    request_result = service.small_match(ids(service, 40))
    assert request_result["seats"][0]["diagnostic"] == SENTINEL
    assert service.ledger.submissions()[0]["request"] == {"opponent_ids": ids(service, 40)}
    path = service.controller / "experiment.json"
    path.chmod(0o600)
    path.write_text(json.dumps({"feedback": "binary"}))
    with pytest.raises(ValueError, match="immutable"):
        factory()
    assert service.ledger.budgets().small_used == 1


def test_deleted_nondefault_config_cannot_resume_as_baseline(factory):
    service = factory({"feedback": "binary"})
    (service.controller / "experiment.json").unlink()
    with pytest.raises(ValueError, match="immutable"):
        factory()


@pytest.mark.parametrize("feedback", ["detailed", "binary"])
def test_random_seed_stays_private_across_public_service_surfaces(factory, feedback):
    seed = 8971236701
    service = factory({"opponent_policy": "random", "feedback": feedback, "seed": seed})
    manifest = service.model_tool_call("workspace_manifest", {})
    assert "seed" not in manifest["experiment"]
    assert manifest["experiment"]["opponent_policy"] == "random"
    result = service.model_tool_call("small_match", {"opponent_ids": ids(service, 1)})
    surfaces = [manifest, service.model_tool_call("list_opponents", {}),
                service.model_tool_call("budget_status", {}), result,
                json.loads((service.workspace / result["raw_result_path"]).read_text())]
    assert str(seed) not in json.dumps(surfaces)
    assert service.experiment.as_dict()["seed"] == seed
    assert service.ledger.state()["metadata"]["experiment"]["seed"] == seed
    for name in ("experiment.json", "experiment.frozen.json"):
        assert json.loads((service.controller / name).read_text())["seed"] == seed


def test_ladder_batch_sequence_repeats_and_rank_one_boundary(factory):
    service = factory({"opponent_policy": "ladder"})
    assert [row["rank"] for row in service.list_opponents()["opponents"]] == [25]
    with pytest.raises(ValueError, match="start at rank 25"):
        service.small_match(ids(service, 24))
    service.small_match(ids(service, 25, 25, 23, 22))
    assert [row["rank"] for row in service.list_opponents()["opponents"]] == [20, 21, 22]
    before = service.ledger.budgets().small_used
    for ranks in ((23,), (19,), (21, 18), (22, 23)):
        with pytest.raises(ValueError, match="repeat or improve"):
            service.small_match(ids(service, *ranks))
    assert service.ledger.budgets().small_used == before
    service.small_match(ids(service, 20, 18, 16, 14, 12, 10, 8, 6))
    service.small_match(ids(service, 4, 2, 1, 1))
    assert [row["rank"] for row in service.list_opponents()["opponents"]] == [1]
    with pytest.raises(ValueError):
        service.small_match(ids(service, 2))
    assert service.ledger.budgets().small_used == 16


def test_top5_rejects_outside_batch_before_reservation(factory):
    service = factory({"opponent_policy": "top5"}, small=5)
    assert [row["rank"] for row in service.list_opponents()["opponents"]] == list(range(1, 6))
    with pytest.raises(ValueError, match="top5"):
        service.small_match(ids(service, 1, 6))
    assert service.ledger.submissions() == []
    service.small_match(ids(service, 5, 4, 3, 2, 1))
    with pytest.raises(ValueError, match="remaining"):
        service.small_match(ids(service, 1))
    assert service.ledger.budgets().small_used == 5


def test_random_is_seeded_audited_and_resumes_without_reselection(factory, monkeypatch):
    config = {"opponent_policy": "random", "seed": 20260915}
    service = factory(config)
    reference = factory(config, name="reference")
    other = factory({**config, "seed": 1}, name="other")
    with pytest.raises(ValueError, match="unknown verified"):
        service.tool_call("small_match", {"opponent_ids": ["invalid-id"]})
    sequence = []
    for _ in range(8):
        sequence.append(reference.tool_call("small_match", {})["opponents"])
        other.small_match()
    assert sequence != [row["request"]["opponent_ids"] for row in other.ledger.submissions()]
    assert service.ledger.budgets().small_used == 0
    monkeypatch.setattr(service, "_execute_pending", lambda *_: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        service.small_match()
    pending = service.ledger.pending_submission()
    assert pending["request"]["opponent_ids"] == sequence[0]
    assert pending["request"]["selected_opponent_ids"] == sequence[0]
    resumed = factory()
    assert resumed.small_match()["opponents"] == sequence[0]
    assert resumed.ledger.budgets().small_used == 1
    assert resumed.ledger.submissions()[0]["request"] == pending["request"]
    for expected in sequence[1:]:
        assert resumed.small_match()["opponents"] == expected
    assert resumed.ledger.budgets().small_used == 8


def test_random_batches_ignore_supplied_identity_and_charge_selected_count(factory):
    first = factory({"opponent_policy": "random"}, small=8)
    second = factory({"opponent_policy": "random"}, name="second", small=8)
    one = first.small_match(ids(first, 1, 2, 3, 4))
    two = second.small_match(ids(second, 25, 26, 27, 28))
    assert one["opponents"] == two["opponents"]
    assert len(set(one["opponents"])) == 4
    assert first.ledger.budgets().small_used == second.ledger.budgets().small_used == 4
    request = first.ledger.submissions()[0]["request"]
    assert request["requested_opponent_ids"] == ids(first, 1, 2, 3, 4)
    assert request["selected_opponent_ids"] == one["opponents"]


def test_ladder_pending_recovery_preserves_sequence_and_cost(factory, monkeypatch):
    service = factory({"opponent_policy": "ladder"})
    monkeypatch.setattr(service, "_execute_pending", lambda *_: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        service.small_match(ids(service, 25, 23, 22))
    resumed = factory()
    with pytest.raises(ValueError, match="pending"):
        resumed.small_match(ids(service, 21))
    resumed.small_match(ids(service, 25, 23, 22))
    resumed.small_match(ids(service, 20))
    assert resumed.ledger.budgets().small_used == 4
    assert len(resumed.ledger.submissions()) == 2


def assert_binary(result):
    assert set(result) <= {"kind", "opponents", "seats", "match_id", "snapshot_id",
                           "snapshot_integrity", "budget", "raw_result_path"}
    assert [seat["win"] for seat in result["seats"]] == [True, False, False]
    assert all(set(seat) == {"opponent_id", "candidate_roles", "win"} for seat in result["seats"])
    assert SENTINEL not in json.dumps(result)


@pytest.mark.parametrize("recover", [False, True])
def test_binary_private_routing_finish_artifacts_and_recovery(factory, monkeypatch, recover):
    service = factory({"feedback": "binary"})
    # Restore real execution, then simulate an evaluator that attempts to return private detail.
    monkeypatch.setattr(service, "_execute_pending", BenchmarkService._execute_pending.__get__(service))
    attempts = []

    def evaluate(strategy, selected, submission_id, replay_root, **options):
        assert options == {"feedback": "binary"}
        assert replay_root.is_relative_to(service.controller)
        private = replay_root / submission_id / "raw.json"
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_text(SENTINEL)
        assert not list((service.workspace / "replays").rglob("*.json"))
        attempts.append(submission_id)
        if recover and len(attempts) == 1:
            raise RuntimeError(SENTINEL)
        return detailed(selected)

    monkeypatch.setattr(service.matches, "small_match", evaluate)
    if recover:
        with pytest.raises(MatchInfrastructureError) as error:
            service.model_tool_call("small_match", {"opponent_ids": ids(service, 1)})
        assert SENTINEL not in str(error.value)
        assert service.ledger.budgets().small_used == 1
        result = service.recover_pending()
        public = service.agent_result(result)
        assert attempts[0] == attempts[1]
    else:
        public = service.model_tool_call("small_match", {"opponent_ids": ids(service, 1)})
    assert_binary(public)
    assert_binary(service.ledger.submissions()[0]["result"])
    assert_binary(service.trajectory.read()[0]["result"])
    assert not list((service.controller / "binary-replays").rglob("*.json"))
    assert not list((service.workspace / "replays").rglob("*.json"))
    assert_binary(json.loads((service.workspace / public["raw_result_path"]).read_text()))
    assert service.ledger.budgets().small_used == 1
    # Crash after commit/before trajectory fsync: rebuilding uses the same allowlist.
    service.trajectory.events_path.unlink()
    service.reconcile_trajectory()
    assert_binary(service.trajectory.read()[0]["result"])


def test_binary_match_service_never_narrates_or_reads_replay(factory, monkeypatch):
    service = factory({"feedback": "binary"})
    opponent = ids(service, 1)[0]
    private = service.controller / "synthetic-private-replay.json"
    private.write_text(SENTINEL)
    rows = tuple(SeatResult(opponent, (f"seat-{index}",), "complete", outcome, score,
                           None, 49317, SENTINEL, str(private), 3, 42)
                 for index, (outcome, score) in enumerate((("win", 1), ("draw", .5), ("loss", 0))))
    monkeypatch.setattr(service.matches, "_run", lambda *_: rows)
    monkeypatch.setattr("aa_arena.benchmark.matches.narrate", lambda *_a, **_k: pytest.fail("narration attempted"))
    monkeypatch.setattr("aa_arena.benchmark.matches.compact_replay", lambda *_a, **_k: pytest.fail("replay attempted"))
    result = service.matches.small_match(service.workspace / "strategy", [opponent], "binary",
                                         service.workspace / "replays", feedback="binary")
    assert_binary(result)
    assert not list((service.workspace / "replays").rglob("*"))


def test_binary_does_not_change_official_baseline_or_large(factory, monkeypatch):
    service = factory({"feedback": "binary"})
    official = {"kind": "large", "rank": 10, "elo": 1000, "wins": 2, "rounds": 49317}
    monkeypatch.setattr(service, "_execute_pending", lambda *_: official)
    assert service.baseline()["rounds"] == 49317
    result = service.model_tool_call("large_match", {})
    assert result["rank"] == 10
    assert result["elo"] == 1000
    assert json.loads((service.workspace / result["raw_result_path"]).read_text())["rounds"] == 49317


@pytest.mark.parametrize("game,rank", [("rollman",5), ("rollman",15), ("rollman",25), ("antwar",35)])
def test_clone_single_fixed_pool_rank_including_rollman(factory, game, rank):
    service = factory({"opponent_policy": "clone", "clone_rank": rank},
                      small=32, large=0, game=game)
    assert [row["rank"] for row in service.list_opponents()["opponents"]] == [rank]
    with pytest.raises(ValueError, match="exactly one"):
        service.small_match(ids(service, rank, rank - 1))
    with pytest.raises(ValueError, match="exactly one"):
        service.small_match(ids(service, rank - 1))
    with pytest.raises(ValueError, match="baseline"):
        service.baseline()
    with pytest.raises(ValueError, match="large"):
        service.large_match()
    assert service.ledger.submissions() == []


def test_clone_32_replicates_resume_and_freeze_post_feedback_adaptation(factory, monkeypatch):
    config = {"opponent_policy": "clone", "clone_rank": 25}
    service = factory(config, small=32, large=0)
    for index in range(31):
        result = service.small_match(ids(service, 25))
        service.ledger.mark_delivered(result["match_id"])
    monkeypatch.setattr(service, "_execute_pending", lambda *_: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        service.small_match(ids(service, 25))
    accepted = service.ledger.pending_submission()
    assert accepted["request"]["replicate"] == 32
    resumed = factory(small=32, large=0)
    result = resumed.recover_pending()
    assert resumed.ledger.submissions()[-1]["request"] == accepted["request"]
    assert resumed.ledger.budgets().small_used == 32
    assert resumed.ledger.state()["status"] == "running"
    assert resumed.ledger.state()["frozen_snapshot_id"] is None
    assert len({row["request"]["seed"] for row in resumed.ledger.submissions()}) == 32
    with pytest.raises(ValueError, match="adaptation"):
        resumed.finalize_clone()
    resumed.ledger.mark_delivered(result["match_id"])
    source = next((resumed.workspace / "strategy").rglob("main.py"))
    source.write_text(source.read_text() + "\n# learned from the 32nd feedback\n")
    resumed.ledger.update_runtime(metadata={**resumed.ledger.state()["metadata"],
                                            "clone_adaptation_complete": True})
    frozen = resumed.finalize_clone()
    assert frozen != result["snapshot_id"]
    assert resumed.snapshots.verify(frozen).strategy_hash != resumed.snapshots.verify(result["snapshot_id"]).strategy_hash
    assert "learned from the 32nd feedback" in source.read_text()
    assert resumed.ledger.state()["status"] == "finalizing"
    assert "champion" not in resumed.ledger.state()["metadata"]
    source.write_text("# accidental modification after freezing\n")
    assert resumed.finalize_clone() == frozen
    assert "learned from the 32nd feedback" in source.read_text()


def test_clone_seed_override_is_local_and_used_by_seat_execution(factory, monkeypatch):
    service = factory({"opponent_policy": "clone", "clone_rank": 5}, small=32, large=0)
    observed = []

    def evaluate(self, strategy, opponent, roles, index, submission):
        observed.append(self.seed)
        return SeatResult(opponent.opponent_id, roles, "complete", "draw", .5,
                          None, 1, None, None, 1, self.seed)

    monkeypatch.setattr(MatchService, "_evaluate_seat", evaluate)
    monkeypatch.setattr(service, "_execute_pending", BenchmarkService._execute_pending.__get__(service))
    for _ in range(2):
        service.small_match(ids(service, 5))
    seeds = [row["request"]["seed"] for row in service.ledger.submissions()]
    assert observed == [seeds[0], seeds[0], seeds[1], seeds[1]]
    assert seeds[0] != seeds[1]
    assert service.matches.seed == 20260831


def test_extension_disables_rank_one_early_stop_but_phase_one_keeps_it(factory, monkeypatch):
    service = factory(small=2, large=3)
    monkeypatch.setattr(service, "_execute_pending", lambda *_: {"kind": "large", "rank": 1, "elo": 2000})
    assert service.large_match()["stopped_early"] is True
    assert service.ledger.state()["status"] == "finalizing"
    extended = factory(name="extended", small=2, large=3)
    extended.ledger.update_runtime(metadata={**extended.ledger.state()["metadata"],
                                             "budget_extension": {"small_expired": 0}})
    monkeypatch.setattr(extended, "_execute_pending", lambda *_: {"kind": "large", "rank": 1, "elo": 2000})
    assert extended.large_match()["stopped_early"] is False
    assert extended.ledger.state()["status"] == "running"
    assert extended.ledger.budgets().large_remaining == 2

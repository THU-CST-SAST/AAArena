"""Public controllers use the complete remote inventory, never local subset ranks."""
import json
import pytest
from aa_arena.benchmark.service import BenchmarkService
from aa_arena.benchmark.experiment import ExperimentConfig
from aa_arena.resources import ARENA_GAMES, _rating_rows, REPOSITORY_ROOT
from test_remote_evaluation import api  # authenticated loopback service fixture

@pytest.mark.parametrize('game', ARENA_GAMES)
def test_public_resource_metadata_and_remote_pool(api, tmp_path, monkeypatch, game):
    _, client, url = api
    monkeypatch.setenv('AA_ARENA_EVAL_URL',url)
    monkeypatch.setenv('AA_ARENA_EVAL_TOKEN','owner-a')
    with BenchmarkService(tmp_path/'run',game=game,model_profile='default',small_budget=2,large_budget=1) as service:
        service.initialize_workspace()
        board=json.loads((service.workspace/'resources/leaderboard.json').read_text())
        assert board['evaluation_scope']=='full-pool'
        assert len(service.matches.opponents)==len(_rating_rows(game,REPOSITORY_ROOT))
        assert service.matches.opponents[0].rank==1
        assert service.workspace_manifest()['official_full_pool'] is True
        assert service.list_opponents()['evaluation_scope']=='full-pool'
        assert all(o.package_root.parts[1]=='remote-only' for o in service.matches.opponents)

@pytest.mark.parametrize('feedback',['detailed','binary'])
def test_remote_tool_transaction_and_recovery(api,tmp_path,monkeypatch,feedback):
    store, client, url=api
    monkeypatch.setenv('AA_ARENA_EVAL_URL',url)
    monkeypatch.setenv('AA_ARENA_EVAL_TOKEN','owner-a')
    monkeypatch.setattr('aa_arena.benchmark.service.rebuild_report',lambda _:None)
    run=tmp_path/'run';ExperimentConfig(feedback=feedback).freeze(run/'controller/experiment.json')
    with BenchmarkService(run,game='pacman',model_profile='default',small_budget=2,large_budget=1) as service:
        service.initialize_workspace()
        (service.workspace/"strategy/main.py").write_text("# synthetic loopback fixture\n")
        monkeypatch.setattr(service,'_preflight',lambda:None)
        result=service.small_match([service.matches.opponents[0].opponent_id])
        assert result['budget']['small_used']==1
        assert bool(list((service.workspace/'replays').rglob('*.json')))==(feedback=='detailed')
        remote=service.matches
        # Same frozen snapshot, same ID: server returns the same receipt without charging.
        row=service.ledger.submissions()[0]
        state=service.ledger.state()
        assert state['small_used']==1
        assert len(store.run_path('owner-a',service.run_id).parts)>1
        large=service.large_match()
        assert large['rank']==20 and large['budget']['large_used']==1
        assert service.ledger.state()['small_used']==1
    with BenchmarkService(run,game='pacman',model_profile='default',small_budget=2,large_budget=1) as reopened:
        assert reopened.matches.pool_sha256==remote.pool_sha256
        assert reopened.ledger.state()['large_used']==1


def test_no_credential_has_no_local_fallback(tmp_path,monkeypatch):
    monkeypatch.delenv('AA_ARENA_EVAL_TOKEN',raising=False)
    with pytest.raises(ValueError,match='Formal matches require'):
        BenchmarkService(tmp_path/'run',game='pacman',model_profile='default')

def test_invalid_upload_is_rejected_before_budget_reservation(api,tmp_path,monkeypatch):
    _, _, url=api
    monkeypatch.setenv('AA_ARENA_EVAL_URL',url)
    monkeypatch.setenv('AA_ARENA_EVAL_TOKEN','owner-a')
    monkeypatch.setattr('aa_arena.benchmark.service.rebuild_report',lambda _:None)
    with BenchmarkService(tmp_path/'run',game='pacman',model_profile='default',small_budget=2,large_budget=1) as service:
        service.initialize_workspace()
        (service.workspace/'strategy/forbidden-link').symlink_to('Makefile')
        with pytest.raises(ValueError,match='symlink'):
            service.small_match([service.matches.opponents[0].opponent_id])
        assert service.ledger.state()['small_used']==0
        assert service.ledger.pending_submission() is None

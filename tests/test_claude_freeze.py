from pathlib import Path
from types import SimpleNamespace
from aa_arena.benchmark.service import BenchmarkService


def test_restore_can_repeat_after_read_only_snapshot_and_frozen_parent(tmp_path):
    workspace=tmp_path/'workspace';workspace.mkdir();(workspace/'strategy').mkdir()
    controller=tmp_path/'controller';controller.mkdir()
    def materialize(snapshot,path):
        root=path/'strategy';root.mkdir();child=root/'nested';child.mkdir()
        (child/'bot.py').write_text('pass\n');(child/'bot.py').chmod(0o444)
        child.chmod(0o555);root.chmod(0o555)
        return path
    service=SimpleNamespace(workspace=workspace,controller=controller,snapshots=SimpleNamespace(
        load_manifest=lambda _: {'files':[{'path':'strategy/nested/bot.py','mode':0o644}]},materialize=materialize))
    BenchmarkService._restore_strategy(service,'frozen')
    for p in (workspace,*workspace.rglob('*')):p.chmod(0o555 if p.is_dir() else 0o444)
    BenchmarkService._restore_strategy(service,'frozen')
    assert (workspace/'strategy/nested/bot.py').read_text()=='pass\n'
    assert workspace.stat().st_mode & 0o777 == 0o555
    assert (workspace/'strategy/nested').stat().st_mode & 0o200


def test_readonly_final_receipt_does_not_rewrite_frozen_artifacts(tmp_path):
    from aa_arena.benchmark.experiment import ExperimentConfig
    workspace=tmp_path/'workspace';workspace.mkdir();workspace.chmod(0o555)
    service=SimpleNamespace(workspace=workspace,experiment=ExperimentConfig(),_public_result=lambda kind,value:value)
    result=BenchmarkService.agent_result(service,{'kind':'large','match_id':'saved','rank':1,'elo':1500.,'per_opponent':[]},persist=False)
    assert result['rank']==1 and result['match_id']=='saved'
    assert 'raw_result_path' not in result
    assert list(workspace.iterdir())==[]

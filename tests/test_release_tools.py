from pathlib import Path
import importlib.util
import io
import json
import hashlib
import tarfile
import pytest

ROOT=Path(__file__).parents[1]

def module(name):
    spec=importlib.util.spec_from_file_location('release_'+name,ROOT/'scripts'/(name+'.py'))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value

@pytest.mark.parametrize('name',['/etc/passwd','../escape','games/pacman/../../escape','games\\pacman'])
def test_asset_installer_rejects_unsafe_names(name):
    with pytest.raises(ValueError):module('install_assets').safe_name(name)

def test_asset_install_is_verified_idempotent_and_preserves_modified_files(tmp_path,monkeypatch):
    installer=module('install_assets');source=tmp_path/'source';(source/'assets').mkdir(parents=True)
    data=b'public SDK\n';path='games/pacman/public_sdk/main.py'
    manifest={'game':'pacman','files':[{'path':path,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'mode':420}]}
    pack=source/'assets/pacman.tar.gz'
    with tarfile.open(pack,'w:gz') as archive:
        for name,content in [(path,data),('assets/manifests/pacman.json',json.dumps(manifest).encode())]:
            info=tarfile.TarInfo(name);info.size=len(content);archive.addfile(info,io.BytesIO(content))
    monkeypatch.setattr(installer,'ROOT',source)
    row={'game':'pacman','archive':'pacman.tar.gz','sha256':installer.digest(pack)};dest=tmp_path/'installed'
    installer.install_game(dest,row);installer.install_game(dest,row)
    (dest/path).write_text('local work')
    with pytest.raises(ValueError,match='hash mismatch'):installer.install_game(dest,row)
    assert (dest/path).read_text()=='local work'
    row['sha256']='0'*64
    with pytest.raises(ValueError,match='Archive hash'):installer.install_game(dest,row)


def test_main_and_ablations_have_exact_protocol_and_disjoint_harness_paths(tmp_path):
    runner=module('run_experiments')
    main=runner.plan('main',['default'],None,[10],tmp_path,'codex')['jobs']
    cc=runner.plan('main',['default'],None,[10],tmp_path,'claude')['jobs']
    assert len(main)==12
    assert all((j['small_budget'],j['large_budget'])==(128,16) for j in main)
    assert not {j['run_dir'] for j in main}&{j['run_dir'] for j in cc}
    order=runner.plan('order',['default'],None,[10,11],tmp_path,'codex')['jobs']
    assert len(order)==24
    assert all(j['config']['fixed_small_batch']==4 and j['config']['match_base_seed']==43 for j in order)
    ladder=[j for j in order if j['config']['opponent_policy']=='ladder']
    assert all(j['config']['initial_rank']==30 for j in ladder)
    for suite,count in [('feedback',6),('batch',12),('active-clone',48),('offpolicy',3)]:
        jobs=runner.plan(suite,['default'],None,[10],tmp_path,'codex')['jobs'];assert len(jobs)==count
        if suite=='active-clone':assert all(j['small_budget']==32 and j['large_budget']==0 and j['skip_baseline'] for j in jobs)
        assert all(j['config']['match_base_seed']==42 for j in jobs)


def test_claude_requires_explicit_cost_ceiling(tmp_path):
    runner=module('run_experiments');job=runner.plan('main',['default'],['pacman'],[1],tmp_path,'claude')['jobs'][0]
    with pytest.raises(ValueError,match='max-budget-usd'):runner.command(job,8,None,'codex','claude')
    assert '--max-budget-usd' in runner.command(job,8,1,'codex','claude')


@pytest.mark.parametrize('harness', ['codex', 'claude'])
@pytest.mark.parametrize('game', json.loads((ROOT/'configs/paper.json').read_text())['games'])
def test_every_bundled_game_accepts_all_suites_and_session_continuation(tmp_path, monkeypatch, harness, game):
    import sys
    runner = module('run_experiments')
    for suite in runner.SUITES:
        jobs = runner.plan(suite, ['user-selected-model'], [game], [1], tmp_path, harness)['jobs']
        assert jobs and all(j['game'] == game and j['profile'] == 'user-selected-model' for j in jobs)
        for job in jobs:
            command = runner.command(job, 8, 1, 'codex', 'claude')
            assert command[command.index('--game') + 1] == game
    run = tmp_path/'base'
    (run/'controller').mkdir(parents=True)
    (run/'controller/experiment.json').write_text(json.dumps(jobs[0]['config']))
    (run/'controller/model-identity.json').write_text(json.dumps({'reasoning_effort': 'max'}))
    metadata = {'harness': 'official-claude-code-agent-sdk'} if harness == 'claude' else {}
    monkeypatch.setattr(runner, 'state', lambda _: dict(status='complete', small_total=128,
        large_total=16, game=game, model_profile='user-selected-model', metadata_json=json.dumps(metadata)))
    output = tmp_path/'continuation.json'
    monkeypatch.setattr(sys, 'argv', ['run_experiments.py', 'continuation-plan', '--run-dir', str(run),
        '--harness', harness, '--output', str(output)])
    runner.main()
    continuation = json.loads(output.read_text())['jobs'][0]
    assert continuation['run_dir'] == str(run) and continuation['harness'] == harness
    assert (continuation['small_budget'], continuation['large_budget']) == (256, 32)
    assert continuation['extend'] and continuation['skip_baseline']
    assert '--extend-budget-once' in runner.command(continuation, 8, 1, 'codex', 'claude')

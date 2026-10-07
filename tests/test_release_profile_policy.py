import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('harness', ['codex', 'claude'])
def test_paper_launcher_rejects_lower_effort_before_creating_run(tmp_path, harness):
    profile = Path(os.environ['AA_ARENA_PROFILE_DIR']) / 'default.json'
    value = json.loads(profile.read_text())
    value['reasoning_effort'] = 'high'
    profile.write_text(json.dumps(value))
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location('release_launcher', root/'scripts/run_experiments.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    job = module.plan('main', ['default'], ['pacman'], [1], tmp_path/'runs', harness)['jobs'][0]
    with pytest.raises(ValueError, match='max-effort'):
        module.run_job(job, SimpleNamespace())
    assert not Path(job['run_dir']).exists()


@pytest.mark.parametrize('field,value', [('model','another-model'), ('base_url','https://another.invalid/v1'), ('claude_auth_mode','bearer')])
def test_completed_run_cannot_be_relabelled_with_another_model_configuration(tmp_path, monkeypatch, field, value):
    root=Path(__file__).parents[1]
    spec=importlib.util.spec_from_file_location('release_launcher',root/'scripts/run_experiments.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    job=module.plan('main',['default'],['pacman'],[1],tmp_path/'runs','codex')['jobs'][0]
    monkeypatch.setattr(module,'state',lambda _: {'status':'complete'})
    assert module.run_job(job,SimpleNamespace())['status']=='already_complete'
    record=Path(job['run_dir'])/'controller/model-identity.json'
    identity=json.loads(record.read_text())
    assert 'base_url' not in identity and 'endpoint_sha256' in identity
    profile=Path(os.environ['AA_ARENA_PROFILE_DIR'])/'default.json'
    data=json.loads(profile.read_text());data[field]=value;profile.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='configuration differs'):
        module.run_job(job,SimpleNamespace())
    assert json.loads(record.read_text())==identity


def test_launcher_rejects_imports_from_another_checkout(tmp_path, monkeypatch):
    import aa_arena
    root=Path(__file__).parents[1]
    spec=importlib.util.spec_from_file_location('release_launcher',root/'scripts/run_experiments.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    job=module.plan('main',['default'],['pacman'],[1],tmp_path/'runs','codex')['jobs'][0]
    monkeypatch.setattr(aa_arena,'__file__',str(tmp_path/'different-checkout/src/aa_arena/__init__.py'))
    with pytest.raises(RuntimeError,match='another checkout'):
        module.run_job(job,SimpleNamespace())
    assert not Path(job['run_dir']).exists()


def test_custom_effort_requires_matching_explicit_plan(tmp_path, monkeypatch):
    root=Path(__file__).parents[1]
    spec=importlib.util.spec_from_file_location('release_launcher',root/'scripts/run_experiments.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    job=module.plan('main',['default'],['pacman'],[1],tmp_path/'runs','codex','high')['jobs'][0]
    with pytest.raises(ValueError,match='high-effort'):
        module.run_job(job,SimpleNamespace())
    assert not Path(job['run_dir']).exists()
    profile=Path(os.environ['AA_ARENA_PROFILE_DIR'])/'default.json'
    value=json.loads(profile.read_text());value['reasoning_effort']='high';profile.write_text(json.dumps(value))
    monkeypatch.setattr(module,'state',lambda _: {'status':'complete'})
    assert module.run_job(job,SimpleNamespace())['status']=='already_complete'
    identity=json.loads((Path(job['run_dir'])/'controller/model-identity.json').read_text())
    assert identity['reasoning_effort']=='high'


def test_claude_formal_plan_requires_max_effort(tmp_path):
    root=Path(__file__).parents[1]
    spec=importlib.util.spec_from_file_location('release_launcher',root/'scripts/run_experiments.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with pytest.raises(ValueError,match='Claude experiment plans require max'):
        module.plan('main',['default'],['pacman'],[1],tmp_path/'runs','claude','high')
    job=module.plan('main',['default'],['pacman'],[1],tmp_path/'runs','codex','high')['jobs'][0]
    job['harness']='claude'
    with pytest.raises(ValueError,match='Claude experiment plans require max'):
        module.run_job(job,SimpleNamespace())
    assert not Path(job['run_dir']).exists()

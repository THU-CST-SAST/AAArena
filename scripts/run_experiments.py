#!/usr/bin/env python3
"""Generate reproducible experiment plans and execute them with bounded concurrency."""
from __future__ import annotations
import argparse
import concurrent.futures
import dataclasses
import fcntl
import json
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
PAPER=json.loads((ROOT/'configs/paper.json').read_text())
SUITES=('main','order','feedback','batch','active-clone','offpolicy')

def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp');temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(path)

def plan(suite,profiles,games,seeds,run_root,harness,reasoning_effort='max'):
    if reasoning_effort not in {'max','high'}:raise ValueError('Unsupported declared reasoning effort')
    if harness=='claude' and reasoning_effort!='max':raise ValueError('Formal Claude experiment plans require max effort')
    games=games or (PAPER['games'] if suite in {'main','active-clone'} else PAPER['ablation_games'])
    if set(games)-set(PAPER['games']):raise ValueError('Unknown paper game')
    from aa_arena.benchmark.distribution import local_subset
    from aa_arena.benchmark.remote import public_distribution
    subset = local_subset(ROOT) and not public_distribution(ROOT)
    jobs=[]
    for profile in profiles:
        if Path(profile).name!=profile or profile in {'.','..'}:raise ValueError('Invalid profile name')
        for game in games:
            for seed in seeds:
                base={k:PAPER['main'][k] for k in ('opponent_policy','feedback','initial_rank')}
                base.update(seed=seed,clone_rank=None)
                arms=[('main',{},128,16)]
                if suite=='order':
                    arms=[(policy,{'opponent_policy':policy,'initial_rank':30 if policy=='ladder' else 25,'fixed_small_batch':4,'match_base_seed':43},128,16) for policy in PAPER['order']['policies']]
                elif suite=='feedback':arms=[(f,{'feedback':f,'match_base_seed':42},128,16) for f in ('detailed','binary')]
                elif suite=='batch':arms=[(str(n),{'fixed_small_batch':n,'match_base_seed':42},128,16) for n in (1,2,4,8)]
                elif suite=='active-clone':arms=[(f'rank{rank}',{'opponent_policy':'clone','clone_rank':rank,'match_base_seed':42},32,0) for rank in PAPER['active_clone']['ranks']]
                elif suite=='offpolicy':arms=[('dense',{'opponent_policy':'offpolicy','match_base_seed':42},128,16)]
                if subset:
                    value=json.loads((ROOT/f'results/elo/{game}/measured_elo.json').read_text())
                    rows=value.get('ratings') if isinstance(value,dict) else value
                    count=sum(i>8 and i%2==0 for i in range(1,len(rows)+1))
                    arms=[(arm, {**override, 'initial_rank':min(override.get('initial_rank',base['initial_rank']),count)}, small,large)
                          for arm,override,small,large in arms if override.get('clone_rank',1)<=count]
                for arm,override,small,large in arms:
                    ident=f'{suite}-{harness}-{profile}-{game}-{arm}-seed{seed}'
                    jobs.append({'id':ident,'suite':suite,'game':game,'profile':profile,'harness':harness,'reasoning_effort':reasoning_effort,'run_dir':str((run_root/ident).resolve()),'config':{**base,**override},'small_budget':small,'large_budget':large,'skip_baseline':suite in {'active-clone','offpolicy'}})
    return {'schema_version':1,'suite':suite,'evaluation_scope':'published-subset' if subset else 'full-pool','jobs':jobs}

def state(run):
    ledger=run/'controller/ledger.sqlite3'
    if not ledger.exists():return None
    with sqlite3.connect('file:'+str(ledger)+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row;return dict(db.execute('select * from run_state').fetchone())

def command(job,workers,max_cost,codex,claude):
    args=[sys.executable,'-m']
    if job['harness']=='codex':args+=['aa_arena.cli','benchmark','run','--codex-binary',codex]
    else:
        if max_cost is None or max_cost<=0:raise ValueError('Claude execution requires --max-budget-usd per experiment')
        args+=['aa_arena.benchmark.claude_runtime','--max-budget-usd',str(max_cost)]
    args+=['--run-dir',job['run_dir'],'--game',job['game'],'--model-profile',job['profile'],'--small-budget',str(job['small_budget']),'--large-budget',str(job['large_budget']),'--workers',str(workers)]
    if job.get('skip_baseline'):args+=['--skip-baseline']
    if job.get('extend'):args+=['--extend-budget-once']
    return args

def install_catalog(run,source):
    from aa_arena.benchmark.pool_dense import load_catalog
    from aa_arena.io import sha256_file
    source=source.resolve();catalog=load_catalog(source);dest=run/'controller/pool-dense-catalog.json'
    controller=(run/'controller').resolve()
    for row in catalog['trajectories']:
        relative=Path(row['replay_file'])
        if relative.is_absolute() or '..' in relative.parts or not relative.parts or relative.parts[0] not in {'replays','public-replays'}:
            raise ValueError('Catalog replay must be inside replays/ or public-replays/')
        if not (controller/relative).resolve().is_relative_to(controller):
            raise ValueError('Catalog replay escapes controller')
    if dest.exists():
        if sha256_file(dest)!=sha256_file(source):raise ValueError('Catalog identity changed')
        load_catalog(dest);return
    # Copy only files in the sealed catalog. Never expose source programs to the learner.
    for row in catalog['trajectories']:
        src=catalog['_by_id'][row['trajectory_id']]['_replay_path']
        target=run/'controller'/row['replay_file'];target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() and sha256_file(target)!=sha256_file(src):raise ValueError('Conflicting catalog replay')
        if not target.exists():shutil.copyfile(src,target)
    shutil.copyfile(source,dest);load_catalog(dest)

def run_job(job,a):
    import aa_arena
    if Path(aa_arena.__file__).resolve() != ROOT/'src/aa_arena/__init__.py':
        raise RuntimeError('Arena import is from another checkout; unset PYTHONPATH and install this package in the active environment')
    from aa_arena.benchmark.experiment import ExperimentConfig
    from aa_arena.benchmark.profile import load_profile
    profile=load_profile(job['profile'])
    expected_effort=job.get('reasoning_effort','max')
    if expected_effort not in {'max','high'}:raise ValueError('Unsupported declared reasoning effort')
    if job['harness']=='claude' and expected_effort!='max':raise ValueError('Formal Claude experiment plans require max effort')
    if profile.reasoning_effort != expected_effort:
        raise ValueError(f'Experiment plan requires a {expected_effort}-effort model profile; profile and plan must request the same effort')
    run=Path(job['run_dir']);run.mkdir(parents=True,exist_ok=True)
    with (run/'launcher.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        identity=dataclasses.asdict(profile)
        identity['endpoint_sha256']=hashlib.sha256(identity.pop('base_url').encode()).hexdigest()
        identity['harness']=job['harness']
        identity_path=run/'controller/model-identity.json'
        if identity_path.exists():
            if json.loads(identity_path.read_text())!=identity:
                raise ValueError('Existing run model/provider configuration differs; use a new run directory')
        else:write_json(identity_path,identity)
        config=ExperimentConfig(**job['config']);path=run/'controller/experiment.json'
        if path.exists():
            if ExperimentConfig.load(path)!=config:raise ValueError('Existing experiment configuration differs')
        else:write_json(path,config.as_dict())
        s=state(run)
        if s and s['status']=='complete' and not job.get('extend'):
            return {'id':job['id'],'status':'already_complete'}
        if config.is_offpolicy:
            if not a.catalog_root:
                from aa_arena.benchmark.remote import download_catalog
                source=download_catalog(job['game'], run/'catalog-download')
            else:
                source=a.catalog_root/job['game']/'manifest.json'
            if json.loads(source.read_text()).get('game')!=job['game']:raise ValueError('Catalog game mismatch')
            install_catalog(run,source)
        cmd=command(job,a.workers,a.max_budget_usd,a.codex_binary,a.claude_binary)
        env={**os.environ,'AA_ARENA_JOB_ID':job['id'],'AA_ARENA_ADMISSION_ROOT':str(a.admission_root.resolve()),'AA_ARENA_CLAUDE_CLI':a.claude_binary,'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','PYTHONDONTWRITEBYTECODE':'1'}
        receipt={'id':job['id'],'started':time.time(),'harness':job['harness'],'command':cmd}
        write_json(run/'controller/launch.json',receipt)
        with (run/'controller/launcher.log').open('a') as output:
            result=subprocess.run(cmd,env=env,stdout=output,stderr=subprocess.STDOUT)
        s=state(run);receipt.update(finished=time.time(),returncode=result.returncode,status=s['status'] if s else 'failed')
        write_json(run/'controller/launch.json',receipt)
        if result.returncode or not s or s['status']!='complete':raise RuntimeError(job['id']+': failed; inspect its controller/launcher.log')
        return receipt

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    q=sub.add_parser('plan');q.add_argument('--suite',choices=SUITES,required=True);q.add_argument('--profiles',nargs='+',required=True);q.add_argument('--games',nargs='+');q.add_argument('--seeds',nargs='+',type=int,default=[20260922]);q.add_argument('--harness',choices=['codex','claude'],default='codex');q.add_argument('--run-root',type=Path,default=ROOT/'runs');q.add_argument('--output',type=Path,required=True)
    q.add_argument('--reasoning-effort',choices=('max','high'),default='max',help='Reasoning effort requested by this experiment; default max')
    q=sub.add_parser('run');q.add_argument('--plan',type=Path,required=True);q.add_argument('--jobs',type=int,default=2);q.add_argument('--workers',type=int,default=8);q.add_argument('--seat-capacity',type=int,default=max(1,min(32,os.cpu_count() or 1)));q.add_argument('--admission-root',type=Path,default=ROOT/'runs/admission');q.add_argument('--catalog-root',type=Path);q.add_argument('--max-budget-usd',type=float);q.add_argument('--codex-binary',default='codex');q.add_argument('--claude-binary',default='claude')
    q=sub.add_parser('continuation-plan');q.add_argument('--run-dir',type=Path,required=True);q.add_argument('--harness',choices=['codex','claude'],default='codex');q.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if a.action=='plan':
        value=plan(a.suite,a.profiles,a.games,a.seeds,a.run_root,a.harness,a.reasoning_effort);write_json(a.output,value);print(f"Wrote {len(value['jobs'])} jobs to {a.output}");return
    if a.action=='continuation-plan':
        run=a.run_dir.resolve();s=state(run)
        if not s or s['status']!='complete' or (s['small_total'],s['large_total'])!=(128,16):raise ValueError('Continuation requires a completed 128/16 ledger')
        if s['game'] not in PAPER['games']:raise ValueError('Unknown paper game')
        metadata=json.loads(s['metadata_json'])
        saved_harness='claude' if metadata.get('harness')=='official-claude-code-agent-sdk' else 'codex'
        if saved_harness!=a.harness:raise ValueError('Continuation harness must match the original session')
        config=json.loads((run/'controller/experiment.json').read_text())
        model_identity=run/'controller/model-identity.json'
        effort=json.loads(model_identity.read_text())['reasoning_effort'] if model_identity.exists() else 'max'
        value={'schema_version':1,'suite':'continuation','jobs':[{'id':'continuation-'+run.name,'suite':'continuation','game':s['game'],'profile':s['model_profile'],'harness':a.harness,'run_dir':str(run),'config':config,'small_budget':256,'large_budget':32,'extend':True,'skip_baseline':True}]}
        value['jobs'][0]['reasoning_effort']=effort
        write_json(a.output,value);return
    if min(a.jobs,a.workers,a.seat_capacity)<1:p.error('jobs, workers and seat capacity must be positive')
    jobs=json.loads(a.plan.read_text())['jobs']
    if len({j['run_dir'] for j in jobs})!=len(jobs):raise ValueError('Duplicate run directories')
    limits={'pools':{'seat':{'capacity':a.seat_capacity,'small_reserve':min(4,a.seat_capacity)}}}
    limitfile=a.admission_root/'limits.json'
    if limitfile.exists() and json.loads(limitfile.read_text())!=limits:raise ValueError('Existing admission policy differs; choose a new admission root or explicitly manage the running queue')
    if not limitfile.exists():write_json(limitfile,limits)
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.jobs) as pool:
        futures={pool.submit(run_job,j,a):j['id'] for j in jobs};failed=[]
        for future in concurrent.futures.as_completed(futures):
            try:print(json.dumps(future.result()),flush=True)
            except Exception as e:failed.append(futures[future]);print(f'{futures[future]}: {e}',file=sys.stderr,flush=True)
    if failed:raise SystemExit(1)
if __name__=='__main__':main()

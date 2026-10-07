#!/usr/bin/env python3
"""Certify paper game resources and public starters without model API calls."""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
import json
import os
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
GAMES=json.loads((ROOT/'configs/paper.json').read_text())['games']

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--games',nargs='+',choices=GAMES,default=GAMES)
    p.add_argument('--jobs',type=int,default=2)
    p.add_argument('--output',type=Path,default=ROOT/'validation/games')
    p.add_argument('--static-only',action='store_true')
    a=p.parse_args()
    if a.jobs<1:p.error('--jobs must be positive')
    import aa_arena
    if Path(aa_arena.__file__).resolve() != ROOT/'src/aa_arena/__init__.py':
        p.error('Arena import is from another checkout; unset PYTHONPATH and install this package in the active environment')
    a.output.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ);env.pop('PYTHONPATH',None);env['PYTHONDONTWRITEBYTECODE']='1'
    def check(game):
        out=a.output/game;out.mkdir(parents=True,exist_ok=True)
        cmd=[sys.executable,'-m','aa_arena.cli','resources','certify','--game',game,'--output',str(out/'resources')]
        if a.static_only:cmd.append('--skip-live')
        started=time.time()
        with (out/'stdout.json').open('w') as stdout,(out/'stderr.log').open('w') as stderr:
            result=subprocess.run(cmd,env=env,cwd=ROOT,stdout=stdout,stderr=stderr)
        row={'game':game,'returncode':result.returncode,'live':not a.static_only,'model_calls':0,'started':started,'finished':time.time()}
        if result.returncode==0:
            row['certificate']=json.loads((out/'stdout.json').read_text())
        (out/'result.json').write_text(json.dumps(row,indent=2)+'\n')
        return row
    results=[]
    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        futures=[pool.submit(check,game) for game in a.games]
        for future in as_completed(futures):
            row=future.result();results.append(row);print(row['game'],row['returncode'],flush=True)
    report={'passed':all(r['returncode']==0 for r in results),'model_calls':0,'games':results}
    (a.output/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
    if not report['passed']:raise SystemExit(1)
if __name__=='__main__':main()

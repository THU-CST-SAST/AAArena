#!/usr/bin/env python3
"""Run real remote small/large matches with public SDKs, without model API calls."""
from __future__ import annotations
import argparse
import concurrent.futures
import json
from pathlib import Path
import time
import traceback
from aa_arena.benchmark.service import BenchmarkService
from aa_arena.benchmark.experiment import ExperimentConfig
from aa_arena.resources import ARENA_GAMES
from aa_arena.io import atomic_write_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--games', nargs='+', choices=ARENA_GAMES, default=list(ARENA_GAMES))
    p.add_argument('--output', type=Path, required=True, help='New directory, or resume this validation')
    p.add_argument('--jobs', type=int, default=4)
    p.add_argument('--large', action='store_true', help='Also evaluate every full game pool')
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)

    def check(game):
        record = {'game': game, 'passed': False, 'checks': []}
        started = time.time()
        try:
            for feedback in ('detailed', 'binary'):
                run = a.output / game / feedback
                ExperimentConfig(feedback=feedback).freeze(run/'controller/experiment.json')
                with BenchmarkService(run, game=game, model_profile='sdk-validation',
                                      small_budget=1, large_budget=1, workers=1) as service:
                    service.initialize_workspace()
                    assert service.remote, 'Validation requires formal remote distribution'
                    prior = next((s['result'] for s in service.ledger.submissions() if s['kind']=='small' and s.get('result')), None)
                    small = prior or service.small_match([service.matches.opponents[0].opponent_id])
                    if feedback == 'detailed':
                        assert small.get('seats') and all(s.get('rounds',0)>1 for s in small['seats']), 'No actual game rounds'
                        assert all(small.get(k,0)==0 for k in ('game_errors','candidate_errors','opponent_errors')), 'Small-match errors'
                        assert any((run/'workspace/replays').rglob('*.json')), 'Missing dense replay'
                    else:
                        assert not any((run/'workspace/replays').rglob('*.json')), 'Binary arm exposed dense replay'
                    assert service.ledger.state()['small_used']==1
                    record['checks'].append({'feedback': feedback, 'kind':'small', 'passed':True})
                    if a.large and feedback=='detailed':
                        prior = next((s['result'] for s in service.ledger.submissions() if s['kind']=='large' and s.get('result')), None)
                        large = prior or service.large_match()
                        assert large.get('elo') is not None and large.get('rank') is not None
                        assert service.ledger.state()['large_used']==1
                        record['checks'].append({'kind':'large','passed':True,'games':large.get('games'),'elo':large['elo'],'rank':large['rank'], 'candidate_errors':large.get('candidate_errors',0),'opponent_errors':large.get('opponent_errors',0),'game_errors':large.get('game_errors',0)})
                    record['pool_count'] = len(service.matches.opponents)
            record['passed'] = True
        except Exception:
            record['error'] = traceback.format_exc()
        record['seconds'] = round(time.time()-started,2)
        atomic_write_json(a.output / (game+'.json'), record)
        print(json.dumps(record),flush=True)
        return record

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,a.jobs)) as pool:
        rows = list(pool.map(check,a.games))
    summary={'passed':all(r['passed'] for r in rows), 'games':rows, 'model_calls':0}
    atomic_write_json(a.output/'summary.json',summary)
    raise SystemExit(0 if summary['passed'] else 1)

if __name__=='__main__':
    main()

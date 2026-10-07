#!/usr/bin/env python3
"""Upload a policy directory and poll a budgeted evaluation job."""
import argparse
import json
from pathlib import Path
import time
from aa_arena.benchmark.experiment import ExperimentConfig
from aa_arena.benchmark.remote import EvaluationClient, pack_strategy


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--game', required=True)
    p.add_argument('--strategy', type=Path, required=True)
    p.add_argument('--kind', choices=('small', 'large'), required=True)
    p.add_argument('--feedback', choices=('binary', 'detailed'), default='detailed')
    p.add_argument('--opponents', nargs='+', default=[])
    p.add_argument('--run-id', required=True)
    p.add_argument('--submission-id', required=True)
    p.add_argument('--seed', type=int, default=20260831)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--timeout', type=float, default=7200)
    a = p.parse_args()
    if a.kind == 'small' and not a.opponents:
        p.error('--opponents is required for small matches (use IDs returned by POST /v1/runs)')
    client = EvaluationClient()
    client.request('POST', '/v1/runs', {
        'run_id': a.run_id, 'game': a.game, 'small_total': 128, 'large_total': 16,
        'seed': a.seed, 'experiment': ExperimentConfig(feedback=a.feedback).as_dict(),
    })
    import re
    def identifier(value):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
            raise ValueError("Invalid run or submission ID")
    identifier(a.run_id)
    identifier(a.submission_id)
    prefix = '/v1/runs/' + a.run_id + '/jobs'
    result = client.request('POST', prefix, {
        'submission_id': a.submission_id, 'kind': a.kind,
        'files': pack_strategy(a.strategy), 'opponent_ids': a.opponents,
        'feedback': a.feedback, 'seed': a.seed, 'allow_repeats': False,
    })
    deadline = time.monotonic() + a.timeout
    while result['status'] in ('queued', 'running'):
        if time.monotonic() >= deadline:
            raise TimeoutError('Job remains on server; retry the same run and submission IDs.')
        time.sleep(2)
        result = client.request('GET', prefix + '/' + a.submission_id)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'submission_id': a.submission_id, 'status': result['status'], 'output': str(a.output)}))
    if result['status'] != 'complete':
        raise SystemExit(1)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Paid, bounded native auto-compaction acceptance with real remote matches."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
from aa_arena.benchmark.profile import load_profile
from aa_arena.benchmark.service import BenchmarkService
from aa_arena.io import atomic_write_json
from aa_arena.resources import ARENA_GAMES

FIRST = ('Interface acceptance only. Keep the SDK unchanged. Call workspace_manifest and '
         'list_opponents, then workspace_shell to write arena-io-ok to notes/acceptance.txt '
         'and read it back. Submit exactly one small_match against {opponent}. '
         'Stop after its result; no large match yet.')
LAST = ('Resume acceptance. Read notes/acceptance.txt using workspace_shell, call '
        'workspace_manifest and verify remaining small=0, large=1. Keep the SDK unchanged '
        'and submit exactly one large_match. Recover a transport-interrupted request with '
        'the same arguments; never repeat a completed match. Report its rank and stop.')


def completed(service, kind):
    return [r for r in service.ledger.submissions() if r['kind']==kind and r['status']=='complete']


def codex(a, profile, key):
    import aa_arena.benchmark.runtime as module
    original=module.write_codex_home
    def test_home(*args, **kwargs):
        result=original(*args, **kwargs)
        config=Path(args[0])/'config.toml'
        config.write_text(config.read_text().replace('model_auto_compact_token_limit = 200000',
                                                     'model_auto_compact_token_limit = 20000'))
        return result
    module.write_codex_home=test_home
    try:
        with BenchmarkService(a.run_dir,game=a.game,model_profile=a.model_profile,
                              small_budget=1,large_budget=1,workers=1) as service:
            service.initialize_workspace()
            with module.CodexArenaRuntime(service,profile,api_key=key,
                                          codex_binary=a.codex_binary,turn_timeout_s=300) as runtime:
                runtime.start()
                runtime.thread_id=runtime._start_thread(
                    'Run only the requested acceptance steps, preserving notes and match budgets.',
                    tools=module.experiment_tools(service.experiment))
                service.ledger.update_runtime(thread_id=runtime.thread_id)
                runtime._run_turn(FIRST.format(opponent=service.matches.opponents[0].opponent_id))
                assert completed(service,'small'), 'No completed small match'
                budget=service.ledger.budgets().as_dict()
                # Reduced threshold is confined to this acceptance run.
                runtime._run_turn('Ignore the disposable padding below. Call workspace_manifest once '
                                  'and then stop; do not submit matches.\n'+' diagnostic-padding'*25000)
                runtime._run_turn('Call workspace_manifest once and then stop. No matches.')
                assert service.ledger.budgets().as_dict()==budget
                assert 'automatic_compaction_completed' in (service.controller/'logs/runtime.log').read_text()
                thread=runtime.thread_id
                runtime.close_client_only();runtime.start_client_only()
                resumed=runtime.client.request('thread/resume',{'threadId':thread,'model':profile.model,
                    'modelProvider':profile.model_provider,'cwd':'/workspace','approvalPolicy':'never',
                    'excludeTurns':True,'permissions':'arena'})
                assert resumed['thread']['id']==thread
                runtime._run_turn(LAST)
                assert completed(service,'large'), 'No completed large match'
                runtime.run()  # Native final review and immutable champion snapshot.
                state=service.ledger.state()
                assert state['status']=='complete'
                return {'harness':'codex','automatic_compaction':True,'test_threshold':20000,
                        'production_threshold':200000,'resume_same_session':True,
                        'small_used':state['small_used'],'large_used':state['large_used'],
                        'complete':True,'manual_compact_called':False}
    finally:
        module.write_codex_home=original


async def claude(a, profile, key):
    from claude_agent_sdk import ClaudeSDKClient
    from aa_arena.benchmark.claude_runtime import ClaudeArenaRuntime, ServiceThread
    owner=ServiceThread()
    service=await owner.call(BenchmarkService,a.run_dir,game=a.game,model_profile=a.model_profile,
                             small_budget=1,large_budget=1,workers=1)
    try:
        await owner.call(service.initialize_workspace)
        runtime=ClaudeArenaRuntime(service,model=profile.model,base_url=profile.base_url,
            token=key,auth_mode=profile.claude_auth_mode,owner=owner,
            max_budget_usd=a.max_budget_usd,acceptance=True)
        async def options():
            value=await runtime.options()
            value.env['CLAUDE_CODE_AUTO_COMPACT_WINDOW']='100000'
            value.extra_args={'autocompact':'100k'}
            return value
        async with ClaudeSDKClient(options=await options()) as client:
            await runtime.turn(client,FIRST.format(opponent=service.matches.opponents[0].opponent_id))
            assert await owner.call(completed,service,'small')
            budget=await owner.call(service.budget_status)
            for _ in range(8):
                if runtime.compactions:break
                if runtime.usage_audit()[1] > a.max_budget_usd*.65:
                    raise RuntimeError('Acceptance allowance reached before automatic compaction')
                await runtime.turn(client,'Capacity test only: call workspace_shell exactly once with '
                    'python3 -c "print(\' \'.join(str(i) for i in range(6000)))" '
                    'then respond OK without quoting its output. This is disposable test data; '
                    'no matches or strategy edits.')
            assert any(v.get('compact_metadata',{}).get('trigger')=='auto' for v in runtime.compactions)
            assert await owner.call(service.budget_status)==budget
        first=await owner.call(service.ledger.state)
        async with ClaudeSDKClient(options=await options()) as client:
            await runtime.turn(client,LAST)
            assert await owner.call(completed,service,'large')
            await runtime.finalize(client)
        state=await owner.call(service.ledger.state)
        assert state['thread_id']==first['thread_id'] and state['status']=='complete'
        return {'harness':'claude','automatic_compaction':True,'test_window':100000,
                'production_window':200000,'resume_same_session':True,'complete':True,
                'small_used':state['small_used'],'large_used':state['large_used'],
                'manual_compact_called':False,'estimated_cost_usd':runtime.usage_audit()[1]}
    finally:
        await owner.call(service.close);owner.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--harness',choices=['codex','claude'],required=True)
    parser.add_argument('--game',choices=ARENA_GAMES,default='pacman')
    parser.add_argument('--model-profile',default='default')
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--codex-binary',default='codex')
    parser.add_argument('--max-budget-usd',type=float,default=2.0,
                        help='Claude CLI soft threshold only; use a provider spending cap for Codex')
    args=parser.parse_args()
    if args.run_dir.exists():parser.error('Use a new acceptance directory; do not reuse a paper run')
    if args.max_budget_usd<=0:parser.error('--max-budget-usd must be positive')
    profile=load_profile(args.model_profile)
    key=os.environ.get(profile.api_key_env)
    if not key:parser.error('Set the API key environment variable named by the profile')
    args.run_dir.mkdir(parents=True)
    atomic_write_json(args.run_dir/'acceptance.json',{'kind':'native-auto-compaction','paper_result':False})
    result=codex(args,profile,key) if args.harness=='codex' else asyncio.run(claude(args,profile,key))
    result['passed']=True
    atomic_write_json(args.run_dir/'acceptance.json',result)
    print(json.dumps(result))

if __name__=='__main__':main()

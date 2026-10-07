import asyncio
import json
import sqlite3
from types import SimpleNamespace
from aa_arena.benchmark.claude_runtime import ClaudeArenaRuntime
from aa_arena.benchmark.experiment import ExperimentConfig
import pytest


@pytest.mark.parametrize('mode,active,inactive', [
    ('api_key', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'),
    ('bearer', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY'),
])
def test_claude_auth_header_configuration_is_explicit_and_not_logged(tmp_path, monkeypatch, mode, active, inactive):
    service=SimpleNamespace(controller=tmp_path,experiment=ExperimentConfig(),
                            ledger=SimpleNamespace(state=lambda:{'thread_id':None}))
    runtime=ClaudeArenaRuntime(service,model='fixture',base_url='http://127.0.0.1',
                               token='fixture-credential',auth_mode=mode)
    monkeypatch.setattr(runtime,'prepare_working_tree',lambda:None)
    monkeypatch.setattr(runtime,'objective',lambda:'test')
    monkeypatch.setattr('aa_arena.benchmark.claude_runtime.official_claude_cli',lambda:'/fixture/claude')
    options=asyncio.run(runtime.options())
    assert options.env[active]=='fixture-credential'
    assert options.env[inactive]==''
    assert 'fixture-credential' not in runtime.events.read_text()
    assert json.loads(runtime.events.read_text())['auth_mode']==mode


def test_claude_auth_rejects_unknown_mode(tmp_path):
    with pytest.raises(ValueError,match='auth_mode'):
        ClaudeArenaRuntime(SimpleNamespace(controller=tmp_path),model='fixture',
                           base_url='http://127.0.0.1',token='fixture',auth_mode='guess')


def test_sdk_callback_keeps_sqlite_thread_and_budget_payload(tmp_path):
    db = sqlite3.connect(':memory:')
    db.execute('create table calls(name text)')
    payload = {'match_id':'abc','budget':{'small_remaining':127,'large_remaining':16}}
    marked = []
    def call(name, args):
        db.execute('insert into calls values(?)',(name,))
        return payload
    service = SimpleNamespace(controller=tmp_path,experiment=ExperimentConfig(),
                              model_tool_call=call,ledger=SimpleNamespace(mark_delivered=marked.append,state=lambda:{'status':'running'}))
    runtime = ClaudeArenaRuntime(service,model='test-model-e',base_url='https://example.invalid',token='test')
    tool = next(t for t in runtime.tools() if t.name=='small_match')
    result = asyncio.run(tool.handler({'opponent_ids':['public-opponent']}))
    assert json.loads(result['content'][0]['text']) == payload
    assert marked == ['abc']
    assert db.execute('select name from calls').fetchall()==[('small_match',)]


def test_frozen_bridge_denies_tools_without_touching_service(tmp_path):
    def forbidden(*args):raise AssertionError('frozen service called')
    service=SimpleNamespace(controller=tmp_path,experiment=ExperimentConfig(),model_tool_call=forbidden)
    runtime=ClaudeArenaRuntime(service,model='test-model-e',base_url='https://example.invalid',token='test')
    runtime.final=True
    result=asyncio.run(runtime.tools()[0].handler({}))
    assert result['isError'] and not runtime.calls


def test_service_thread_keeps_sqlite_owner_without_blocking_sdk(tmp_path):
    from aa_arena.benchmark.claude_runtime import ServiceThread
    import time
    import threading
    async def scenario():
        owner = ServiceThread()
        db = await owner.call(sqlite3.connect, ':memory:')
        await owner.call(db.execute, 'create table calls(name text)')
        def blocking():
            db.execute("insert into calls values('real-match')")
            time.sleep(.15)
            return threading.get_ident()
        async def transport():
            await asyncio.sleep(.025)
            return not task.done()
        task = asyncio.create_task(owner.call(blocking))
        assert await transport(), 'SDK transport was blocked by the match'
        identity = await task
        assert identity != threading.get_ident()
        assert await owner.call(lambda: db.execute('select name from calls').fetchall()) == [('real-match',)]
        await owner.call(db.close)
        owner.close()
    asyncio.run(scenario())


def test_working_sdk_is_writable_but_resources_and_frozen_runs_stay_readonly(tmp_path):
    for name in ['strategy', 'skills', 'notes', 'replays', 'artifacts', 'resources']:
        root=tmp_path/name;root.mkdir();(root/'sample.py').write_text('pass\n')
        (root/'sample.py').chmod(0o444);root.chmod(0o555)
    state={'status':'running'}
    service=SimpleNamespace(controller=tmp_path,workspace=tmp_path,ledger=SimpleNamespace(state=lambda:state))
    runtime=ClaudeArenaRuntime(service,model='test-model-e',base_url='unused',token='unused')
    runtime.prepare_working_tree()
    assert (tmp_path/'strategy').stat().st_mode & 0o200
    assert (tmp_path/'strategy/sample.py').stat().st_mode & 0o200
    assert not (tmp_path/'resources/sample.py').stat().st_mode & 0o200
    (tmp_path/'strategy/sample.py').chmod(0o444)
    state['status']='complete'
    runtime.prepare_working_tree()
    assert not (tmp_path/'strategy/sample.py').stat().st_mode & 0o200


def test_accounting_includes_compaction_and_separate_cli_processes(tmp_path):
    service = SimpleNamespace(controller=tmp_path)
    runtime = ClaudeArenaRuntime(service, model='test-model-e', base_url='unused', token='unused')
    def result(cost, inputs, outputs):
        return {'type':'sdk_event','event':{'total_cost_usd':cost,'usage':{},
                'model_usage':{'test-model-e':{'inputTokens':inputs,'outputTokens':outputs}}}}
    rows = [{'type':'harness_config'},result(.1, 10, 20),result(.2, 20, 30),
            {'type':'harness_config'},result(.05, 5, 6),result(.07, 7, 8)]
    runtime.events.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    usage,cost=runtime.usage_audit()
    assert abs(cost-.27)<1e-8
    assert usage['input_tokens']==27 and usage['output_tokens']==38


def test_committed_receipt_recovery_does_not_evaluate_or_spend_budget(tmp_path):
    order=[]
    payload={'match_id':'committed','budget':{'small_remaining':0,'large_remaining':0},'rank':4}
    def forbidden(*args):raise AssertionError('receipt recovery submitted another match')
    ledger=SimpleNamespace(state=lambda:{'status':'finalizing'},submissions=lambda:[{'kind':'large','status':'complete','result':payload}],mark_delivered=lambda _:order.append('delivered'))
    service=SimpleNamespace(controller=tmp_path,experiment=ExperimentConfig(),ledger=ledger,model_tool_call=forbidden,agent_result=lambda r,**kwargs:r)
    runtime=ClaudeArenaRuntime(service,model='test-model-e',base_url='unused',token='unused')
    runtime.recover_receipt=True
    def freeze():
        order.append('frozen');runtime.final=True
    runtime.freeze=freeze
    large=next(t for t in runtime.tools() if t.name=='large_match')
    async def scenario():
        result=await large.handler({})
        assert json.loads(result['content'][0]['text'])==payload
        assert order==['frozen','delivered']
        assert (await large.handler({}))['isError']
    asyncio.run(scenario())


def test_formal_experiment_requires_explicit_cost_ceiling():
    import pytest
    from aa_arena.benchmark.claude_runtime import resolve_cost_ceiling
    with pytest.raises(ValueError, match='explicit'):
        resolve_cost_ceiling(None, test_mode=False)
    assert resolve_cost_ceiling(None, test_mode=True)==1.0
    assert resolve_cost_ceiling(12.5, test_mode=False)==12.5
    for value in [0, -1, float('inf'), float('nan')]:
        with pytest.raises(ValueError):
            resolve_cost_ceiling(value, test_mode=False)


def test_claude_clone_final_adaptation_is_durable_and_separate(tmp_path):
    state={'status':'running','metadata':{}}
    calls=[]
    def update_runtime(**kwargs):state.update(kwargs)
    def freeze():
        assert state['metadata']['clone_adaptation_complete']
        state['status']='finalizing';calls.append('freeze')
    ledger=SimpleNamespace(state=lambda:state,pending_submission=lambda:None,
        submissions=lambda:[{'status':'complete','result':{'wins':1}} for _ in range(32)],
        update_runtime=update_runtime,undelivered_results=lambda:[],mark_delivered=lambda _:None)
    service=SimpleNamespace(controller=tmp_path,ledger=ledger,agent_result=lambda r:r,
        _preflight=lambda:calls.append('preflight'),finalize_clone=freeze)
    runtime=ClaudeArenaRuntime(service,model='test',base_url='https://example.invalid',token='test')
    async def turn(client,prompt):
        assert '32' in prompt;calls.append('adapt')
    runtime.turn=turn
    asyncio.run(runtime.finish_clone_learning(None))
    assert calls==['adapt','preflight','freeze']
    state['status']='running';calls.clear()
    asyncio.run(runtime.finish_clone_learning(None))
    assert calls==['preflight','freeze']

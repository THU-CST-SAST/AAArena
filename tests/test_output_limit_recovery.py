import json
from dataclasses import replace
from types import SimpleNamespace
import pytest
from aa_arena.benchmark.runtime import CodexArenaRuntime
from aa_arena.benchmark.profile import load_profile, write_codex_home
from aa_arena.benchmark.credential_proxy import Handler

ERROR = "Codex turn failed: stream disconnected before completion: Incomplete response returned, reason: max_output_tokens"

def runtime():
    r=object.__new__(CodexArenaRuntime)
    r.thread_id='same-thread';r._diagnostic=lambda _:None
    return r

def test_incomplete_continues_same_thread_and_preserves_final_flag():
    r=runtime();calls=[]
    def once(prompt, *, final):
        calls.append((r.thread_id,prompt,final))
        if len(calls)==1:raise RuntimeError(ERROR)
        return {'status':'completed'}
    r._run_turn_once=once
    assert r._run_turn('review',final=True)=={'status':'completed'}
    assert len(calls)==2 and all(c[0]=='same-thread' and c[2] for c in calls)
    assert '冻结' in calls[-1][1]

def test_persistent_incomplete_is_bounded_and_not_success():
    r=runtime();calls=[]
    def once(*args,**kw):
        calls.append(1);raise RuntimeError(ERROR)
    r._run_turn_once=once
    with pytest.raises(RuntimeError,match='max_output_tokens'):r._run_turn('work')
    assert len(calls)==3

def test_other_errors_do_not_retry():
    r=runtime();calls=[]
    def once(*args,**kw):
        calls.append(1);raise RuntimeError('Arrearage')
    r._run_turn_once=once
    with pytest.raises(RuntimeError,match='Arrearage'):r._run_turn('work')
    assert len(calls)==1

def test_context_limit_compacts_same_thread_before_retry():
    r=runtime();calls=[];compactions=[]
    def once(*args,**kw):
        calls.append(r.thread_id)
        if len(calls)==1:raise RuntimeError('context_length_exceeded')
        return {'status':'completed'}
    r._run_turn_once=once
    r._native_compact=lambda **kw:compactions.append(r.thread_id)
    assert r._run_turn('work')['status']=='completed'
    assert calls==['same-thread','same-thread'] and compactions==['same-thread']

def test_context_compaction_failure_remains_failed():
    r=runtime()
    def once(*args,**kw):raise RuntimeError('maximum context length')
    def compact(**kw):raise RuntimeError('native compact failed')
    r._run_turn_once=once;r._native_compact=compact
    with pytest.raises(RuntimeError,match='native compact failed'):r._run_turn('work')

def test_stream_retries_follow_explicit_configuration_not_profile_name(tmp_path):
    profile = load_profile('test-model-d')
    assert 'stream_max_retries' not in write_codex_home(tmp_path/'default', profile).read_text()
    configured = replace(profile, stream_max_retries=0)
    assert 'stream_max_retries = 0' in write_codex_home(tmp_path/'configured', configured).read_text()
    renamed = replace(configured, name='another-profile', model='another-model')
    assert 'stream_max_retries = 0' in write_codex_home(tmp_path/'renamed', renamed).read_text()

@pytest.mark.parametrize('value', [-1, True, '0', 0.5])
def test_invalid_stream_retry_configuration_is_rejected(value):
    with pytest.raises(ValueError, match='stream_max_retries'):
        replace(load_profile('default'), stream_max_retries=value)

def test_incomplete_usage_audit_is_numeric_and_private():
    event={'type':'response.incomplete','response':{'usage':{'input_tokens':100,'output_tokens':32,'total_tokens':132,'private':'secret'},'output':['secret'],'incomplete_details':{'reason':'max_output_tokens'}}}
    rows=Handler._terminal_usage(b'data: '+json.dumps(event).encode()+b'\n\ndata: [DONE]\n')
    assert rows[0]['usage']['total_tokens']==132 and rows[0]['output_limited']
    assert 'secret' not in json.dumps(rows)

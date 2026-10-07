from types import SimpleNamespace
import queue
import pytest
import aa_arena.benchmark.runtime as module
from aa_arena.benchmark.runtime import CodexArenaRuntime
from aa_arena.benchmark.protocol import JsonRpcStdioClient

def runtime_for(monkeypatch, steps, tool_seconds=0, tool_error=None):
    clock=[0.0]; events=[]
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    class Client:
        def request(self,*_args):return {}
        def next_message(self,timeout):
            assert timeout>0
            advance,message=steps.pop(0);clock[0]+=advance
            if isinstance(message,Exception):raise message
            return message
    r=object.__new__(CodexArenaRuntime)
    r.client=Client();r.thread_id='same-thread';r.profile=SimpleNamespace(reasoning_effort='high');r.turn_timeout_s=2
    r.service=SimpleNamespace(ledger=SimpleNamespace(update_runtime=lambda **_:None))
    r._resume_active=lambda:events.append('resume');r._pause_active=lambda:events.append('pause')
    def dispatch(*args,**kwargs):
        clock[0]+=tool_seconds
        if tool_error:raise tool_error
    r._dispatch_tool=dispatch
    return r,events

def call(r):return getattr(r,'_run_turn_once',r._run_turn)('continue')
def tool():return {'id':1,'method':'item/tool/call','params':{'tool':'large_match'}}
def completed():return {'method':'turn/completed','params':{'turn':{'id':'t','status':'completed'}}}

@pytest.mark.parametrize('duration',[0,100,100000])
def test_real_model_deadline_excludes_long_match_wait(monkeypatch,duration):
    r,events=runtime_for(monkeypatch,[(.25,tool()),(.25,completed())],tool_seconds=duration)
    assert call(r)['status']=='completed'
    assert events==['resume','pause','resume','pause']

def test_poll_expiry_does_not_end_a_slow_but_in_budget_model_call(monkeypatch):
    r,_=runtime_for(monkeypatch,[(.5,TimeoutError()),(.5,completed())])
    assert call(r)['status']=='completed'

def test_model_deadline_still_applies_across_tool_calls(monkeypatch):
    r,_=runtime_for(monkeypatch,[(.5,tool()),(.5,tool()),(1.1,TimeoutError())],tool_seconds=100)
    with pytest.raises(TimeoutError,match='active wall-time'):call(r)

def test_failed_tool_releases_the_active_time_pause(monkeypatch):
    r,events=runtime_for(monkeypatch,[(.25,tool())],tool_seconds=100,tool_error=ValueError('recover accepted match'))
    with pytest.raises(ValueError,match='recover accepted'):call(r)
    assert events==['resume','pause','resume']

def test_queue_timeout_is_a_timeout_not_the_block_flag():
    seen=[]
    class Queue:
        def get(self,*,timeout):seen.append(timeout);raise queue.Empty
    client=object.__new__(JsonRpcStdioClient);client.messages=Queue();client._closed=False
    with pytest.raises(TimeoutError):client.next_message(.01)
    assert seen==[.01]

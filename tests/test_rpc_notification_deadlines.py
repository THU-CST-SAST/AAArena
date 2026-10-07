import queue,threading,time
from types import SimpleNamespace
import pytest
from aa_arena.benchmark.protocol import JsonRpcStdioClient, AppServerProtocolError
from aa_arena.benchmark.runtime import CodexArenaRuntime

def test_empty_notification_queue_actually_times_out():
    client=object.__new__(JsonRpcStdioClient);client.messages=queue.Queue();client._closed=False
    result=[]
    def wait():
        try:client.next_message(.02)
        except Exception as error:result.append(error)
    worker=threading.Thread(target=wait,daemon=True);worker.start();worker.join(.5)
    assert not worker.is_alive(), 'timeout was accidentally passed as Queue.get(block)'
    assert len(result)==1 and isinstance(result[0],TimeoutError)

def test_closed_reader_fails_without_waiting_for_whole_turn():
    client=object.__new__(JsonRpcStdioClient);client.messages=queue.Queue();client._closed=True
    with pytest.raises(AppServerProtocolError,match='closed'):client.next_message(.001)

def test_compact_survives_a_poll_timeout_but_handles_failure_event():
    runtime=object.__new__(CodexArenaRuntime);runtime.thread_id='same-thread';runtime._diagnostic=lambda _:None
    events=iter([TimeoutError(),{'method':'error','params':{'error':{'message':'compact rejected'}}}])
    def next_message(timeout):
        value=next(events)
        if isinstance(value,Exception):raise value
        return value
    runtime.client=SimpleNamespace(request=lambda *_:None,next_message=next_message,_redact=lambda s:s)
    with pytest.raises(RuntimeError,match='compact rejected'):runtime._native_compact(timeout_s=1)

def test_turn_poll_timeout_is_not_the_turn_deadline():
    runtime=object.__new__(CodexArenaRuntime);runtime.thread_id='same-thread';runtime.turn_timeout_s=1
    runtime.profile=SimpleNamespace(reasoning_effort='high')
    runtime._resume_active=lambda:None;runtime._pause_active=lambda:None
    events=iter([TimeoutError(),{'method':'turn/completed','params':{'turn':{'status':'completed'}}}])
    def next_message(timeout):
        value=next(events)
        if isinstance(value,Exception):raise value
        return value
    runtime.client=SimpleNamespace(request=lambda *_:None,next_message=next_message)
    assert runtime._run_turn_once('continue')['status']=='completed'

def test_compaction_ignores_the_goal_turn_it_replaces():
    runtime=object.__new__(CodexArenaRuntime);runtime.thread_id='same-thread';runtime._diagnostic=lambda _:None
    events=iter([
        {'method':'turn/completed','params':{'turn':{'id':'goal-follow-up','status':'interrupted'}}},
        {'method':'item/completed','params':{'turnId':'compact','item':{'type':'contextCompaction'}}},
    ])
    runtime.client=SimpleNamespace(request=lambda *_:None,next_message=lambda _:next(events),_redact=lambda s:s)
    runtime._native_compact(timeout_s=1)

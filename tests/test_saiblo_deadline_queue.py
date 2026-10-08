"""Queued referee transitions must not become player timeouts under load."""
import json
import sys
import time
from pathlib import Path

import pytest

from aa_arena.saiblo import judger
from aa_arena.sandbox import DirectLauncher, ProcessSpec


@pytest.mark.parametrize('transition', ['terminal', 'next_round', 'late_player'])
def test_dispatch_delay_respects_frame_arrival(tmp_path: Path, monkeypatch, transition):
    backend = tmp_path / 'backend.py'
    backend.write_text('''import json,struct,sys

def exact(n):
 out=b''
 while len(out)<n:
  x=sys.stdin.buffer.read(n-len(out))
  if not x: raise EOFError
  out+=x
 return out

def read(): return json.loads(exact(struct.unpack('>I',exact(4))[0]))
def send(d):
 b=json.dumps(d).encode();sys.stdout.buffer.write(struct.pack('>Ii',len(b),-1)+b);sys.stdout.buffer.flush()
read()
send({'state':0,'time':1,'length':2048})
send({'state':1,'listen':[0],'player':[0],'content':['go\\n']})
x=read()
''' + ("assert x['player']==-1 and json.loads(x['content'])['error']==1\n" if transition == 'late_player' else "assert x['player']==0 and x['content']=='ok'\n") + ("send({'state':2,'listen':[0],'player':[0],'content':['go\\n']})\nx=read();assert x['player']==0 and x['content']=='ok'\n" if transition == 'next_round' else '') + "send({'state':-1,'end_info':{'0':1,'1':0}})\n")
    player = tmp_path / 'player.py'
    player.write_text("import sys,struct,time\nfor line in sys.stdin.buffer:\n " + ('time.sleep(1.1)\n ' if transition == 'late_player' else '') + "sys.stdout.buffer.write(struct.pack('>I',2)+b'ok');sys.stdout.buffer.flush()\n")
    survivor = tmp_path / 'survivor.py'
    survivor.write_text('import sys;sys.stdin.buffer.read()\n')
    original_write = judger._write
    delayed = False

    def pause_controller(stream, payload):
        nonlocal delayed
        original_write(stream, payload)
        should_pause = payload == b'go\n' if transition == 'late_player' else False
        if transition != 'late_player' and len(payload) > 4:
            try:
                d = json.loads(payload[4:])
                should_pause = d.get('player') == 0 and d.get('content') == 'ok'
            except (ValueError, AttributeError):
                pass
        if should_pause and not delayed:
            delayed = True
            time.sleep(1.35)  # Referee reader remains active during controller scheduling delay.

    monkeypatch.setattr(judger, '_write', pause_controller)
    result = judger.run_stdio_match(
        backend=ProcessSpec((sys.executable, str(backend)), tmp_path),
        players=tuple(ProcessSpec((sys.executable, str(p)), tmp_path) for p in [player, survivor]),
        config={}, replay_path=tmp_path/'replay.json', events_path=tmp_path/'events.jsonl',
        timeout_s=8, player_launcher=DirectLauncher(),
    )
    assert delayed
    assert result.scores == (1, 0)
    errors = [e for line in (tmp_path/'events.jsonl').read_text().splitlines()
              if (e := json.loads(line))['kind'] == 'ai_error']
    if transition == 'late_player':
        assert len(errors) == 1 and errors[0]['error_log'] == 'timeOutError'
    else:
        assert errors == []

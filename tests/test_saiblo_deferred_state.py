"""Delayed output cannot be carried into a different referee state."""
import json
import sys
from pathlib import Path
import pytest
from aa_arena.saiblo.judger import run_stdio_match
from aa_arena.sandbox import DirectLauncher, ProcessSpec

@pytest.mark.parametrize('mode', ['stale', 'same_state', 'initial_direct', 'listen_only_next_state'])
def test_deferred_output_obeys_state_boundary(tmp_path: Path, mode):
    backend = tmp_path/'backend.py'
    backend.write_text('''import json,struct,sys,time

def exact(n):
 b=b''
 while len(b)<n:
  x=sys.stdin.buffer.read(n-len(b))
  if not x:raise EOFError
  b+=x
 return b

def read():return json.loads(exact(struct.unpack('>I',exact(4))[0]))
def send(d,target=-1):
 b=(json.dumps(d) if isinstance(d,dict) else d).encode();sys.stdout.buffer.write(struct.pack('>Ii',len(b),target)+b);sys.stdout.buffer.flush()
read();send({'state':0,'time':2,'length':2048})
''' + ("send('old\\n',0)\n" if mode=='initial_direct' else "send({'state':1,'listen':[],'player':[0],'content':['old\\n']})\n") + '''time.sleep(0.2)
''' + ("send({'state':2,'listen':[0],'player':[0],'content':['new\\n']})\n" if mode=='stale' else "send({'state':2,'listen':[0],'player':[],'content':[]})\n" if mode=='listen_only_next_state' else "send({'state':1,'listen':[0],'player':[],'content':[]})\n") + "x=read();assert x['player']==0 and x['content']==" + repr('new' if mode=='stale' else 'old') + ",x\nsend({'state':-1,'end_info':{'0':1,'1':0}})\n")
    player=tmp_path/'player.py'
    player.write_text('''import sys,struct
for line in sys.stdin.buffer:
 b=line.strip();sys.stdout.buffer.write(struct.pack('>I',len(b))+b);sys.stdout.buffer.flush()
''')
    survivor=tmp_path/'survivor.py';survivor.write_text('import sys;sys.stdin.buffer.read()\n')
    result=run_stdio_match(backend=ProcessSpec((sys.executable,str(backend)),tmp_path),players=tuple(ProcessSpec((sys.executable,str(p)),tmp_path) for p in [player,survivor]),config={},replay_path=tmp_path/'replay.json',events_path=tmp_path/'events.jsonl',timeout_s=5,player_launcher=DirectLauncher())
    assert result.scores==(1,0)
    events=[json.loads(x) for x in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert any(x['kind']=='defer_ai_output' for x in events)
    assert not any(x['kind']=='ai_error' for x in events)
    discarded=[x for x in events if x['kind']=='discard_stale_ai_output']
    assert bool(discarded)==(mode=='stale')

"""A terminal notification can make player EOF arrive before referee GameOver."""
import json
import sys
from pathlib import Path

import pytest

from aa_arena.saiblo.judger import run_stdio_match
from aa_arena.sandbox import DirectLauncher, ProcessSpec


@pytest.mark.parametrize('mode', ['terminal', 'required_next', 'required_now'])
def test_player_exit_is_resolved_against_referee_reply_requests(tmp_path: Path, mode):
    backend = tmp_path / 'backend.py'
    backend.write_text('''import json,struct,sys,time

def exact(n):
 b=b''
 while len(b)<n:
  x=sys.stdin.buffer.read(n-len(b))
  if not x: raise EOFError
  b+=x
 return b

def read(): return json.loads(exact(struct.unpack('>I',exact(4))[0]))
def send(d):
 b=json.dumps(d).encode();sys.stdout.buffer.write(struct.pack('>Ii',len(b),-1)+b);sys.stdout.buffer.flush()
read()
send({'state':0,'time':2,'length':2048})
send({'state':1,'listen':[0],'player':[0],'content':['go\\n']})
x=read();assert x['player']==0 and x['content']=='ok'
''' + "send({'state':2,'listen':" + ('[0]' if mode == 'required_now' else '[]') + ",'player':[0],'content':['quit\\n']})\n" + '''time.sleep(0.25)
''' + ("send({'state':3,'listen':[0],'player':[0],'content':['go\\n']})\n" if mode == 'required_next' else '') + ("x=read();assert x['player']==-1 and json.loads(x['content'])['error']==0\n" if mode != 'terminal' else '') + "send({'state':-1,'end_info':{'0':1,'1':0}})\n")
    player = tmp_path / 'player.py'
    player.write_text('''import sys,struct
for line in sys.stdin.buffer:
 if line==b'quit\n': break
 sys.stdout.buffer.write(struct.pack('>I',2)+b'ok');sys.stdout.buffer.flush()
'''.replace("b'quit\n'", "b'quit\\n'"))
    survivor = tmp_path / 'survivor.py'
    survivor.write_text('import sys;sys.stdin.buffer.read()\n')
    result = run_stdio_match(
        backend=ProcessSpec((sys.executable, str(backend)), tmp_path),
        players=tuple(ProcessSpec((sys.executable, str(p)), tmp_path) for p in [player, survivor]),
        config={}, replay_path=tmp_path/'replay.json', events_path=tmp_path/'events.jsonl',
        timeout_s=6, player_launcher=DirectLauncher(),
    )
    assert result.scores == (1, 0)
    events = [json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    errors = [e for e in events if e['kind']=='ai_error']
    if mode == 'terminal':
        assert errors == []
        assert any(e['kind']=='ai_exit_deferred' and e['player']==0 for e in events)
    else:
        assert len(errors)==1 and errors[0]['player']==0 and errors[0]['error_log']=='runError'
        assert errors[0]['state'] == (3 if mode=='required_next' else 2)

import multiprocessing as mp
import json,os,sqlite3,time
from pathlib import Path
from aa_arena.benchmark.admission import Gate

def work(root, events, n):
 with Gate(root).lease('seat',str(n%3), 'small' if n%4==0 else 'large'):
  events.put(('start',n,time.monotonic()));time.sleep(.12);events.put(('end',n,time.monotonic()))

def crash(root):
 with Gate(root).lease('seat','crash'):os._exit(3)

def test_cap_and_completion(tmp_path):
 (tmp_path/'limits.json').write_text(json.dumps({'dispatch_waiters':True,'pools':{'seat':{'capacity':3,'small_reserve':1}}}))
 Gate(tmp_path);ctx=mp.get_context('fork');q=ctx.Queue();ps=[ctx.Process(target=work,args=(tmp_path,q,i)) for i in range(15)]
 for p in ps:p.start()
 events=[q.get(timeout=20) for _ in range(30)]
 for p in ps:p.join(10);assert p.exitcode==0
 active=0;peak=0
 for kind,n,at in sorted(events,key=lambda e:e[2]):
  active+=1 if kind=='start' else -1;peak=max(peak,active);assert 0<=active<=3
 assert peak==3 and active==0
 with Gate(tmp_path).connect() as c:assert c.execute('select count(*) from tickets').fetchone()[0]==0

def test_crash_does_not_recycle_unverified_descendants(tmp_path):
 (tmp_path/'limits.json').write_text(json.dumps({'dispatch_waiters':True,'pools':{'seat':{'capacity':1}}}))
 Gate(tmp_path);ctx=mp.get_context('fork');p=ctx.Process(target=crash,args=(tmp_path,));p.start();p.join(10);assert p.exitcode==3
 with Gate(tmp_path).connect() as c:assert c.execute("select count(*) from tickets where state='active'").fetchone()[0]==1


def test_poll_connections_are_closed_without_garbage_collection(tmp_path):
 import gc
 (tmp_path/'limits.json').write_text(json.dumps({'dispatch_waiters':True,'pools':{'seat':{'capacity':1}}}))
 gate=Gate(tmp_path)
 gc.collect();gc.disable()
 try:
  before=len(list(Path('/proc/self/fd').iterdir()))
  for _ in range(250):
   with gate.lease('seat','test'):pass
  after=len(list(Path('/proc/self/fd').iterdir()))
  assert after<=before+3
 finally:gc.enable()

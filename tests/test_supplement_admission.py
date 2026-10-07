import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from aa_arena.benchmark.matches import MatchService


def test_match_workers_share_host_capacity(tmp_path, monkeypatch):
    monkeypatch.setenv('AA_ARENA_ADMISSION_ROOT',str(tmp_path))
    monkeypatch.setenv('AA_ARENA_JOB_ID','test-cohort')
    (tmp_path/'limits.json').write_text(json.dumps({'dispatch_waiters':True,'pools':{'seat':{'capacity':2}}}))
    service=object.__new__(MatchService);service.game='rollman'
    lock=threading.Lock();counts={'active':0,'peak':0}
    def evaluate(*args):
        with lock:
            counts['active']+=1
            counts['peak']=max(counts['peak'],counts['active'])
        time.sleep(.08)
        with lock:counts['active']-=1
        return args[-1]
    service._evaluate_seat_unadmitted=evaluate
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures=[pool.submit(service._evaluate_seat,None,None,None,0,str(i)) for i in range(8)]
        results=[f.result(timeout=10) for f in futures]
    assert results==list(map(str,range(8)))
    assert 1<=counts['peak']<=2 and counts['active']==0

def test_small_match_marks_its_own_admission_kind(tmp_path, monkeypatch):
    from aa_arena.benchmark.matches import MatchService, Opponent
    service=object.__new__(MatchService)
    service.game='pacman'; service.run_root=tmp_path; service.public_practice=False
    opponent=Opponent('example',1000,1,tmp_path)
    service.validate_opponents=lambda *a,**k:[opponent]
    observed=[]
    def run(self,*args):
        observed.append(getattr(self,'_admission_kind','large'))
        return []
    monkeypatch.setattr(MatchService,'_run',run)
    service.small_match(tmp_path,['example'],'small',tmp_path/'replays',feedback='binary')
    assert observed==['small']
    assert not hasattr(service,'_admission_kind')

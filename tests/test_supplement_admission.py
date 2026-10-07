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

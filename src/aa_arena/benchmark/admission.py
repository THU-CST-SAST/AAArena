"""Host-local fair admission with durable leases and fail-closed crash handling.

The database is private controller state. Dead active owners are NOT reaped:
external supervision must verify sandbox descendants have exited before cleanup.
"""
from contextlib import contextmanager
from pathlib import Path
import fcntl, json, os, sqlite3, time, uuid


def identity():
    return str(os.getpid())+':'+Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')',1)[1].split()[19]+':'+Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def is_alive(owner):
    try:
        pid,tick,boot=owner.split(':')
        row=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        return row[0] not in ('Z','X') and row[19]==tick and Path('/proc/sys/kernel/random/boot_id').read_text().strip()==boot
    except (OSError,ValueError,IndexError):return False


class Gate:
    def __init__(self,root):
        self.root=Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        # Serialize schema/WAL setup separately from admission transactions.
        with (self.root/'schema.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            with self.connect() as c:
                if c.execute('pragma journal_mode').fetchone()[0].lower() != 'wal':
                    c.execute('pragma journal_mode=WAL')
                c.executescript('''CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY,pool TEXT,grp TEXT,kind TEXT,owner TEXT,created REAL,state TEXT,started REAL,detail TEXT);
    CREATE TABLE IF NOT EXISTS turns(pool TEXT,grp TEXT,last REAL,PRIMARY KEY(pool,grp));
    CREATE INDEX IF NOT EXISTS queue ON tickets(pool,state,created);''')
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.root/'admission.sqlite3',timeout=30)
        c.row_factory=sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()
    def config(self):return json.loads((self.root/'limits.json').read_text())
    @contextmanager
    def lease(self,pool,group,kind='large',detail=''):
        token=uuid.uuid4().hex;owner=identity();leased=False
        with self.connect() as c:c.execute('insert into tickets values(?,?,?,?,?,?,?,NULL,?)',(token,pool,group,kind,owner,time.time(),'waiting',detail))
        try:
            while not leased:
                cfg=self.config()['pools'][pool];cap=int(cfg['capacity']);reserve=int(cfg.get('small_reserve',0))
                with self.connect() as c:
                    c.execute('begin immediate')
                    wait=c.execute("select * from tickets where pool=? and state='waiting' order by created",(pool,)).fetchall()
                    living={owner:is_alive(owner) for owner in {r['owner'] for r in wait}}
                    for row in wait:
                        if not living[row['owner']]:c.execute('delete from tickets where id=?',(row['id'],))
                    wait=[r for r in wait if living[r['owner']]]
                    active=c.execute("select * from tickets where pool=? and state='active'",(pool,)).fetchall()
                    if self.config().get('dispatch_waiters',False):
                        # Any waiter can dispatch available seats. Requiring the
                        # chosen waiter to win the SQLite lock itself causes severe
                        # head-of-line blocking with hundreds of contenders.
                        turns={r['grp']:r['last'] for r in c.execute('select * from turns where pool=?',(pool,))}
                        active_small=sum(r['kind']=='small' for r in active)
                        for _ in range(max(0,cap-len(active))):
                            if not wait:break
                            eligible=[r for r in wait if r['kind']=='small'] if active_small<reserve and any(r['kind']=='small' for r in wait) else wait
                            chosen=min(eligible,key=lambda r:(turns.get(r['grp'],0),r['created']))
                            now=time.time()
                            c.execute("update tickets set state='active',started=? where id=?",(now,chosen['id']))
                            c.execute('insert into turns values(?,?,?) on conflict(pool,grp) do update set last=excluded.last',(pool,chosen['grp'],now))
                            turns[chosen['grp']]=now
                            active_small+=chosen['kind']=='small'
                            wait.remove(chosen)
                        row=c.execute('select state from tickets where id=?',(token,)).fetchone()
                        leased=bool(row and row['state']=='active')
                    else:
                        if len(active)<cap and wait:
                            active_small=sum(r['kind']=='small' for r in active)
                            eligible=[r for r in wait if r['kind']=='small'] if active_small<reserve and any(r['kind']=='small' for r in wait) else wait
                            turns={r['grp']:r['last'] for r in c.execute('select * from turns where pool=?',(pool,))}
                            chosen=min(eligible,key=lambda r:(turns.get(r['grp'],0),r['created']))
                            if chosen['id']==token:
                                now=time.time();c.execute("update tickets set state='active',started=? where id=?",(now,token))
                                c.execute('insert into turns values(?,?,?) on conflict(pool,grp) do update set last=excluded.last',(pool,group,now));leased=True
                if not leased:time.sleep(.2)
            yield token
        finally:
            with self.connect() as c:
                # Normal unwinding is after evaluator's process-tree cleanup.
                c.execute('delete from tickets where id=?',(token,))


@contextmanager
def admission(pool,kind='large',detail='',group=None):
    root=os.environ.get('AA_ARENA_ADMISSION_ROOT')
    if not root:
        yield None;return
    group=group or os.environ.get('AA_ARENA_JOB_ID','validation')
    with Gate(root).lease(pool,group,kind,str(detail)) as token:yield token

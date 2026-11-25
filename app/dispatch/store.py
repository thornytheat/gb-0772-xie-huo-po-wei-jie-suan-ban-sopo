from contextlib import closing,contextmanager
from datetime import UTC,datetime
import json
from pathlib import Path
import sqlite3
import threading

from app.archive.canonical import digest
from app.curb.timeaxis import M, minute
from app.problems import DocketError

RELEASE_STATES=('PUBLISHED','BLOCKED','REVOKED')


class ReleaseStore:
    """放行凭据、分钟台账与补报事件的单库台账。

    所有写入均为幂等 INSERT OR IGNORE：自然键冲突即复用既有凭据，
    补报事件按编号去重，分钟级撤回重复执行不产生新行，旧清单重跑
    因此无法覆盖补报之后的结果。进程内用互斥锁把「查重—写入」串行化，
    配合 SQLite 唯一约束保证两班同时提交也只出一份凭据。
    """

    _locks={}
    _locks_guard=threading.Lock()

    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with ReleaseStore._locks_guard:
            self._write_lock=ReleaseStore._locks.setdefault(str(self.path.resolve()),threading.Lock())
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS releases(
                    credential TEXT PRIMARY KEY,
                    berth TEXT NOT NULL,
                    start TEXT NOT NULL,
                    finish TEXT NOT NULL,
                    grace_minutes INTEGER NOT NULL,
                    source_reservation TEXT,
                    source_vehicle TEXT,
                    created_utc TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS release_minutes(
                    credential TEXT NOT NULL,
                    at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    backfill_id TEXT,
                    PRIMARY KEY(credential,at)) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS backfills(
                    event_id TEXT PRIMARY KEY,
                    berth TEXT NOT NULL,
                    vehicle TEXT NOT NULL,
                    start TEXT NOT NULL,
                    finish TEXT NOT NULL,
                    submitted_utc TEXT NOT NULL,
                    reservation TEXT,
                    outcome TEXT NOT NULL,
                    day TEXT NOT NULL,
                    paid_minutes INTEGER NOT NULL,
                    due_cents INTEGER NOT NULL,
                    detail TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path,timeout=15)) as db,db:
            db.row_factory=sqlite3.Row
            yield db

    @contextmanager
    def write_lock(self):
        with self._write_lock:
            yield

    @staticmethod
    def credential_key(berth,start,finish):
        return digest({'berth':berth,'start':start.isoformat(),'finish':finish.isoformat()})[:24]

    # ---- 放行凭据 -------------------------------------------------------
    def insert_release(self,credential,row,minutes):
        created=datetime.now(UTC).isoformat(timespec='seconds')
        with self.write_lock(), self.connect() as db:
            existed=db.execute('SELECT 1 FROM releases WHERE credential=?',(credential,)).fetchone() is not None
            db.execute('INSERT OR IGNORE INTO releases VALUES (?,?,?,?,?,?,?,?)',
                       (credential,row['berth'],row['start'].isoformat(),row['finish'].isoformat(),
                        row['grace_minutes'],row.get('source_reservation'),row.get('source_vehicle'),created))
            db.executemany(
                'INSERT OR IGNORE INTO release_minutes(credential,at,state) VALUES (?,?,?)',
                [(credential,when.isoformat(),state) for when,state in minutes])
        return credential,existed

    def get_release(self,credential):
        with self.connect() as db:
            head=db.execute('SELECT * FROM releases WHERE credential=?',(credential,)).fetchone()
            if head is None:
                return None
            slots=[dict(r) for r in db.execute(
                'SELECT at,state,backfill_id FROM release_minutes WHERE credential=? ORDER BY at',(credential,))]
        record=dict(head)
        record['start']=minute(record['start']); record['finish']=minute(record['finish'])
        record['minutes']=[{'at':minute(r['at']),'state':r['state'],'backfill_id':r['backfill_id']} for r in slots]
        return record

    def list_releases(self):
        with self.connect() as db:
            heads=[dict(r) for r in db.execute('SELECT * FROM releases ORDER BY created_utc,rowid')]
            slots=db.execute('SELECT credential,at,state,backfill_id FROM release_minutes ORDER BY at').fetchall()
        ledger={}
        for r in slots:
            ledger.setdefault(r['credential'],[]).append(dict(r))
        for head in heads:
            head['start']=minute(head['start']); head['finish']=minute(head['finish'])
            head['minutes']=[{'at':minute(r['at']),'state':r['state'],'backfill_id':r['backfill_id']}
                             for r in ledger.get(head['credential'],[])]
        return heads

    def overlapping_active(self,berth,start,finish,exclude_vehicle=None):
        """返回与 [start,finish) 相交、当前仍可放行（未撤回）的分钟归属。"""
        start,finish=start.astimezone(UTC),finish.astimezone(UTC)
        hits=[]
        for release in self.list_releases():
            if release['berth']!=berth or release['finish']<=start or release['start']>=finish:
                continue
            if exclude_vehicle and release['source_vehicle']==exclude_vehicle:
                continue
            hits.append(release)
        return hits

    # ---- 分钟撤回 -------------------------------------------------------
    def revoke_minutes(self,credential,revoked_at,backfill_id):
        stamps={when.astimezone(UTC).isoformat() for when in revoked_at}
        if not stamps:
            return 0
        placeholders=','.join('?' for _ in stamps)
        with self.connect() as db:
            cur=db.execute(
                f"UPDATE release_minutes SET state='REVOKED',backfill_id=? "
                f"WHERE credential=? AND state!='REVOKED' AND at IN ({placeholders})",
                (backfill_id,credential,*sorted(stamps)))
            changed=cur.rowcount
        return changed

    def active_minutes(self,berth,start,finish):
        """该泊位在 [start,finish) 内仍处于可放行状态的分钟（按凭据分组）。"""
        start,finish=start.astimezone(UTC),finish.astimezone(UTC)
        groups=[]
        for release in self.list_releases():
            if release['berth']!=berth or release['finish']<=start or release['start']>=finish:
                continue
            active=[m['at'] for m in release['minutes']
                    if m['state']=='PUBLISHED' and start<=m['at']<finish]
            if active:
                groups.append({'credential':release['credential'],'at':active})
        return groups

    # ---- 补报事件 -------------------------------------------------------
    def backfill_exists(self,event_id):
        with self.connect() as db:
            row=db.execute('SELECT detail FROM backfills WHERE event_id=?',(event_id,)).fetchone()
        return None if row is None else json.loads(row[0])

    def insert_backfill(self,event_id,base,detail):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO backfills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (event_id,base['berth'],base['vehicle'],base['start'].isoformat(),
                        base['finish'].isoformat(),base['submitted_utc'],base.get('reservation'),
                        base['outcome'],base['day'],base['paid_minutes'],base['due_cents'],
                        json.dumps(detail,ensure_ascii=False,sort_keys=True)))
            row=db.execute('SELECT detail FROM backfills WHERE event_id=?',(event_id,)).fetchone()
        return json.loads(row[0])

    def list_backfills(self):
        with self.connect() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM backfills ORDER BY submitted_utc,rowid')]
        for row in rows:
            row['start']=minute(row['start']); row['finish']=minute(row['finish'])
            row['detail']=json.loads(row.pop('detail'))
        return rows


def merge_runs(moments):
    """把分钟集合压缩成 [start,end) 连续区间，便于凭据与页面展示。"""
    ordered=sorted(moments)
    runs=[]
    for when in ordered:
        if runs and when==runs[-1][1]:
            runs[-1][1]=when+M
        else:
            runs.append([when,when+M])
    return runs


def require_window(payload,survey,*,want=('berth','start','end')):
    try:
        berth=payload['berth']
        if berth not in survey.berths:
            raise ValueError('UNKNOWN_BERTH')
        start,end=minute(payload['start']),minute(payload['end'])
        if end<=start:
            raise ValueError('INTERVAL')
        if not survey.start<=start<end<=survey.end:
            raise ValueError('RANGE')
    except (KeyError,TypeError,ValueError) as error:
        code=getattr(error,'args',('REQUEST_BAD',))[0]
        raise DocketError(code if code in {'UNKNOWN_BERTH','INTERVAL','RANGE'} else 'REQUEST_BAD',
                          '放行时间窗或泊位无效') from None
    missing=[key for key in want if key not in payload]
    if missing:
        raise DocketError('REQUEST_BAD',f'缺少字段：{",".join(missing)}')
    return berth,start,end


__all__=['ReleaseStore','RELEASE_STATES','merge_runs','require_window']

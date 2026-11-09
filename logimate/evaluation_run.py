"""P8.2 durable collection ledger. External attempts are never automatically replayed."""
import hashlib
from contextlib import contextmanager
import json
import sqlite3
import threading
import time
from pathlib import Path
from uuid import uuid4
from .deepseek import ModelUnavailable, safe_failure
from .end_to_end_eval import IMPLEMENTATIONS, query_hash
from .repository import Repository


def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)


def execution_plan(cases):
    modes=['single','five','shared']
    return [(c,m) for i,c in enumerate(cases) for m in modes[i%3:]+modes[:i%3]]


class RunLedger:
    def __init__(self,path,*,max_attempts=90,max_tokens=60000,secret=''):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.max_attempts,self.max_tokens,self.secret=max_attempts,max_tokens,secret
        self.lock=threading.Lock();self.stop_reason=None
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''CREATE TABLE IF NOT EXISTS cells (case_id TEXT, mode TEXT, intent TEXT NOT NULL, state TEXT NOT NULL,
                response TEXT, error TEXT, PRIMARY KEY(case_id,mode));
                CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, case_id TEXT, mode TEXT, state TEXT NOT NULL, metadata TEXT, error TEXT);''')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=3);db.execute('PRAGMA synchronous=FULL');db.row_factory=sqlite3.Row
        try:yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    def safe_text(self,value):
        text=encoded(value)
        if len(text.encode())>2097152 or (self.secret and self.secret in text):raise ValueError('oversized or sensitive output withheld')
        return text

    def begin(self,case,mode,planner):
        with self.db() as db:db.execute('INSERT INTO cells VALUES (?,?,?,?,?,?)',
            (case['case_id'],mode,encoded({'query_sha256':query_hash(case['query']),'planner_mode':planner}),'running',None,None))

    def finish(self,case_id,mode,response,*,error=None):
        text=self.safe_text(response)
        with self.db() as db:db.execute('UPDATE cells SET state=?,response=?,error=? WHERE case_id=? AND mode=?',
            ('recorded',text,encoded(error) if error else None,case_id,mode))

    def provider(self,underlying,case_id,mode,planner):
        owner=self
        class Audited:
            def complete(self,*args,**kwargs):
                with owner.lock:
                    summary=owner.accounting()
                    if owner.stop_reason:raise ModelUnavailable('Collection stopped',category='budget')
                    if summary['attempts_started']>=owner.max_attempts or summary['known_tokens']>=owner.max_tokens:
                        owner.stop_reason='budget_exhausted';raise ModelUnavailable('Collection budget exhausted',category='budget')
                    aid=str(uuid4())
                    with owner.db() as db:db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)',(aid,case_id,mode,'started' if planner=='live' else 'fixture_started',None,None))
                try:
                    message,meta=underlying.complete(*args,**kwargs)
                    # Store only known metadata. No messages, headers, reasoning or arbitrary exception text.
                    if not isinstance(meta,dict) or set(meta)-{'provider','model','response_id','usage','finish_reason','elapsed_ms','agent'}:raise ValueError('unrecognized metadata')
                    usage=meta.get('usage',{})
                    if not isinstance(usage,dict):raise ValueError('invalid usage')
                    for k in ('prompt_tokens','completion_tokens','total_tokens'):
                        if type(usage.get(k,0)) is not int or usage.get(k,0)<0:raise ValueError('invalid usage')
                    if planner=='live' and (meta.get('provider')!='deepseek' or not meta.get('response_id')):raise ValueError('missing provider identity')
                    if planner=='fixture' and (any(usage.get(k,0) for k in ('prompt_tokens','completion_tokens','total_tokens')) or meta.get('provider')=='deepseek' or meta.get('response_id')):raise ValueError('fixture accounting mismatch')
                    text=owner.safe_text(meta)
                    with owner.db() as db:db.execute('UPDATE attempts SET state=?,metadata=? WHERE id=?',('response_received',text,aid))
                    return message,meta
                except BaseException as exc:
                    with owner.db() as db:db.execute('UPDATE attempts SET state=?,error=? WHERE id=?',('failed_unknown_billing' if planner=='live' else 'fixture_failed',encoded(safe_failure(exc)),aid))
                    if getattr(exc,'http_status',None) in (401,402,429):owner.stop_reason='provider_auth_billing_or_rate_limit'
                    raise ModelUnavailable('Provider attempt failed; see safe ledger',category=getattr(exc,'category','invalid_response'),http_status=getattr(exc,'http_status',None)) from None
        return Audited()

    def accounting(self):
        with self.db() as db:rows=[dict(r) for r in db.execute('SELECT * FROM attempts ORDER BY rowid')]
        calls=[json.loads(r['metadata']) for r in rows if r['metadata'] is not None]
        return {'attempts_started':len(rows),'responses_recorded':len(calls),
            'known_tokens':sum(c.get('usage',{}).get('total_tokens',0) for c in calls),
            'billing_unknown_attempts':sum(r['state'] in ('started','failed_unknown_billing') for r in rows),
            'responses_sha256':hashlib.sha256(encoded(calls).encode()).hexdigest()}

    def rows(self):
        with self.db() as db:cells=[dict(r) for r in db.execute('SELECT * FROM cells ORDER BY rowid')]
        return [{**json.loads(c['intent']),'case_id':c['case_id'],'mode':c['mode'],'response':json.loads(c['response'])}
            for c in cells if c['state']=='recorded']

    def unfinished(self):
        with self.db() as db:return [dict(r) for r in db.execute("SELECT case_id,mode,state FROM cells WHERE state!='recorded'")]


def collect_cell(ledger,case,mode,planner,execute,*,fault=None):
    ledger.begin(case,mode,planner);began=time.perf_counter()
    try:
        result=execute()
        if fault:fault('after_external_before_result')
        ledger.safe_text(result)
        if result.get('implementation')!=IMPLEMENTATIONS[mode]:raise ValueError('implementation mismatch')
        with ledger.db() as db:recorded=[json.loads(r['metadata']) for r in db.execute('SELECT metadata FROM attempts WHERE case_id=? AND mode=? AND metadata IS NOT NULL',(case['case_id'],mode))]
        normalized=lambda values:sorted(encoded({k:v for k,v in m.items() if k!='agent'}) for m in values)
        if normalized(result.get('provider_calls',[]))!=normalized(recorded):raise ValueError('application/ledger receipt mismatch')
        ledger.finish(case['case_id'],mode,result)
    except Exception as exc:
        # An explicit collector failure envelope is distinct from an application return value.
        with ledger.db() as db:receipts=[json.loads(r['metadata']) for r in db.execute('SELECT metadata FROM attempts WHERE case_id=? AND mode=? AND metadata IS NOT NULL',(case['case_id'],mode))]
        repo=Repository()
        result={'simulated':True,'implementation':IMPLEMENTATIONS[mode],'dataset_sha256':repo.sha256,'dataset_id':repo.dataset.dataset_id,
            'status':'unavailable','route':None,'records':{},'tool_calls':[],'diagnosis':None,'provider_calls':receipts,
            'usage':{k:sum(m.get('usage',{}).get(k,0) for m in receipts) for k in ('prompt_tokens','completion_tokens','total_tokens')},
            'elapsed_ms':round((time.perf_counter()-began)*1000,3),'collector_failure_envelope':True}
        ledger.finish(case['case_id'],mode,result,error={'exception_type':type(exc).__name__,**safe_failure(exc)})
    return result

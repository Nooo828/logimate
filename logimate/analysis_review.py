"""P7.3 freeze a five-agent receipt before case/native-review binding.

Unrecorded external attempts are uncertain, never automatically re-run. This is
whole-analysis receipt recovery, not checkpointing inside individual experts.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from pydantic import Field, field_validator
from .cases import Actor, CaseError, digest, encoded, request_key
from .models import Model, Snapshot, Diagnosis
from .rules import DOMAINS, diagnose
from .graph_tools import RECORD_TYPES

REVISION=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SHARED='langgraph-five-agent-mcp-shared-policy-v2'
BUSINESS='langgraph-five-agent-mcp-v1'
ROOT_KEYS=set('trace_id implementation simulated dataset_id dataset_sha256 route status diagnosis model_called prompt_sha256 events domain_results provider_calls tool_calls records model_attempts failure_details elapsed_ms usage limitations tool_transport tool_transport_audit policy_context policy_transport_audit policy_consumption tool_startup_ms mcp_protocol_version policy_degraded reason'.split())


class AnalysisRequest(Model):
    query: str=Field(min_length=1,max_length=500,strict=True)
    @field_validator('query')
    @classmethod
    def valid_query(cls,value):
        value=value.strip();value.encode('utf-8')
        if not value:raise ValueError('empty query')
        return value


def validate_analysis(result,repo,tenant,query,*,planner_mode,implementation):
    if not isinstance(result,dict) or set(result)-ROOT_KEYS:raise ValueError('unrecognized analysis fields')
    if (result.get('simulated') is not True or result.get('dataset_id')!=repo.dataset.dataset_id or
        result.get('dataset_sha256')!=repo.sha256 or result.get('implementation')!=implementation or
        result.get('tool_transport')!='mcp-stdio'):raise ValueError('analysis identity mismatch')
    if result.get('status') not in {'answered','unavailable','needs_human','not_found','human','clarify','refuse'}:
        raise ValueError('unsupported analysis status')
    if len(encoded(result).encode())>2_097_152:raise ValueError('analysis too large')
    records=result.get('records',{})
    if not isinstance(records,dict) or set(records)-set(DOMAINS):raise ValueError('unrecognized records')
    order_ids=set()
    for domain,value in records.items():
        if value is None:continue
        record=RECORD_TYPES[domain].model_validate(value)
        truth=repo.get(record.order_id,tenant)
        expected=getattr(truth,domain,None) if truth else None
        if expected is None or record.model_dump(mode='json')!=expected.model_dump(mode='json'):
            raise ValueError('unverified/cross-tenant observation')
        order_ids.add(record.order_id)
    if len(order_ids)>1:raise ValueError('mixed order evidence')
    for domain,value in records.items():
        if value is None and order_ids:
            if getattr(repo.get(next(iter(order_ids)),tenant),domain) is not None:raise ValueError('false missing observation')
    diagnosis=result.get('diagnosis')
    if diagnosis is not None:
        actual=Diagnosis.model_validate(diagnosis).model_dump(mode='json')
        expected=diagnose(Snapshot.model_validate(records),as_of=repo.dataset.as_of,dataset_id=repo.dataset.dataset_id,intent='overall').model_dump(mode='json')
        actual.pop('trace_id');expected.pop('trace_id')
        if actual!=expected:raise ValueError('diagnosis/evidence mismatch')
    if result['status']=='answered' and (not records.get('order') or (set(records)==set(DOMAINS) and diagnosis is None)):
        raise ValueError('answered without required observations')
    policy=result.get('policy_context',{})
    if policy.get('status')=='context_only':
        from .policy_tools import PolicyArguments
        from .shared_policy import validate_result
        route=result['route'];domains=[d for d in DOMAINS if d=='order' or d in route['domains']]
        canonical=re.sub(r'(?<![A-Za-z0-9-])SIM-[0-9]{3}(?![A-Za-z0-9-])','',query).strip()[:500] or '订单 物流 仓储 售后 退货退款 库存异常 状态冲突 人工核实'
        from .tool_gateway import Principal
        validate_result(policy,PolicyArguments(query=canonical,domains=domains),Principal(tenant,frozenset(DOMAINS)))
        if result.get('policy_degraded') is not policy['degraded']:raise ValueError('hidden policy degradation')
    elif implementation==SHARED and records:raise ValueError('business records without shared policy context')
    calls=result.get('provider_calls',[])
    if not isinstance(calls,list) or len(calls)>5:raise ValueError('invalid model receipts')
    totals={k:0 for k in ('prompt_tokens','completion_tokens','total_tokens')}
    ids=[]
    for call in calls:
        if set(call)-{'provider','model','response_id','usage','finish_reason','elapsed_ms','agent'}:raise ValueError('unsafe provider metadata')
        if call.get('agent') not in ('coordinator',*DOMAINS):raise ValueError('unknown agent')
        usage=call.get('usage',{})
        if set(usage)-{'prompt_tokens','completion_tokens','total_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens','prompt_tokens_details','completion_tokens_details'}:
            raise ValueError('unrecognized usage metadata')
        for name,value in usage.items():
            values=value.values() if isinstance(value,dict) and name.endswith('_details') else [value]
            if any(type(v) is not int or v<0 for v in values):raise ValueError('invalid usage detail')
        for key in totals:
            value=call.get('usage',{}).get(key,0)
            if type(value) is not int or value<0:raise ValueError('invalid token accounting')
            totals[key]+=value
        if planner_mode=='live':
            if call.get('provider')!='deepseek' or not isinstance(call.get('response_id'),str) or not call['response_id']:
                raise ValueError('missing real provider identity')
            ids.append(call['response_id'])
    if result.get('usage')!=totals or len(ids)!=len(set(ids)):raise ValueError('model accounting mismatch')
    attempts=result.get('model_attempts',[])
    if not isinstance(attempts,list) or len(attempts)>5:raise ValueError('invalid model attempts')
    if sorted(v['agent'] for v in attempts if v.get('status')=='response_received')!=sorted(v['agent'] for v in calls):
        raise ValueError('attempt/response accounting mismatch')
    if result['status']=='answered' and not calls:raise ValueError('answered without planner response')
    if planner_mode=='fixture' and any(totals.values()):raise ValueError('fixture cannot claim model tokens')
    clean=copy.deepcopy(result)
    if planner_mode=='fixture':clean['model_called']=False
    return clean


class AnalysisReview:
    def __init__(self,store,native,repo,runner_factory,*,planner_mode='live',implementation=SHARED,fault=None):
        if planner_mode not in ('live','fixture') or implementation not in (SHARED,BUSINESS):raise ValueError('invalid trusted analysis profile')
        self.store,self.native,self.repo,self.runner_factory=store,native,repo,runner_factory
        self.planner_mode,self.implementation,self.fault=planner_mode,implementation,fault
        self.lock_path=store.path.with_suffix('.analysis-lock.sqlite3')
        with store._connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS analysis_receipts (
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                actor TEXT NOT NULL, query TEXT NOT NULL, revision TEXT NOT NULL, planner_mode TEXT NOT NULL,
                state TEXT NOT NULL, receipt TEXT, receipt_hash TEXT, response TEXT, response_hash TEXT,
                UNIQUE(tenant,request_key))''')

    @contextmanager
    def _exclusive(self):
        db=None
        try:
            db=sqlite3.connect(self.lock_path,timeout=3,isolation_level=None)
            db.execute('CREATE TABLE IF NOT EXISTS writer_lock (id INTEGER PRIMARY KEY)');db.execute('BEGIN IMMEDIATE')
            yield
        except sqlite3.Error:raise CaseError(503,'ANALYSIS_BUSY_OR_STORAGE_UNAVAILABLE') from None
        finally:
            if db is not None:
                if db.in_transaction:db.rollback()
                db.close()

    def _row(self,tenant,*,analysis_id=None,key=None):
        with self.store._connection(write=False) as db:
            row=db.execute('SELECT * FROM analysis_receipts WHERE tenant=? AND '+('id=?' if analysis_id else 'request_key=?'),
                (tenant,analysis_id or key)).fetchone()
        if row is None:return None
        if row['revision']!=REVISION:raise CaseError(503,'ANALYSIS_REVISION_MISMATCH')
        return dict(row)

    def _case_body(self,row,result):
        records=result.get('records',{});diagnosis=result.get('diagnosis');reasons=[]
        if result['status']!='answered':reasons.append('analysis_'+result['status'])
        if diagnosis and diagnosis['needs_human']:reasons.append('diagnosis_requires_human')
        if diagnosis and diagnosis['missing_domains']:reasons.append('missing_evidence')
        if diagnosis and any(v['code']=='RETURN_CONFLICT' for v in diagnosis['findings']):reasons.append('state_conflict')
        if result.get('policy_degraded'):reasons.append('degraded_policy_context')
        return {'simulated':True,'implementation':'five-agent-analysis-case-v1',
            'order_id':records.get('order',{}).get('order_id') if records.get('order') else None,
            'state':'waiting_human' if reasons else 'completed','reasons':reasons,
            'source':{'dataset_id':self.repo.dataset.dataset_id,'dataset_sha256':self.repo.sha256,'as_of':self.repo.dataset.as_of.isoformat()},
            'snapshot':Snapshot.model_validate({d:records.get(d) for d in DOMAINS}).model_dump(mode='json') if records.get('order') else None,
            'diagnosis':diagnosis,'analysis_receipt':{'analysis_id':row['id'],'sha256':row['receipt_hash'],
                'planner_mode':row['planner_mode'],'result':result},
            'limitations':['Frozen whole five-agent result; not arbitrary expert-node checkpoint recovery.',
                'Manual review changes review state only; no external business writes.']}

    def _recover(self,row):
        if row['state'] in ('running','uncertain'):
            with self.store._connection(write=True) as db:db.execute('UPDATE analysis_receipts SET state=? WHERE id=?',('uncertain',row['id']))
            raise CaseError(503,'ANALYSIS_UNRECORDED_ATTEMPT_NO_AUTO_RETRY')
        if row['state']=='bound':
            response=json.loads(row['response'])
            if digest(response)!=row['response_hash']:raise CaseError(503,'ANALYSIS_RESPONSE_INTEGRITY_FAILURE')
            self.store.get(response['thread']['case']['case_id'],row['tenant'])
            return response
        result=json.loads(row['receipt'])
        if digest(result)!=row['receipt_hash']:raise CaseError(503,'ANALYSIS_RECEIPT_INTEGRITY_FAILURE')
        validate_analysis(result,self.repo,row['tenant'],row['query'],planner_mode=row['planner_mode'],implementation=self.implementation)
        actor=Actor(row['tenant'],row['actor'])
        case=self.store.create(actor,{'analysis_id':row['id']},'analysis-case-'+row['id'],lambda:self._case_body(row,result))['case']
        if self.fault:self.fault('after_case')
        thread=self.native.start(actor,case['case_id'],'analysis-thread-'+row['id'])
        if self.fault:self.fault('after_thread')
        response={'simulated':True,'analysis_id':row['id'],'analysis_status':result['status'],'receipt_sha256':row['receipt_hash'],
            'planner_mode':row['planner_mode'],'recorded_usage':result['usage'],'thread':thread,
            'recorded_model_responses':len(result['provider_calls']) if row['planner_mode']=='live' else 0}
        with self.store._connection(write=True) as db:db.execute('UPDATE analysis_receipts SET state=?,response=?,response_hash=? WHERE id=?',
            ('bound',encoded(response),digest(response),row['id']))
        return response

    def analyze(self,actor,arguments,key):
        args=AnalysisRequest.model_validate(arguments);request_key(key)
        fingerprint=digest({'query':args.query,'actor':actor.subject})
        with self._exclusive():
            row=self._row(actor.tenant,key=key)
            if row is not None:
                if row['fingerprint']!=fingerprint:raise CaseError(409,'ANALYSIS_IDEMPOTENCY_CONFLICT')
                response=self._recover(row)
                return {**response,'replayed':True,'model_responses_this_request':0,'model_attempts_this_request':0}
            runner=self.runner_factory()  # Pure configuration check before any running/external attempt is recorded.
            analysis_id=str(uuid4())
            with self.store._connection(write=True) as db:db.execute('INSERT INTO analysis_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (analysis_id,actor.tenant,key,fingerprint,actor.subject,args.query,REVISION,self.planner_mode,'running',None,None,None,None))
            try:
                result=runner(args.query,actor.tenant)
                result=validate_analysis(result,self.repo,actor.tenant,args.query,planner_mode=self.planner_mode,implementation=self.implementation)
                if self.fault:self.fault('after_external_before_receipt')
                with self.store._connection(write=True) as db:db.execute('UPDATE analysis_receipts SET state=?,receipt=?,receipt_hash=? WHERE id=?',
                    ('recorded',encoded(result),digest(result),analysis_id))
            except BaseException:
                with self.store._connection(write=True) as db:db.execute('UPDATE analysis_receipts SET state=? WHERE id=?',('uncertain',analysis_id))
                raise
            if self.fault:self.fault('after_receipt')
            response=self._recover(self._row(actor.tenant,analysis_id=analysis_id))
            return {**response,'replayed':False,
                'model_responses_this_request':len(result['provider_calls']) if self.planner_mode=='live' else 0,
                'model_attempts_this_request':len(result['model_attempts']) if self.planner_mode=='live' else 0}

    def get(self,analysis_id,tenant):
        row=self._row(tenant,analysis_id=analysis_id)
        if row is None:raise CaseError(404,'ANALYSIS_NOT_FOUND')
        with self._exclusive():
            row=self._row(tenant,analysis_id=analysis_id)
            if row['state'] in ('running','uncertain'):
                with self.store._connection(write=True) as db:db.execute('UPDATE analysis_receipts SET state=? WHERE id=?',('uncertain',analysis_id))
                return {'simulated':True,'analysis_id':analysis_id,'state':'uncertain','automatic_retry':False,
                    'planner_mode':row['planner_mode'],'model_accounting':'unknown_unrecorded' if row['planner_mode']=='live' else 'fixture_no_model',
                    'model_responses_this_request':0}
            response=self._recover(row)
            current=self.native.get(response['thread']['thread_id'],tenant)
            return {**response,'thread':current,'replayed':True,'model_responses_this_request':0,'model_attempts_this_request':0}

    def get_by_key(self,actor,key):
        request_key(key);row=self._row(actor.tenant,key=key)
        if row is None:raise CaseError(404,'ANALYSIS_NOT_FOUND')
        return self.get(row['id'],actor.tenant)

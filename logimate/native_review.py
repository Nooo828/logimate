"""Native interrupt/Command + official SqliteSaver around frozen P7.1 case evidence.

Control inbox and checkpointer are separate SQLite transactions. Authorized pending
commands reconcile on retry/read; a separate DB write lock serializes graph writers.
No model/tool/business writes occur in replayable nodes.
"""
from contextlib import contextmanager
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sqlite3
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langsmith import tracing_context
from .cases import Actor, CaseError, ReviewCase, request_key, digest, encoded

REVISION = digest({'code':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    'langgraph':version('langgraph'),'checkpoint':version('langgraph-checkpoint'),
    'sqlite':version('langgraph-checkpoint-sqlite')})


class ReviewState(TypedDict, total=False):
    thread_id: str
    source_case: dict
    source_case_sha256: str
    capture_count: int
    decisions: list[dict]
    last_command_id: str
    last_action: str
    finalized: bool


def compile_review(saver, capture=None):
    def capture_case(state):
        if capture is not None: capture(state['source_case'])  # Trusted test instrumentation only.
        return {'source_case_sha256':digest(state['source_case']),'capture_count':1,'decisions':[]}
    def human_gate(state):
        answer=interrupt({'thread_id':state['thread_id'],'case_id':state['source_case']['case_id'],
            'kind':'synthetic_review_required','review_count':len(state['decisions'])})
        if any(d['command_id']==answer['command_id'] for d in state['decisions']):
            return {'last_command_id':answer['command_id']}
        return {'decisions':state['decisions']+[answer], 'last_command_id':answer['command_id'],'last_action':answer['action']}
    builder=StateGraph(ReviewState)
    builder.add_node('capture_case',capture_case);builder.add_node('human_gate',human_gate)
    builder.add_node('finalize',lambda state:{'finalized':True})
    builder.add_edge(START,'capture_case')
    builder.add_conditional_edges('capture_case',lambda s:'human_gate' if s['source_case']['state']=='waiting_human' else 'finalize')
    builder.add_conditional_edges('human_gate',lambda s:'human_gate' if s['last_action']=='request_evidence' else 'finalize')
    builder.add_edge('finalize',END)
    return builder.compile(checkpointer=saver)


class NativeReview:
    def __init__(self, store, *, fault=None, capture=None):
        self.store,self.fault,self.capture=store,fault,capture
        self.checkpoints=store.path.with_suffix('.checkpoints.sqlite3')
        self.lock_path=store.path.with_suffix('.review-lock.sqlite3')
        with store._connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS native_threads (
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, case_id TEXT NOT NULL UNIQUE,
                    revision TEXT NOT NULL, frozen_case TEXT NOT NULL, frozen_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS native_operations (
                    tenant TEXT NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    thread_id TEXT NOT NULL REFERENCES native_threads(id), command_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                    response TEXT, response_hash TEXT, PRIMARY KEY(tenant,request_key));
            ''')

    @contextmanager
    def _exclusive(self):
        db=None
        try:
            db=sqlite3.connect(self.lock_path,timeout=3,isolation_level=None)
            db.execute('CREATE TABLE IF NOT EXISTS writer_lock (id INTEGER PRIMARY KEY)')
            db.execute('BEGIN IMMEDIATE')
            yield
        except sqlite3.Error: raise CaseError(503,'REVIEW_BUSY_OR_STORAGE_UNAVAILABLE') from None
        finally:
            if db is not None:
                if db.in_transaction:db.rollback()
                db.close()

    @contextmanager
    def _graph(self):
        conn=sqlite3.connect(self.checkpoints,check_same_thread=False)
        try:
            conn.execute('PRAGMA synchronous=FULL')
            serde=JsonPlusSerializer(pickle_fallback=False,allowed_json_modules=None,allowed_msgpack_modules=None)
            saver=SqliteSaver(conn,serde=serde);saver.jsonplus_serde=serde
            with tracing_context(enabled=False): yield compile_review(saver,self.capture)
        finally:conn.close()

    def _thread(self,thread_id,tenant):
        with self.store._connection(write=False) as db:
            row=db.execute('SELECT * FROM native_threads WHERE id=? AND tenant=?',(thread_id,tenant)).fetchone()
        if row is None:raise CaseError(404,'REVIEW_THREAD_NOT_FOUND')
        if row['revision']!=REVISION:raise CaseError(503,'REVIEW_GRAPH_REVISION_MISMATCH')
        frozen=json.loads(row['frozen_case'])
        if (digest(frozen)!=row['frozen_hash'] or frozen['tenant_id']!=tenant or frozen['case_id']!=row['case_id']):
            raise CaseError(503,'REVIEW_SOURCE_INTEGRITY_FAILURE')
        return dict(row)

    def _cached(self,actor,key,fingerprint):
        with self.store._connection(write=False) as db:
            row=db.execute('SELECT * FROM native_operations WHERE tenant=? AND request_key=?',(actor.tenant,key)).fetchone()
        if row is None:return None
        if row['fingerprint']!=fingerprint:raise CaseError(409,'REVIEW_IDEMPOTENCY_CONFLICT')
        if row['status']=='done':
            response=json.loads(row['response'])
            if digest(response)!=row['response_hash']:raise CaseError(503,'REVIEW_RESPONSE_INTEGRITY_FAILURE')
            return {**response,'replayed':True}
        return None

    def _view(self,thread,graph):
        config={'configurable':{'thread_id':thread['id']}}
        snapshot=graph.get_state(config);values=snapshot.values
        if not values or values.get('source_case_sha256')!=thread['frozen_hash']:
            raise CaseError(503,'REVIEW_CHECKPOINT_SOURCE_MISMATCH')
        if digest(values['source_case'])!=thread['frozen_hash']:raise CaseError(503,'REVIEW_CHECKPOINT_SOURCE_MISMATCH')
        decisions=values.get('decisions',[])
        ids=[d['command_id'] for d in decisions]
        if len(set(ids))!=len(ids):raise CaseError(503,'REVIEW_DUPLICATE_CHECKPOINT_DECISION')
        with self.store._connection(write=False) as db:
            for decision in decisions:
                op=db.execute('SELECT * FROM native_operations WHERE command_id=? AND thread_id=?',
                    (decision['command_id'],thread['id'])).fetchone()
                if op is None or digest(self._payload(dict(op)).get('decision'))!=digest(decision):
                    raise CaseError(503,'REVIEW_DECISION_INTEGRITY_FAILURE')
        paused=any(task.interrupts for task in snapshot.tasks)
        status='waiting_human' if paused else {'confirm_review':'reviewed','reject_review':'rejected'}.get(values.get('last_action'),'completed')
        if snapshot.next and not paused:raise CaseError(503,'REVIEW_GRAPH_INCOMPLETE')
        case=self.store.get(thread['case_id'],thread['tenant'])['case']
        if case['version']!=json.loads(thread['frozen_case'])['version']+len(decisions):
            raise CaseError(503,'REVIEW_PROJECTION_VERSION_MISMATCH')
        return {'simulated':True,'implementation':'native-case-review-v1','thread_id':thread['id'],
            'version':len(decisions)+1,'status':status,'checkpoint_id':snapshot.config['configurable']['checkpoint_id'],
            'pending_nodes':list(snapshot.next),'capture_count':values['capture_count'],'decisions':decisions,
            'case':case,'replayed':False,'limitations':['Native review graph over frozen case evidence; not checkpointed five-agent execution.',
                'SQLite inbox reconciliation and one global graph writer; no distributed DB or business side effects.']}

    def _payload(self,op):
        payload=json.loads(op['payload']); expected=payload.pop('integrity',None)
        if digest(payload)!=expected:raise CaseError(503,'REVIEW_COMMAND_INTEGRITY_FAILURE')
        if op['kind']=='resume':
            request=ReviewCase.model_validate(payload['request_arguments'])
            decision=payload['decision']
            if (decision['command_id']!=op['command_id'] or decision['action']!=request.action or decision['note']!=request.note or
                payload['case_arguments']['action']!=request.action or payload['case_arguments']['note']!=request.note or
                digest({'kind':'resume','thread_id':op['thread_id'],'actor':decision['actor'],'arguments':request.model_dump()})!=op['fingerprint']):
                raise CaseError(503,'REVIEW_COMMAND_INTEGRITY_FAILURE')
        return payload

    def _recover(self,thread):
        with self.store._connection(write=False) as db:
            pending=[dict(r) for r in db.execute('SELECT * FROM native_operations WHERE thread_id=? AND status=? ORDER BY rowid',(thread['id'],'pending'))]
        with self._graph() as graph:
            config={'configurable':{'thread_id':thread['id']},'recursion_limit':12}
            for op in pending:
                payload=self._payload(op);snapshot=graph.get_state(config)
                if op['kind']=='start':
                    if not snapshot.values:graph.invoke({'thread_id':thread['id'],'source_case':json.loads(thread['frozen_case'])},config,durability='sync')
                    elif snapshot.next and not any(t.interrupts for t in snapshot.tasks):graph.invoke(None,config,durability='sync')
                else:
                    if not any(d['command_id']==op['command_id'] for d in snapshot.values.get('decisions',[])):
                        graph.invoke(Command(resume=payload['decision']),config,durability='sync')
                    values=graph.get_state(config).values
                    if not any(d['command_id']==op['command_id'] for d in values.get('decisions',[])):
                        raise CaseError(503,'REVIEW_COMMAND_NOT_CHECKPOINTED')
                if self.fault:self.fault('after_graph')
                if op['kind']=='resume':
                    actor=Actor(thread['tenant'],payload['decision']['actor'],True)
                    self.store.decide(actor,thread['case_id'],payload['case_arguments'],'wf-'+op['command_id'],workflow_id=thread['id'])
                    if self.fault:self.fault('after_case_projection')
                response=self._view(thread,graph)
                with self.store._connection(write=True) as db:
                    db.execute('UPDATE native_operations SET status=?,response=?,response_hash=? WHERE command_id=?',
                        ('done',encoded(response),digest(response),op['command_id']))
            return self._view(thread,graph)

    def start(self,actor,case_id,key):
        request_key(key);fingerprint=digest({'kind':'start','case_id':case_id,'actor':actor.subject})
        self.store.get(case_id,actor.tenant)  # Authorization before any checkpoint access.
        with self._exclusive():
            with self.store._connection(write=False) as db:
                previous=db.execute('SELECT thread_id FROM native_operations WHERE tenant=? AND request_key=?',(actor.tenant,key)).fetchone()
            if previous is not None:
                cached=self._cached(actor,key,fingerprint)
                if cached is not None:return cached
                thread=self._thread(previous['thread_id'],actor.tenant);self._recover(thread)
                return self._cached(actor,key,fingerprint)
            thread_id=str(uuid4());command_id=str(uuid4())
            with self.store._connection(write=True) as db:
                body,_=self.store._read(db,case_id,actor.tenant)
                if db.execute('SELECT 1 FROM case_review_threads WHERE case_id=?',(case_id,)).fetchone():raise CaseError(409,'CASE_ALREADY_BOUND')
                db.execute('INSERT INTO native_threads VALUES (?,?,?,?,?,?)',(thread_id,actor.tenant,case_id,REVISION,encoded(body),digest(body)))
                db.execute('INSERT INTO case_review_threads VALUES (?,?)',(case_id,thread_id))
                db.execute('INSERT INTO native_operations VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (actor.tenant,key,fingerprint,thread_id,command_id,'start',encoded({'integrity':digest({})}),'pending',None,None))
            return self._recover(self._thread(thread_id,actor.tenant))

    def get(self,thread_id,tenant):
        thread=self._thread(thread_id,tenant)
        with self._exclusive():return self._recover(thread)

    def resume(self,actor,thread_id,arguments,key):
        if not actor.reviewer:raise CaseError(403,'REVIEW_ROLE_REQUIRED')
        args=ReviewCase.model_validate(arguments);request_key(key)
        fingerprint=digest({'kind':'resume','thread_id':thread_id,'actor':actor.subject,'arguments':args.model_dump()})
        thread=self._thread(thread_id,actor.tenant)
        with self._exclusive():
            current=self._recover(thread)
            cached=self._cached(actor,key,fingerprint)
            if cached is not None:return cached
            if current['status']!='waiting_human':raise CaseError(409,'REVIEW_NOT_WAITING')
            if current['version']!=args.expected_version:raise CaseError(409,'STALE_REVIEW_VERSION')
            command_id=str(uuid4())
            decision={'command_id':command_id,'actor':actor.subject,'action':args.action,'note':args.note}
            payload={'decision':decision,'request_arguments':args.model_dump(),
                'case_arguments':{**args.model_dump(),'expected_version':current['case']['version']}}
            payload['integrity']=digest(payload)
            with self.store._connection(write=True) as db:
                db.execute('INSERT INTO native_operations VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (actor.tenant,key,fingerprint,thread_id,command_id,'resume',encoded(payload),'pending',None,None))
            return self._recover(thread)

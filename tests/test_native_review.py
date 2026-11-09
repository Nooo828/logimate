"""Official SDK interrupt, actual SQLite checkpoints and OS crash reconciliation; no LLM."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from logimate.api import create_app
from logimate.cases import Actor, CaseStore, CaseService, CaseError
from logimate.native_review import NativeReview

USER=Actor('demo-a','operator')
REVIEWER=Actor('demo-a','reviewer',True)


@pytest.fixture
def source(tmp_path):
    store=CaseStore(tmp_path/'cases.sqlite3')
    case=CaseService(store).create(USER,{'order_id':'SIM-006'},'create-case-1')['case']
    return store,case


def test_native_interrupt_survives_new_service_and_completed_capture_is_not_reexecuted(source):
    store,case=source;captures=[]
    workflow=NativeReview(store,capture=lambda value:captures.append(value))
    first=workflow.start(USER,case['case_id'],'create-thread-1')
    assert first['status']=='waiting_human' and first['pending_nodes']==['human_gate']
    assert first['checkpoint_id'] and first['capture_count']==1 and len(captures)==1
    def forbidden(value):raise AssertionError('completed capture must not execute on resume')
    restarted=NativeReview(CaseStore(store.path),capture=forbidden)
    assert restarted.get(first['thread_id'],'demo-a')['status']=='waiting_human'
    final=restarted.resume(REVIEWER,first['thread_id'],{'expected_version':1,'action':'confirm_review'},'resume-thread-1')
    assert final['status']=='reviewed' and final['capture_count']==1 and final['version']==2
    assert final['case']['diagnosis']==case['diagnosis'] and final['case']['snapshot']==case['snapshot']
    assert final['case']['state']=='reviewed' and len(store.get(case['case_id'],'demo-a')['events'])==2


def test_evidence_requests_reinterrupt_and_idempotent_resume_preserves_audit(source):
    store,case=source;w=NativeReview(store);first=w.start(USER,case['case_id'],'create-thread-1');tid=first['thread_id']
    args={'expected_version':1,'action':'request_evidence','note':'need source'}
    second=w.resume(REVIEWER,tid,args,'resume-thread-1')
    assert second['status']=='waiting_human' and second['version']==2 and second['capture_count']==1
    replay=w.resume(REVIEWER,tid,args,'resume-thread-1');assert replay['replayed'] and replay['decisions']==second['decisions']
    with pytest.raises(CaseError) as e:w.resume(REVIEWER,tid,{**args,'note':'changed'},'resume-thread-1')
    assert e.value.status==409
    with pytest.raises(CaseError) as e:store.decide(REVIEWER,case['case_id'],{'expected_version':2,'action':'confirm_review'},'bypass-thread-1')
    assert e.value.code=='CASE_REQUIRES_NATIVE_REVIEW_THREAD'
    with pytest.raises(CaseError) as e:w.resume(REVIEWER,tid,{'expected_version':1,'action':'confirm_review'},'stale-thread-1')
    assert e.value.code=='STALE_REVIEW_VERSION'
    final=w.resume(REVIEWER,tid,{'expected_version':2,'action':'reject_review'},'resume-thread-2')
    assert final['status']=='rejected' and final['version']==3 and final['case']['version']==3


def test_start_replay_is_original_response_and_second_thread_binding_is_rejected(source):
    store,case=source;w=NativeReview(store);first=w.start(USER,case['case_id'],'create-thread-1')
    w.resume(REVIEWER,first['thread_id'],{'expected_version':1,'action':'confirm_review'},'resume-thread-1')
    replay=w.start(USER,case['case_id'],'create-thread-1')
    assert replay['replayed'] and replay['status']=='waiting_human' and replay['version']==1
    assert w.get(first['thread_id'],'demo-a')['status']=='reviewed'
    with pytest.raises(CaseError) as e:w.start(USER,case['case_id'],'create-thread-2')
    assert e.value.code=='CASE_ALREADY_BOUND'


def test_thread_can_bind_existing_case_version_without_confusing_thread_version(source):
    store,case=source
    store.decide(REVIEWER,case['case_id'],{'expected_version':1,'action':'request_evidence'},'pre-thread-review')
    w=NativeReview(store);first=w.start(USER,case['case_id'],'create-thread-1')
    assert first['version']==1 and first['case']['version']==2
    final=w.resume(REVIEWER,first['thread_id'],{'expected_version':1,'action':'confirm_review'},'resume-thread-1')
    assert final['version']==2 and final['case']['version']==3


@pytest.mark.parametrize('phase',['after_graph','after_case_projection'])
def test_pending_authorized_command_reconciles_without_duplicate_case_event(source,phase):
    store,case=source;w=NativeReview(store);tid=w.start(USER,case['case_id'],'create-thread-1')['thread_id']
    def crash(at):
        if at==phase:raise RuntimeError('explicit crash boundary')
    w.fault=crash;args={'expected_version':1,'action':'confirm_review','note':'confirmed'}
    with pytest.raises(RuntimeError):w.resume(REVIEWER,tid,args,'resume-thread-1')
    fresh=NativeReview(CaseStore(store.path));result=fresh.get(tid,'demo-a')
    assert result['status']=='reviewed' and result['version']==2 and result['capture_count']==1
    assert len(store.get(case['case_id'],'demo-a')['events'])==2
    assert fresh.resume(REVIEWER,tid,args,'resume-thread-1')['replayed']


@pytest.mark.parametrize('phase',['after_graph','after_case_projection'])
def test_real_process_exit_releases_writer_lock_and_recovers_pending_command(source,phase):
    store,case=source;tid=NativeReview(store).start(USER,case['case_id'],'create-thread-1')['thread_id']
    code="""import os,sys
from logimate.cases import CaseStore,Actor
from logimate.native_review import NativeReview
def fault(at):
 if at==sys.argv[3]:os._exit(31)
w=NativeReview(CaseStore(sys.argv[1]),fault=fault)
w.resume(Actor('demo-a','reviewer',True),sys.argv[2],{'expected_version':1,'action':'confirm_review'},'crash-resume-key')
"""
    p=subprocess.run([sys.executable,'-c',code,str(store.path),tid,phase],capture_output=True,timeout=20)
    assert p.returncode==31,p.stderr
    result=NativeReview(CaseStore(store.path)).get(tid,'demo-a')
    assert result['status']=='reviewed' and result['version']==2 and len(store.get(case['case_id'],'demo-a')['events'])==2


def test_auth_and_revision_checks_happen_before_checkpoint_deserialization(source,monkeypatch):
    store,case=source;w=NativeReview(store);tid=w.start(USER,case['case_id'],'create-thread-1')['thread_id']
    def forbidden():raise AssertionError('checkpoint access before authorization')
    monkeypatch.setattr(w,'_graph',forbidden)
    with pytest.raises(CaseError) as e:w.get(tid,'demo-b')
    assert e.value.status==404
    with pytest.raises(CaseError) as e:w.resume(USER,tid,{'expected_version':1,'action':'confirm_review'},'resume-thread-1')
    assert e.value.status==403
    with store._connection(write=True) as db:db.execute('UPDATE native_threads SET revision=? WHERE id=?',('unsupported',tid))
    with pytest.raises(CaseError) as e:w.get(tid,'demo-a')
    assert e.value.code=='REVIEW_GRAPH_REVISION_MISMATCH'


def test_concurrent_resume_is_serialized_and_accepts_one_version(source):
    store,case=source;w=NativeReview(store);tid=w.start(USER,case['case_id'],'create-thread-1')['thread_id']
    def run(i):
        try:return NativeReview(CaseStore(store.path)).resume(REVIEWER,tid,{'expected_version':1,'action':'request_evidence'},f'concurrent-{i}')
        except CaseError as e:return e.status
    with ThreadPoolExecutor(max_workers=2) as pool:rows=list(pool.map(run,[1,2]))
    assert sum(isinstance(v,dict) for v in rows)==1 and 409 in rows
    assert len(store.get(case['case_id'],'demo-a')['events'])==2


def test_partial_pending_payload_tampering_is_rejected(source):
    store,case=source;w=NativeReview(store);tid=w.start(USER,case['case_id'],'create-thread-1')['thread_id']
    w.fault=lambda phase:(_ for _ in ()).throw(RuntimeError('explicit fault'))
    with pytest.raises(RuntimeError):w.resume(REVIEWER,tid,{'expected_version':1,'action':'confirm_review'},'resume-thread-1')
    with store._connection(write=True) as db:db.execute('UPDATE native_operations SET payload=? WHERE status=?',('{}','pending'))
    with pytest.raises(CaseError) as e:NativeReview(store).get(tid,'demo-a')
    assert e.value.code=='REVIEW_COMMAND_INTEGRITY_FAILURE'


def test_clear_case_finishes_without_human_or_resume(source):
    store,_=source;case=CaseService(store).create(USER,{'order_id':'SIM-001'},'clear-case-1')['case']
    w=NativeReview(store);first=w.start(USER,case['case_id'],'clear-thread-1')
    assert first['status']=='completed' and first['pending_nodes']==[]
    with pytest.raises(CaseError):w.resume(REVIEWER,first['thread_id'],{'expected_version':1,'action':'confirm_review'},'resume-thread-1')


def test_http_contract_rejects_arbitrary_state_and_requires_independent_review_identity(source):
    store,case=source;w=NativeReview(store)
    app=create_app(token='reader',review_token='reviewer',case_store=store,native_review_service=w)
    with TestClient(app) as c:
        headers={'Authorization':'Bearer reader','Idempotency-Key':'http-thread-1'}
        r=c.post('/v1/review-threads',json={'case_id':case['case_id'],'checkpoint_id':'fake'},headers=headers)
        assert r.status_code==422
        r=c.post('/v1/review-threads',json={'case_id':case['case_id']},headers=headers)
        assert r.status_code==200 and r.json()['status']=='waiting_human'
        tid=r.json()['thread_id'];body={'expected_version':1,'action':'confirm_review'}
        assert c.post('/v1/review-threads/'+tid+'/resume',json=body,headers=headers).status_code==401
        r=c.post('/v1/review-threads/'+tid+'/resume',json=body,headers={'Authorization':'Bearer reviewer','Idempotency-Key':'http-resume-1'})
        assert r.status_code==200 and r.json()['status']=='reviewed'

"""Durable review workflow: actual SQLite, new OS process, rollback and concurrent review."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from logimate.api import create_app
from logimate.cases import Actor, CaseStore, CaseService, CaseError

USER = Actor('demo-a','operator')
REVIEWER = Actor('demo-a','reviewer',True)


@pytest.fixture
def service(tmp_path): return CaseService(CaseStore(tmp_path/'case.sqlite3'))


@pytest.mark.parametrize('order,trigger,waiting,reason', [('SIM-001','auto',False,None),
    ('SIM-001','user_requested',True,'user_requested'),('SIM-001','high_risk',True,'high_risk'),
    ('SIM-005','auto',True,'missing_evidence'),('SIM-006','auto',True,'state_conflict')])
def test_routing_preserves_actual_records_and_pause(service, order, trigger, waiting, reason):
    response=service.create(USER,{'order_id':order,'trigger':trigger},'create-key-1')
    case=response['case']; assert (case['state']=='waiting_human') is waiting
    assert case['source']['dataset_sha256']==service.repository.sha256
    assert case['snapshot']['order']['order_id']==order and case['version']==1
    if reason: assert reason in case['reasons']
    assert service.store.get(case['case_id'],'demo-a')['case']==case


def test_create_idempotency_is_atomic_even_for_concurrent_calls(service):
    args={'order_id':'SIM-006'}
    with ThreadPoolExecutor(max_workers=8) as pool: rows=list(pool.map(lambda _:service.create(USER,args,'same-create-key'),range(8)))
    assert len({r['case']['case_id'] for r in rows})==1
    assert sum(not r['replayed'] for r in rows)==1
    assert len(service.store.get(rows[0]['case']['case_id'],'demo-a')['events'])==1
    with pytest.raises(CaseError) as e: service.create(USER,{'order_id':'SIM-001'},'same-create-key')
    assert e.value.status==409


def test_review_is_durable_idempotent_and_does_not_rewrite_business_facts(service):
    initial=service.create(USER,{'order_id':'SIM-006'},'create-key-1')['case']
    case_id=initial['case_id']; args={'expected_version':1,'action':'confirm_review','note':'human checked'}
    response=service.store.decide(REVIEWER,case_id,args,'review-key-1')
    reopened=CaseStore(service.store.path).get(case_id,'demo-a')
    assert reopened==response and reopened['case']['state']=='reviewed'
    assert reopened['case']['diagnosis']==initial['diagnosis'] and reopened['case']['snapshot']==initial['snapshot']
    assert reopened['events'][-1]['review']['note']=='human checked'
    replay=CaseStore(service.store.path).decide(REVIEWER,case_id,args,'review-key-1')
    assert replay['replayed'] and replay['case']==response['case'] and len(replay['events'])==2
    # Creation replay is the original response; GET is the current state.
    created=service.create(USER,{'order_id':'SIM-006'},'create-key-1')
    assert created['replayed'] and created['case']['version']==1
    assert reopened['case']['version']==2


def test_request_evidence_stays_paused_and_retains_all_review_notes(service):
    c=service.create(USER,{'order_id':'SIM-006'},'create-key-1')['case']
    first=service.store.decide(REVIEWER,c['case_id'],{'expected_version':1,'action':'request_evidence','note':'need source'},'review-key-1')
    assert first['case']['state']=='waiting_human'
    with pytest.raises(CaseError) as e: service.store.decide(REVIEWER,c['case_id'],{'expected_version':1,'action':'confirm_review'},'review-key-2')
    assert e.value.code=='STALE_CASE_VERSION'
    last=service.store.decide(REVIEWER,c['case_id'],{'expected_version':2,'action':'reject_review','note':'cannot confirm'},'review-key-3')
    assert [v['review']['note'] for v in last['events'][1:]]==['need source','cannot confirm']
    with pytest.raises(CaseError): service.store.decide(REVIEWER,c['case_id'],{'expected_version':3,'action':'confirm_review'},'review-key-4')


def test_two_reviewers_cannot_both_advance_same_version(service):
    cid=service.create(USER,{'order_id':'SIM-006'},'create-key-1')['case']['case_id']
    def decide(i):
        try: return service.store.decide(REVIEWER,cid,{'expected_version':1,'action':'request_evidence'},f'review-key-{i}')
        except CaseError as e: return e.status
    with ThreadPoolExecutor(max_workers=2) as pool: rows=list(pool.map(decide,[1,2]))
    assert sum(isinstance(r,dict) for r in rows)==1 and 409 in rows
    assert len(service.store.get(cid,'demo-a')['events'])==2


def test_other_tenant_and_wrong_role_never_read_or_mutate_case(service):
    cid=service.create(USER,{'order_id':'SIM-006'},'create-key-1')['case']['case_id']
    with pytest.raises(CaseError) as e: service.store.get(cid,'demo-b')
    assert e.value.status==404
    with pytest.raises(CaseError) as e: service.store.decide(Actor('demo-b','reviewer',True),cid,{'expected_version':1,'action':'confirm_review'},'review-key-1')
    assert e.value.status==404
    with pytest.raises(CaseError) as e: service.store.decide(USER,cid,{'expected_version':1,'action':'confirm_review'},'review-key-1')
    assert e.value.status==403
    assert service.store.get(cid,'demo-a')['case']['version']==1


def test_failed_append_rolls_back_case_change_and_idempotency_record(service, monkeypatch):
    before=service.create(USER,{'order_id':'SIM-006'},'create-key-1')
    cid=before['case']['case_id']
    def fail(*args): raise RuntimeError('explicit fault after case update, before event insertion')
    monkeypatch.setattr(service.store,'_event',fail)
    with pytest.raises(RuntimeError): service.store.decide(REVIEWER,cid,{'expected_version':1,'action':'confirm_review'},'review-key-1')
    assert CaseStore(service.store.path).get(cid,'demo-a')==before
    retry=CaseStore(service.store.path).decide(REVIEWER,cid,{'expected_version':1,'action':'confirm_review'},'review-key-1')
    assert not retry['replayed'] and retry['case']['version']==2


@pytest.mark.parametrize('target', ['case','event'])
def test_partial_storage_tampering_fails_closed(service,target):
    cid=service.create(USER,{'order_id':'SIM-006'},'create-key-1')['case']['case_id']
    with sqlite3.connect(service.store.path) as db:
        if target=='case': db.execute('UPDATE cases SET body=? WHERE id=?', ('{}',cid))
        else: db.execute('DELETE FROM events WHERE case_id=?',(cid,))
    with pytest.raises(CaseError) as e: service.store.get(cid,'demo-a')
    assert e.value.status==503


def test_actual_process_crash_rolls_back_and_new_process_reads_paused_state(service):
    before=service.create(USER,{'order_id':'SIM-006'},'create-key-1')
    path=str(service.store.path);cid=before['case']['case_id']
    code="import sqlite3,sys,os; d=sqlite3.connect(sys.argv[1]); d.execute('BEGIN IMMEDIATE'); d.execute('UPDATE cases SET body=? WHERE id=?',('{}',sys.argv[2])); os._exit(23)"
    p=subprocess.run([sys.executable,'-c',code,path,cid],capture_output=True,timeout=10)
    assert p.returncode==23
    code="import json,sys; from logimate.cases import CaseStore; print(json.dumps(CaseStore(sys.argv[1]).get(sys.argv[2],'demo-a')))"
    p=subprocess.run([sys.executable,'-c',code,path,cid],capture_output=True,text=True,encoding='utf-8',timeout=10)
    assert p.returncode==0,p.stderr
    assert json.loads(p.stdout)==before


def test_api_authorization_contract_and_role_separation(service):
    with TestClient(create_app(token='reader',review_token='reviewer',case_store=service.store)) as client:
        body={'order_id':'SIM-006'}; headers={'Authorization':'Bearer reader','Idempotency-Key':'create-key-1'}
        assert client.post('/v1/cases',json=body).status_code==401
        assert client.post('/v1/cases',json={**body,'tenant_id':'demo-b'},headers=headers).status_code==422
        assert client.post('/v1/cases',json=body,headers={'Authorization':'Bearer reader'}).status_code==422
        response=client.post('/v1/cases',json=body,headers=headers)
        assert response.status_code==201
        assert client.post('/v1/cases',json=body,headers=headers).status_code==200
        cid=response.json()['case']['case_id']; path='/v1/cases/'+cid+'/decisions'
        assert client.post(path,json={'expected_version':1,'action':'confirm_review'},headers=headers).status_code==401
        result=client.post(path,json={'expected_version':1,'action':'confirm_review'},headers={'Authorization':'Bearer reviewer','Idempotency-Key':'review-key-1'})
        assert result.status_code==200 and result.json()['case']['state']=='reviewed'


def test_disabled_store_and_same_review_token_are_not_silently_accepted(monkeypatch):
    monkeypatch.delenv('LOGIMATE_CASE_DB',raising=False)
    with TestClient(create_app(token='reader')) as client:
        assert client.post('/v1/cases',json={'order_id':'SIM-006'},headers={'Authorization':'Bearer reader','Idempotency-Key':'create-key-1'}).status_code==503


def test_reviewer_cannot_share_reader_token(service):
    with pytest.raises(ValueError): create_app(token='same',review_token='same',case_store=service.store)


def test_tenant_scoped_idempotency_does_not_collide(service):
    first=service.create(USER,{'order_id':'SIM-006'},'create-key-1')['case']
    second=service.create(Actor('demo-b','operator'),{'order_id':'SIM-009','trigger':'user_requested'},'create-key-1')['case']
    assert first['case_id']!=second['case_id']
    assert second['tenant_id']=='demo-b'


def test_storage_open_failure_is_explicit_and_not_memory_fallback(tmp_path):
    path=tmp_path/'bad.sqlite3';path.write_text('not a sqlite file',encoding='utf-8')
    with pytest.raises(CaseError) as e: CaseStore(path)
    assert e.value.status==503

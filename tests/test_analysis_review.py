"""SQLite/native workflow with explicit graph-result doubles, no MCP/LLM/network claims."""
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
from uuid import uuid4
import pytest
from pathlib import Path

from logimate.analysis_review import AnalysisReview, BUSINESS, validate_analysis
from logimate.cases import Actor, CaseStore, CaseError
from logimate.native_review import NativeReview
from logimate.repository import Repository
from logimate.rules import DOMAINS, diagnose
from logimate.deepseek import ModelUnavailable

USER=Actor('demo-a','operator')
REVIEWER=Actor('demo-a','reviewer',True)


def result_double(order_id='SIM-006'):
    repo=Repository();snapshot=repo.get(order_id,'demo-a')
    return {'simulated':True,'implementation':BUSINESS,'trace_id':str(uuid4()),'tool_transport':'mcp-stdio',
        'dataset_id':repo.dataset.dataset_id,'dataset_sha256':repo.sha256,'status':'answered','model_called':True,
        'records':snapshot.model_dump(mode='json'),
        'diagnosis':diagnose(snapshot,as_of=repo.dataset.as_of,dataset_id=repo.dataset.dataset_id,intent='overall').model_dump(mode='json'),
        'provider_calls':[{'agent':agent,'model':'explicit-result-double-no-LLM','usage':{'total_tokens':0}} for agent in ('coordinator',*DOMAINS)],
        'model_attempts':[{'agent':agent,'status':'response_received'} for agent in ('coordinator',*DOMAINS)],
        'usage':{'prompt_tokens':0,'completion_tokens':0,'total_tokens':0},
        'limitations':['Explicit synthetic graph-result double; not actual MCP/model execution.']}


@pytest.fixture
def service(tmp_path):
    store=CaseStore(tmp_path/'cases.sqlite3');calls=[]
    def prepare():
        def execute(query,tenant):calls.append(query);return result_double()
        return execute
    return AnalysisReview(store,NativeReview(store),Repository(),prepare,planner_mode='fixture',implementation=BUSINESS),calls


def test_completed_receipt_replay_and_review_never_call_runner_again(service):
    s,calls=service;r=s.analyze(USER,{'query':'四域SIM-006'},'analysis-key-1')
    assert len(calls)==1 and r['thread']['status']=='waiting_human' and r['planner_mode']=='fixture'
    assert r['model_responses_this_request']==0
    result=r['thread']['case']['analysis_receipt']['result']
    assert result['model_called'] is False and 'state_conflict' in r['thread']['case']['reasons']
    def forbidden():raise AssertionError('receipt replay must not prepare a model')
    fresh=AnalysisReview(s.store,NativeReview(s.store),s.repo,forbidden,planner_mode='fixture',implementation=BUSINESS)
    replay=fresh.analyze(USER,{'query':'四域SIM-006'},'analysis-key-1')
    assert replay['replayed'] and replay['analysis_id']==r['analysis_id'] and len(calls)==1
    fresh.native.resume(REVIEWER,r['thread']['thread_id'],{'expected_version':1,'action':'confirm_review'},'analysis-review-1')
    current=fresh.get(r['analysis_id'],'demo-a')
    assert current['thread']['status']=='reviewed' and current['recorded_usage']==r['recorded_usage']
    assert current['thread']['case']['analysis_receipt']['result']==result


@pytest.mark.parametrize('phase',['after_receipt','after_case','after_thread'])
def test_receipt_to_binding_crash_retries_without_reanalysis(service,phase):
    s,calls=service
    def fault(at):
        if at==phase:raise RuntimeError('explicit crash boundary')
    s.fault=fault
    with pytest.raises(RuntimeError):s.analyze(USER,{'query':'四域SIM-006'},'analysis-key-1')
    assert len(calls)==1
    fresh=AnalysisReview(s.store,NativeReview(s.store),s.repo,lambda:(_ for _ in ()).throw(AssertionError('no reanalysis')),planner_mode='fixture',implementation=BUSINESS)
    recovered=fresh.get_by_key(USER,'analysis-key-1')
    assert recovered['thread']['status']=='waiting_human'
    with s.store._connection(write=False) as db:
        assert db.execute('SELECT COUNT(*) FROM cases').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM native_threads').fetchone()[0]==1


def test_unrecorded_external_result_stays_uncertain_and_never_retries(service):
    s,calls=service
    s.fault=lambda at:(_ for _ in ()).throw(RuntimeError('lost result')) if at=='after_external_before_receipt' else None
    with pytest.raises(RuntimeError):s.analyze(USER,{'query':'四域SIM-006'},'analysis-key-1')
    state=s.get_by_key(USER,'analysis-key-1')
    assert state['state']=='uncertain' and state['automatic_retry'] is False and len(calls)==1
    with pytest.raises(CaseError) as e:s.analyze(USER,{'query':'四域SIM-006'},'analysis-key-1')
    assert e.value.code=='ANALYSIS_UNRECORDED_ATTEMPT_NO_AUTO_RETRY' and len(calls)==1
    with s.store._connection(write=False) as db:assert db.execute('SELECT COUNT(*) FROM cases').fetchone()[0]==0


@pytest.mark.parametrize('phase,expected',[('after_external_before_receipt','uncertain'),('after_receipt','waiting_human')])
def test_actual_process_exit_recovers_recorded_receipt_or_preserves_uncertainty(service,tmp_path,phase,expected):
    s,_=service;file=tmp_path/'result.json';file.write_text(json.dumps(result_double()),encoding='utf-8')
    code="""import sys,json,os
from pathlib import Path
from logimate.cases import CaseStore,Actor
from logimate.native_review import NativeReview
from logimate.analysis_review import AnalysisReview,BUSINESS
from logimate.repository import Repository
store=CaseStore(sys.argv[1])
def fault(at):
 if at==sys.argv[3]:os._exit(33)
s=AnalysisReview(store,NativeReview(store),Repository(),lambda:lambda q,t:json.loads(Path(sys.argv[2]).read_text(encoding='utf-8')),planner_mode='fixture',implementation=BUSINESS,fault=fault)
s.analyze(Actor('demo-a','operator'),{'query':'四域SIM-006'},'crash-analysis-1')
"""
    p=subprocess.run([sys.executable,'-c',code,str(s.store.path),str(file),phase],capture_output=True,timeout=20)
    assert p.returncode==33,p.stderr
    result=s.get_by_key(USER,'crash-analysis-1')
    assert (result.get('state') or result['thread']['status'])==expected


def test_concurrent_same_key_analyzes_once_and_conflict_does_not_reexecute(service):
    s,calls=service
    with ThreadPoolExecutor(max_workers=2) as pool:r=list(pool.map(lambda _:s.analyze(USER,{'query':'四域SIM-006'},'analysis-key-1'),range(2)))
    assert len(calls)==1 and r[0]['analysis_id']==r[1]['analysis_id']
    with pytest.raises(CaseError) as e:s.analyze(USER,{'query':'different'},'analysis-key-1')
    assert e.value.status==409 and len(calls)==1
    with pytest.raises(CaseError) as e:s.get(r[0]['analysis_id'],'demo-b')
    assert e.value.status==404


@pytest.mark.parametrize('change', ['tenant','diagnosis','tokens','unknown_field'])
def test_invalid_evidence_or_accounting_never_becomes_case(service,change):
    s,_=service;result=result_double()
    if change=='tenant':result['records']['order']['tenant_id']='demo-b'
    if change=='diagnosis':result['diagnosis']['findings'][0]['statement']='fabricated finance root cause'
    if change=='tokens':result['usage']['total_tokens']=99
    if change=='unknown_field':result['unexpected_secret']='must not persist'
    s.runner_factory=lambda:lambda q,t:result
    with pytest.raises(ValueError):s.analyze(USER,{'query':'四域SIM-006'},'analysis-key-1')
    with s.store._connection(write=False) as db:
        assert db.execute('SELECT COUNT(*) FROM cases').fetchone()[0]==0
        assert db.execute('SELECT receipt FROM analysis_receipts').fetchone()[0] is None


def test_unavailable_analysis_does_not_fill_missing_records_from_repository(service):
    s,_=service;result=result_double();result.update(status='unavailable',records={},diagnosis=None,provider_calls=[],model_attempts=[])
    s.runner_factory=lambda:lambda q,t:result
    case=s.analyze(USER,{'query':'四域SIM-006'},'analysis-key-1')['thread']['case']
    assert case['snapshot'] is None and case['diagnosis'] is None and case['order_id'] is None
    assert case['state']=='waiting_human'


def test_missing_model_configuration_is_checked_before_attempt_is_recorded(service):
    s,_=service
    def missing():raise ModelUnavailable('missing',category='configuration')
    s.runner_factory=missing
    with pytest.raises(ModelUnavailable):s.analyze(USER,{'query':'四域SIM-006'},'analysis-key-1')
    with s.store._connection(write=False) as db:assert db.execute('SELECT COUNT(*) FROM analysis_receipts').fetchone()[0]==0


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1]/'docs/evidence/p6/shared-docker-20261104T052414384153/summary.json').exists(), reason='Private historical receipt intentionally excluded from public distribution')
def test_historical_accepted_shared_receipt_validates_without_new_model_or_network(monkeypatch):
    """Parse already-recorded P6.2 data only; never count its tokens as a new measurement."""
    from pathlib import Path
    from logimate.analysis_review import SHARED
    root=Path(__file__).resolve().parents[1]
    gate=json.loads((root/'docs/evidence/p6/shared-docker-20261104T052414384153/summary.json').read_text(encoding='utf-8'))
    result=gate['results']['live-shared-normal']['enhanced']
    monkeypatch.setenv('LOGIMATE_DENSE_BACKEND','milvus')  # Historical receipt's recorded deployment profile.
    query='请从四个业务域整体排查SIM-008退货入库48小时未退款，核实订单、物流、仓储和售后。'
    validated=validate_analysis(result,Repository(),'demo-a',query,planner_mode='live',implementation=SHARED)
    assert validated==result


def test_partial_query_preserves_only_observed_records_and_no_new_diagnosis(service):
    s,_=service;result=result_double();result['records']={'order':result['records']['order']};result['diagnosis']=None
    s.runner_factory=lambda:lambda q,t:result
    case=s.analyze(USER,{'query':'仅查询SIM-006订单'},'analysis-key-1')['thread']['case']
    assert case['snapshot']['order'] and case['snapshot']['aftersales'] is None
    assert case['diagnosis'] is None


def test_bound_response_tamper_fails_closed_without_model_call(service):
    s,calls=service;r=s.analyze(USER,{'query':'四域SIM-006'},'analysis-key-1')
    with s.store._connection(write=True) as db:db.execute('UPDATE analysis_receipts SET response=? WHERE id=?',('{}',r['analysis_id']))
    with pytest.raises(CaseError) as exc:s.get(r['analysis_id'],'demo-a')
    assert exc.value.code=='ANALYSIS_RESPONSE_INTEGRITY_FAILURE' and len(calls)==1

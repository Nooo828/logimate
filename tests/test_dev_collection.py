"""Development adapter guardrails and explicit no-model planners; not target-model scores."""
import dataclasses,json,shutil,subprocess,sys
from pathlib import Path
import pytest
from logimate.dev_collection import select_dev,InputGuard
from logimate.deepseek import ModelUnavailable
from logimate.evaluation_run import RunLedger,collect_cell
from logimate.single_agent import run_agent
from logimate.five_agent import run_five_agent
from logimate.boundary_eval import score
from scripts.collect_boundary_dev import DevPlanner,export,input_hashes

ROOT=Path(__file__).resolve().parents[1]
DATA,DEV=select_dev(ROOT)


def test_only_public_development_inputs_are_selected():
    _,cases=select_dev(ROOT)
    assert len(cases)==16 and all(set(dataclasses.asdict(c))=={'case_id','query'} for c in cases)
    assert {c.case_id for c in cases}=={c['case_id'] for c in DATA['cases'] if c['split']=='dev'}
    with pytest.raises(ValueError):select_dev(ROOT,split='holdout')


def test_cli_refuses_holdout_before_output_creation_or_key_prompt(tmp_path):
    out=tmp_path/'forbidden'
    r=subprocess.run([sys.executable,'-m','scripts.collect_boundary_dev','--live','--split','holdout','--output',str(out)],cwd=ROOT,capture_output=True,timeout=15)
    assert r.returncode==2 and not out.exists() and b'API key' not in r.stdout


def test_frozen_rubric_drift_is_rejected(tmp_path):
    lock=json.loads((ROOT/'eval/boundary_v2.freeze.json').read_text())
    for name in ['eval/boundary_v2.freeze.json',*lock['sources']]:
        target=tmp_path/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,target)
    (tmp_path/'logimate/boundary_eval.py').write_text('changed',encoding='utf-8')
    with pytest.raises(ValueError):select_dev(tmp_path)


@pytest.mark.parametrize('content',[json.dumps({'query':'wrong'}),json.dumps({'query':DEV[0].query,'gold':{}}),
    json.dumps({'query':DEV[0].query,'family':'stock_scope'}),'unrelated question'])
def test_input_guard_blocks_gold_or_wrong_query_before_provider(content):
    class Forbidden:
        def complete(self,*a,**kw):pytest.fail('guard must run before external provider')
    with pytest.raises(ModelUnavailable):InputGuard(Forbidden(),DEV[0].query).complete([{'role':'user','content':content}])


@pytest.mark.parametrize('mode,engine',[('single',run_agent),('five',run_five_agent)])
@pytest.mark.parametrize('case',DEV,ids=lambda c:c.case_id)
def test_actual_local_engines_and_ledger_with_explicit_fixture(tmp_path,mode,engine,case):
    ledger=RunLedger(tmp_path/'r.sqlite3');provider=InputGuard(ledger.provider(DevPlanner(),case.case_id,mode,'fixture'),case.query)
    public=dataclasses.asdict(case)
    r=collect_cell(ledger,public,mode,'fixture',lambda:engine(provider,case.query,'demo-a'))
    assert not r.get('collector_failure_envelope') and provider.verified_calls>0
    assert ledger.accounting()['known_tokens']==ledger.accounting()['billing_unknown_attempts']==0
    packet={'dataset_sha256':input_hashes()['eval/boundary_v2.json'],'split':'dev','planner_mode':'fixture','rows':ledger.rows()}
    scores=score(DATA,packet,ROOT)
    assert scores['summary'][mode]['counts']['task_exact']==1 and scores['summary'][mode]['counts']['boundary_failure']==0


def test_non_dev_attempt_in_ledger_cannot_be_hidden_by_empty_rows(tmp_path):
    ledger=RunLedger(tmp_path/'r.sqlite3')
    with ledger.db() as db:db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)',('synthetic-attempt','carrier_progress-1','single','started',None,None))
    (tmp_path/'run.json').write_text(json.dumps({'input_sha256':input_hashes()}),encoding='utf-8')
    result=export(tmp_path,ledger,DATA,'live','stopped',DEV)
    assert not result['dev_only_verified'] and result['non_dev_attempts_detected']==1 and result['status']=='unexpected_split_rejected'


def test_provider_messages_are_query_derived_without_evaluator_labels(tmp_path):
    case=DEV[0];seen=[]
    class Observer(DevPlanner):
        def complete(self,messages,*a,**kw):seen.append(messages);return super().complete(messages,*a,**kw)
    run_agent(InputGuard(Observer(),case.query),case.query,'demo-a')
    wire=json.dumps(seen,ensure_ascii=False)
    assert case.case_id not in wire and '"gold"' not in wire and '"family"' not in wire

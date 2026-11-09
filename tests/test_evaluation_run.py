"""Fault boundaries and provider accounting doubles; no external model calls."""
import json,sqlite3,subprocess,sys
from pathlib import Path
import pytest
from logimate.evaluation_run import RunLedger,collect_cell,execution_plan
from logimate.deepseek import ModelUnavailable
from scripts.collect_paired_eval import CalibrationPlanner
from scripts.inspect_paired_eval import inspect
from logimate.single_agent import run_agent
from logimate.five_agent import run_five_agent

ROOT=Path(__file__).resolve().parents[1]
DATA=json.loads((ROOT/'eval/e2e_v1.json').read_text(encoding='utf-8'))


def test_rotation_has_every_planned_cell_once_and_balances_first_mode():
    plan=execution_plan(DATA['cases']);assert len(plan)==18 and len({(c['case_id'],m) for c,m in plan})==18
    assert [m for _,m in plan[::3]]==['single','five','shared']*2


@pytest.mark.parametrize('mode,fn',[('single',run_agent),('five',run_five_agent)])
@pytest.mark.parametrize('case',DATA['cases'],ids=lambda c:c['case_id'])
def test_actual_business_engines_fixture_receipts_reconcile(tmp_path,mode,fn,case):
    ledger=RunLedger(tmp_path/'run.sqlite3');provider=ledger.provider(CalibrationPlanner(),case['case_id'],mode,'fixture')
    result=collect_cell(ledger,case,mode,'fixture',lambda:fn(provider,case['query'],'demo-a'))
    assert result['status']=='answered' and not result.get('collector_failure_envelope')
    assert ledger.accounting()['responses_recorded']>=2 and ledger.accounting()['known_tokens']==0
    assert ledger.rows()[0]['response']==result


def test_failed_parse_after_provider_response_retains_known_tokens(tmp_path):
    ledger=RunLedger(tmp_path/'r.sqlite3');case=DATA['cases'][0]
    class LiveDouble:
        def complete(self):return {},{'provider':'deepseek','response_id':'explicit-double','usage':{'total_tokens':11}}
    provider=ledger.provider(LiveDouble(),case['case_id'],'single','live')
    def execute():provider.complete();raise ValueError('sensitive text must not be serialized')
    r=collect_cell(ledger,case,'single','live',execute)
    assert r['collector_failure_envelope'] and r['usage']['total_tokens']==11
    assert ledger.accounting()['known_tokens']==11 and ledger.accounting()['billing_unknown_attempts']==0
    assert 'sensitive text' not in ledger.path.read_bytes().decode(errors='ignore')


def test_budget_prevents_new_external_attempt_and_duplicate_cell_never_reruns(tmp_path):
    ledger=RunLedger(tmp_path/'r.sqlite3',max_attempts=1);case=DATA['cases'][0]
    p=ledger.provider(CalibrationPlanner(),case['case_id'],'single','fixture')
    ledger.begin(case,'single','fixture');p.complete([{'content':case['query']}])
    with pytest.raises(ModelUnavailable):p.complete([{'content':case['query']}])
    assert ledger.accounting()['attempts_started']==1 and ledger.stop_reason=='budget_exhausted'
    with pytest.raises(sqlite3.IntegrityError):collect_cell(ledger,case,'single','fixture',lambda:pytest.fail('duplicate executes'))


def test_sensitive_provider_metadata_is_withheld_and_billing_unknown(tmp_path):
    ledger=RunLedger(tmp_path/'r.sqlite3',secret='explicit-private-value');case=DATA['cases'][0]
    class Bad:
        def complete(self):return {},{'provider':'deepseek','response_id':'explicit-private-value','usage':{'total_tokens':1}}
    p=ledger.provider(Bad(),case['case_id'],'single','live');ledger.begin(case,'single','live')
    with pytest.raises(ModelUnavailable):p.complete()
    report=inspect(ledger.path);assert report['billing_unknown_attempts']==1 and report['known_tokens']==0
    assert 'explicit-private-value' not in json.dumps(report)


def test_auth_failure_stops_following_cells_and_preserves_unknown_billing(tmp_path):
    ledger=RunLedger(tmp_path/'r.sqlite3');case=DATA['cases'][0]
    class Bad:
        def complete(self):raise ModelUnavailable('Do not persist raw errors',category='provider_http',http_status=401)
    p=ledger.provider(Bad(),case['case_id'],'single','live');ledger.begin(case,'single','live')
    with pytest.raises(ModelUnavailable):p.complete()
    assert ledger.stop_reason=='provider_auth_billing_or_rate_limit'
    assert ledger.accounting()['billing_unknown_attempts']==1


def test_actual_process_exit_preserves_started_attempt_without_replay(tmp_path):
    path=tmp_path/'crash.sqlite3'
    code="""import os,sys
from logimate.evaluation_run import RunLedger
class Exit:
 def complete(self):os._exit(37)
l=RunLedger(sys.argv[1]);c={'case_id':'explicit-crash','query':'SIM-006'};l.begin(c,'single','live')
l.provider(Exit(),c['case_id'],'single','live').complete()
"""
    r=subprocess.run([sys.executable,'-c',code,str(path)],cwd=ROOT,timeout=15,capture_output=True)
    assert r.returncode==37
    before=path.read_bytes();state=inspect(path)
    assert state['cells'][0]['state']=='running' and state['billing_unknown_attempts']==1 and not state['automatic_retry']
    assert path.read_bytes()==before

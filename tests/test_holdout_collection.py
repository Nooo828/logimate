"""Explicit fixture collection and exposure reservation tests; no target-model execution."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json,shutil,subprocess,sys
from pathlib import Path
import pytest
from logimate.holdout_collection import select_holdout,reserve_exposure,complete_exposure
from logimate.dev_collection import select_dev,InputGuard
from logimate.evaluation_run import RunLedger,collect_cell
from logimate.boundary_eval import score
from logimate.single_agent import run_agent
from logimate.five_agent import run_five_agent
from scripts.collect_boundary_holdout import HoldoutPlanner,input_hashes
from scripts.verify_boundary_holdout_docker import parent_plan,build_files

ROOT=Path(__file__).resolve().parents[1]
DATA,HELD=select_holdout(ROOT,acknowledged=True)


def test_explicit_scope_and_public_input_separation():
    with pytest.raises(ValueError):select_holdout(ROOT)
    _,dev=select_dev(ROOT)
    assert len(HELD)==16 and not ({c.case_id for c in HELD}&{c.case_id for c in dev})
    assert all(set(asdict(c))=={'case_id','query'} for c in HELD)


@pytest.mark.parametrize('module',['scripts.collect_boundary_holdout','scripts.verify_boundary_holdout_docker'])
def test_live_cli_without_ack_does_not_prompt_for_credentials(module):
    r=subprocess.run([sys.executable,'-m',module,'--live'],cwd=ROOT,capture_output=True,timeout=15)
    assert r.returncode!=0 and b'API key' not in r.stdout


def test_no_release_means_no_live_access(tmp_path):
    lock=json.loads((ROOT/'eval/boundary_v2.freeze.json').read_text())
    for n in ['eval/boundary_v2.freeze.json',*lock['sources'],'eval/routing_v1.json','eval/e2e_v1.json','eval/e2e_v1_1.json']:
        target=tmp_path/n;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/n,target)
    with pytest.raises(FileNotFoundError):select_holdout(tmp_path,acknowledged=True,live=True)
    (tmp_path/'eval/holdout_release_v2.json').write_text(json.dumps({'model':'wrong','split':'holdout','prompt_or_rubric_tuned_on_dev':False}),encoding='utf-8')
    with pytest.raises(ValueError):select_holdout(tmp_path,acknowledged=True,live=True)


def test_reservation_is_exclusive_even_with_concurrent_callers(tmp_path):
    path=tmp_path/'exposure.json'
    def reserve(i):
        try:reserve_exposure(path,{'owner':i});return True
        except FileExistsError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:out=list(pool.map(reserve,range(2)))
    assert sum(out)==1 and json.loads(path.read_text())['state']=='reserved_before_target_calls'
    complete_exposure(path,{'state':'failed','known_accounting':None})
    with pytest.raises(FileExistsError):reserve_exposure(path,{'owner':3})


def test_actual_process_exit_retains_exposure_marker(tmp_path):
    p=tmp_path/'crash.json'
    code="from pathlib import Path; import os,sys; from logimate.holdout_collection import reserve_exposure; reserve_exposure(Path(sys.argv[1]),{'synthetic_test':True}); os._exit(41)"
    r=subprocess.run([sys.executable,'-c',code,str(p)],cwd=ROOT,timeout=15,capture_output=True)
    assert r.returncode==41 and json.loads(p.read_text())['state']=='reserved_before_target_calls'
    with pytest.raises(FileExistsError):reserve_exposure(p,{})


@pytest.mark.parametrize('mode,engine',[('single',run_agent),('five',run_five_agent)])
@pytest.mark.parametrize('case',HELD,ids=lambda c:c.case_id)
def test_actual_engines_with_explicit_fixture_not_target_model(tmp_path,mode,engine,case):
    ledger=RunLedger(tmp_path/'run.sqlite3');provider=InputGuard(ledger.provider(HoldoutPlanner(),case.case_id,mode,'fixture'),case.query)
    r=collect_cell(ledger,asdict(case),mode,'fixture',lambda:engine(provider,case.query,'demo-a'))
    assert not r.get('collector_failure_envelope')
    packet={'dataset_sha256':input_hashes()['eval/boundary_v2.json'],'split':'holdout','planner_mode':'fixture','rows':ledger.rows()}
    counts=score(DATA,packet,ROOT)['summary'][mode]['counts']
    assert counts['task_exact']==1 and counts['boundary_failure']==0
    assert ledger.accounting()['known_tokens']==0


def test_new_image_gate_uses_accepted_dev_worker_without_enabling_target_calls(tmp_path):
    parent=parent_plan(ROOT);assert parent['tag']=='logimate-p85-20261105t133941236862-worker:verification'
    recipe,runtime=build_files(ROOT,tmp_path,parent['tag'],'logimate-p86-test')
    assert 'DEEPSEEK_API_KEY' not in runtime.read_text() and 'pip install' not in recipe.read_text()


def test_published_live_release_validation_does_not_reserve_or_call_a_model():
    marker=ROOT/'docs/evidence/p8/holdout-target-exposure-v2.json';assert not marker.exists()
    _,cases=select_holdout(ROOT,acknowledged=True,live=True)
    assert len(cases)==16 and not marker.exists()
    with pytest.raises(ValueError):select_holdout(ROOT,acknowledged=True,live=True,model='different-model')


def test_published_release_rejects_changed_collector_before_key_input(tmp_path):
    release=json.loads((ROOT/'eval/holdout_release_v2.json').read_text(encoding='utf-8'))
    for n in ['eval/holdout_release_v2.json',*release['runtime_sha256']]:
        p=tmp_path/n;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/n,p)
    (tmp_path/'scripts/collect_boundary_holdout.py').write_text('changed collector',encoding='utf-8')
    with pytest.raises(ValueError):select_holdout(tmp_path,acknowledged=True,live=True)

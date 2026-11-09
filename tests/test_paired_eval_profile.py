"""Image provenance/layout, keyless Compose, and failure accounting; no paid calls."""
import json,os,shutil,subprocess
from pathlib import Path,PurePosixPath
import pytest
from scripts.verify_paired_eval_docker import parent_plan,build_files
from scripts.collect_paired_eval import export,input_hashes
from logimate.evaluation_run import RunLedger

ROOT=Path(__file__).resolve().parents[1]


def test_layout_inherits_accepted_image_and_copies_all_scored_inputs(tmp_path):
    parent=parent_plan(ROOT);recipe,runtime=build_files(ROOT,tmp_path,parent['tag'],'logimate-p82-test')
    config=json.loads(runtime.read_text(encoding='utf-8'));worker=config['services']['worker']
    assert worker['build']['network']=='none' and 'ports' not in worker and worker['healthcheck']['disable']
    assert 'DEEPSEEK_API_KEY' not in runtime.read_text(encoding='utf-8')
    assert 'pip install' not in recipe.read_text() and 'apt-get' not in recipe.read_text()
    targets=[]
    for line in recipe.read_text().splitlines():
        if not line.startswith('COPY '):continue
        *sources,dest=line.split()[1:]
        for source in sources:targets.append((ROOT/source,PurePosixPath('/app')/dest/(PurePosixPath(source).name if (ROOT/source).is_file() else '')))
    for pattern in ('logimate/*.py','scripts/*.py','eval/*.json','data/**/*.json','prompts/*.txt','requirements-retrieval.lock','requirements-checkpoint.lock'):
        for f in ROOT.glob(pattern):
            assert PurePosixPath('/app')/f.relative_to(ROOT).as_posix() in [dest/(f.relative_to(src).as_posix() if src.is_dir() else '') for src,dest in targets if f==src or src in f.parents]


def test_actual_compose_config_does_not_need_a_model_key(tmp_path):
    if not shutil.which('docker'):pytest.skip('Docker CLI not installed')
    _,runtime=build_files(ROOT,tmp_path,parent_plan(ROOT)['tag'],'logimate-p82-test')
    env=dict(os.environ);env.pop('DEEPSEEK_API_KEY',None)
    r=subprocess.run(['docker','compose','-f',str(runtime),'config','--quiet'],env=env,capture_output=True,timeout=20)
    assert r.returncode==0,r.stderr


@pytest.mark.parametrize('drift',[False,True])
def test_export_marks_unknown_billing_and_input_drift_explicitly(tmp_path,drift):
    ledger=RunLedger(tmp_path/'ledger.sqlite3');dataset=json.loads((ROOT/'eval/e2e_v1.json').read_text(encoding='utf-8'))
    with ledger.db() as db:db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)',('attempt-id','finance-recorded','single','started',None,None))
    (tmp_path/'run.json').write_text(json.dumps({'input_sha256':{} if drift else input_hashes()}),encoding='utf-8')
    result=export(tmp_path,ledger,dataset,'live','collected')
    assert result['status']==('input_drift' if drift else 'collected_billing_incomplete')
    assert not result['billing_is_complete'] and result['accounting']['billing_unknown_attempts']==1
    assert result['output_sha256']['ledger.sqlite3'] and result['known_tokens']==0


def test_rejected_score_does_not_leave_an_old_success_report(tmp_path):
    ledger=RunLedger(tmp_path/'ledger.sqlite3');dataset=json.loads((ROOT/'eval/e2e_v1.json').read_text(encoding='utf-8'))
    case=dataset['cases'][0];ledger.begin(case,'single','fixture');ledger.finish(case['case_id'],'single',{'simulated':False})
    (tmp_path/'scores.json').write_text('{"complete":true}',encoding='utf-8')
    r=export(tmp_path,ledger,dataset,'fixture','failed')
    assert r['status']=='scoring_rejected' and json.loads((tmp_path/'scores.json').read_text())['complete'] is False

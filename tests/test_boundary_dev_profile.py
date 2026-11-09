"""New image manifest gates and dev-only operational options; no Docker engine or model calls."""
import json,os,shutil,subprocess,sys
from pathlib import Path,PurePosixPath
import pytest
from scripts.verify_boundary_dev_docker import parent_plan,build_files

ROOT=Path(__file__).resolve().parents[1]


def test_parent_uses_actual_p82_worker_and_p84_frozen_protocol():
    p=parent_plan(ROOT)
    assert p['tag']=='logimate-p82-20261105t074259913219-worker:verification'
    assert p['id']=='sha256:350900375443f58ee5a341882837f5e096cad3ba0de2941ebf0b1999a00c36fc'
    lock=json.loads((ROOT/'eval/boundary_v2.freeze.json').read_text())
    assert lock['holdout_execution_enabled'] is False


def test_all_inputs_are_copied_without_downloads_or_public_ports(tmp_path):
    recipe,runtime=build_files(ROOT,tmp_path,parent_plan(ROOT)['tag'],'logimate-p85-test')
    text=recipe.read_text();config=json.loads(runtime.read_text());w=config['services']['worker']
    assert 'pip install' not in text and 'apt-get' not in text and 'ports' not in w
    assert w['build']['network']=='none' and 'DEEPSEEK_API_KEY' not in runtime.read_text()
    targets=[]
    for line in text.splitlines():
        if not line.startswith('COPY '):continue
        *sources,dest=line.split()[1:]
        for source in sources:targets.append((ROOT/source,PurePosixPath('/app')/dest/(PurePosixPath(source).name if (ROOT/source).is_file() else '')))
    for pattern in ('logimate/*.py','scripts/*.py','eval/*.json','data/**/*.json','prompts/*.txt','requirements-retrieval.lock','requirements-checkpoint.lock'):
        for f in ROOT.glob(pattern):
            assert PurePosixPath('/app')/f.relative_to(ROOT).as_posix() in [dest/(f.relative_to(src).as_posix() if src.is_dir() else '') for src,dest in targets if f==src or src in f.parents]


def test_actual_compose_config_is_keyless(tmp_path):
    if not shutil.which('docker'):pytest.skip('Docker CLI not installed')
    _,runtime=build_files(ROOT,tmp_path,parent_plan(ROOT)['tag'],'logimate-p85-test')
    env=dict(os.environ);env.pop('DEEPSEEK_API_KEY',None)
    r=subprocess.run(['docker','compose','-f',str(runtime),'config','--quiet'],env=env,capture_output=True,timeout=20)
    assert r.returncode==0,r.stderr


def test_container_wrapper_rejects_holdout_before_creating_evidence():
    folder=ROOT/'docs/evidence/p8';before=set(folder.glob('boundary-dev-docker-*'))
    r=subprocess.run([sys.executable,'-m','scripts.verify_boundary_dev_docker','--split','holdout','--live'],cwd=ROOT,capture_output=True,timeout=15)
    assert r.returncode==2 and set(folder.glob('boundary-dev-docker-*'))==before
    assert b'API key' not in r.stdout

"""Offline image layout and accepted-parent gates; no Docker or model is simulated as accepted."""
import hashlib
import json
import os
import shutil
import subprocess
import pytest
from pathlib import Path, PurePosixPath
from scripts.verify_analysis_review_docker import build_files, parent_plan, GATE


def test_child_sources_are_copied_and_build_config_needs_no_model_key(tmp_path):
    root=Path(__file__).resolve().parents[1];parent=parent_plan(root)
    recipe,runtime,config=build_files(root,tmp_path,parent['tag'],'logimate-test',18087)
    text=recipe.read_text(encoding='utf-8');assert 'RUN ' not in text and 'network' not in text
    assert '${DEEPSEEK_API_KEY' not in runtime.read_text(encoding='utf-8')
    assert config['services']['api']['build']['network']=='none'
    targets=[]
    for line in text.splitlines():
        if not line.startswith('COPY '):continue
        *sources,dest=line.split()[1:]
        target=PurePosixPath('/app')/dest
        for source in sources:
            targets.append((root/source,target/(PurePosixPath(source).name if (root/source).is_file() else '')))
    for pattern in ('logimate/*.py','scripts/*.py','data/**/*.json','prompts/*.txt','requirements-retrieval.lock','requirements-checkpoint.lock'):
        for file in root.glob(pattern):
            expected=PurePosixPath('/app')/file.relative_to(root).as_posix()
            actual=[dest/(file.relative_to(src).as_posix() if src.is_dir() else '') for src,dest in targets if file==src or src in file.parents]
            assert expected in actual,(file,actual)


def test_parent_provenance_uses_accepted_old_files_and_locks_while_child_may_change():
    root=Path(__file__).resolve().parents[1];parent=parent_plan(root)
    gate=json.loads((root/GATE/'summary.json').read_text(encoding='utf-8'))
    assert parent['file_sha256']==gate['child_file_sha256']
    assert parent['file_sha256']['logimate/api.py']!=hashlib.sha256((root/'logimate/api.py').read_bytes()).hexdigest()


def test_actual_compose_config_separates_keyless_build_from_runtime_interpolation(tmp_path):
    if not shutil.which('docker'):pytest.skip('Docker CLI not installed; no engine required')
    root=Path(__file__).resolve().parents[1]
    _,runtime,config=build_files(root,tmp_path,parent_plan(root)['tag'],'logimate-layout-test',18087)
    env=dict(os.environ);env.pop('DEEPSEEK_API_KEY',None)
    argv=['docker','compose','-f',str(runtime),'config','--quiet']
    def run():return subprocess.run(argv,env=env,capture_output=True,timeout=30)
    assert run().returncode==0
    config['services']['api']['environment']['DEEPSEEK_API_KEY']='${DEEPSEEK_API_KEY:?required}'
    runtime.write_text(json.dumps(config),encoding='utf-8')
    assert run().returncode!=0
    env['DEEPSEEK_API_KEY']='explicit-compose-only-placeholder-not-a-provider-key'
    assert run().returncode==0
    assert env['DEEPSEEK_API_KEY'] not in runtime.read_text(encoding='utf-8')

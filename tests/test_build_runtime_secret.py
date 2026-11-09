"""Real Compose interpolation/bake regression. No engine, image download or model calls."""
import copy
import json
import os
import shutil
import subprocess

import pytest
from scripts.verify_shared_graph_docker import write_runtime


@pytest.fixture
def config(tmp_path):
    (tmp_path/'Dockerfile').write_text('FROM scratch\n', encoding='utf-8')
    return {'services': {'api': {'image': 'logimate-secret-config-unit:verification',
        'build': {'context': str(tmp_path), 'dockerfile': 'Dockerfile'},
        'environment': {'LOGIMATE_RESILIENT_RETRIEVAL': '1',
            'DEEPSEEK_API_KEY': '${DEEPSEEK_API_KEY:?Hidden runtime key required}'}}}}


def compose(path, argv, key=None):
    docker = shutil.which('docker')
    if not docker: pytest.skip('Docker CLI unavailable for real configuration-only test')
    env = dict(os.environ)
    for name in ('DEEPSEEK_API_KEY', 'LOGIMATE_MODEL', 'COMPOSE_FILE', 'COMPOSE_ENV_FILES'):
        env.pop(name, None)
    if key: env['DEEPSEEK_API_KEY'] = key
    return subprocess.run([docker, 'compose', '-p', 'logimate-secret-config-unit', '-f', str(path), *argv],
        cwd=path.parent, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)


def test_build_bake_print_resolves_without_model_key(tmp_path, config):
    path = tmp_path/'build.json'; original = copy.deepcopy(config)
    write_runtime(path, config)
    rendered = json.loads(path.read_text(encoding='utf-8'))
    assert 'DEEPSEEK_API_KEY' not in rendered['services']['api']['environment']
    assert config == original
    result = compose(path, ['build', '--print', 'api'])
    assert result.returncode == 0, result.stderr
    bake = json.loads(result.stdout)
    assert 'api' in bake['target'] and 'DEEPSEEK_API_KEY' not in result.stdout


def test_runtime_still_fails_closed_without_key(tmp_path, config):
    path = tmp_path/'runtime.json'; write_runtime(path, config, live=True)
    result = compose(path, ['config', '--quiet'])
    assert result.returncode != 0 and 'Hidden runtime key required' in result.stderr


def test_runtime_accepts_key_from_environment_without_persisting_it(tmp_path, config):
    path = tmp_path/'runtime.json'; write_runtime(path, config, live=True)
    marker = 'explicit-unit-only-non-provider-key'
    result = compose(path, ['config', '--quiet'], key=marker)
    assert result.returncode == 0, result.stderr
    assert marker not in path.read_text(encoding='utf-8')
    assert '${DEEPSEEK_API_KEY:?' in path.read_text(encoding='utf-8')

"""Local-parent provenance rejects substitutions; no real Docker or network in unit tests."""
import ast
import copy
import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from scripts.local_image_parent import plan, validate_image, validate_child, dockerfile, file_probe, REFERENCE

ROOT = Path(__file__).resolve().parents[1]
TAG = 'logimate-p62-20261104t022146289635-api:verification'
ID = 'sha256:' + 'a'*64
LAYERS = ['sha256:' + 'b'*64, 'sha256:' + 'c'*64]


def image():
    return {'Id': ID, 'RepoTags': [TAG], 'Os': 'linux', 'Architecture': 'amd64',
            'Config': {'Env': ['PATH=/usr/local/bin']}, 'RootFS': {'Layers': LAYERS}}


@pytest.fixture
def frozen_parent_root(tmp_path):
    # P7 legitimately changes app sources. Historical P6 parent checks use the frozen inputs.
    with zipfile.ZipFile(ROOT.parent/'logimate-p62-shared-graph-accepted-2026-11-04.zip') as z:
        report=json.loads(z.read(REFERENCE+'/summary.json'))
        names=[REFERENCE+'/summary.json',REFERENCE+'/service-images.txt']
        names += [n for n in report['input_sha256'] if n.startswith('logimate/') or n=='requirements-retrieval.lock']
        for name in names:
            target=tmp_path/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(z.read(name))
    return tmp_path


def test_recorded_real_image_can_only_be_selected_with_current_application_and_lock(frozen_parent_root):
    selected = plan(frozen_parent_root)
    assert selected['images']['api']['tag'] == TAG
    assert selected['images']['api']['id'] == 'sha256:cb3ad377c034f90916fed346538d5a5821456c1d0961902efde6c70f3bfb3e60'
    assert selected['file_sha256']['requirements-retrieval.lock'] == hashlib.sha256((ROOT/'requirements-retrieval.lock').read_bytes()).hexdigest()


@pytest.mark.parametrize('field,value', [('Id', 'sha256:'+'d'*64), ('RepoTags', ['foreign:latest']),
    ('Os', 'windows'), ('Architecture', 'arm64'), ('RootFS', {'Layers': []})])
def test_parent_substitution_is_rejected(field, value):
    actual = image(); actual[field] = value
    with pytest.raises(ValueError): validate_image(actual, {'tag': TAG, 'id': ID})


def test_parent_credential_environment_is_rejected():
    actual = image(); actual['Config']['Env'].append('DEEPSEEK_API_KEY=explicit-test-value')
    with pytest.raises(ValueError, match='credential'): validate_image(actual, {'tag': TAG, 'id': ID})


def test_child_must_inherit_verified_layers_in_order():
    validate_child({'RootFS': {'Layers': LAYERS+['sha256:'+'d'*64]}}, LAYERS)
    with pytest.raises(ValueError): validate_child({'RootFS': {'Layers': list(reversed(LAYERS))}}, LAYERS)


def test_local_recipe_copies_current_code_and_retains_runtime_without_dependency_downloads():
    source = (ROOT/'Dockerfile.retrieval').read_text(encoding='utf-8')
    recipe = dockerfile(source, TAG)
    assert recipe.startswith('FROM '+TAG+'\n') and 'RUN ' not in recipe
    assert [line for line in recipe.splitlines() if line.startswith('COPY ')] == [line for line in source.splitlines() if line.startswith('COPY ')]
    assert 'USER 10001' in recipe and 'HEALTHCHECK ' in recipe and 'CMD ' in recipe
    with pytest.raises(ValueError): dockerfile(source, 'foreign:latest')
    ast.parse(file_probe({'requirements-retrieval.lock':'a'*64}))


def test_record_provenance_rejects_file_drift(tmp_path,frozen_parent_root):
    selected = plan(frozen_parent_root)
    report = json.loads((frozen_parent_root/REFERENCE/'summary.json').read_text(encoding='utf-8'))
    tmp_path=tmp_path/'drift'
    reference = tmp_path/REFERENCE; reference.mkdir(parents=True)
    (reference/'summary.json').write_text(json.dumps(report), encoding='utf-8')
    for name in selected['file_sha256']:
        target = tmp_path/name; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((frozen_parent_root/name).read_bytes())
    (tmp_path/'requirements-retrieval.lock').write_text('drift', encoding='utf-8')
    with pytest.raises(ValueError, match='changed'): plan(tmp_path)

"""Explicit doubles for contracts/failure paths. Real joint containers are separate acceptance."""
import copy
import hashlib
import json

import pytest
pytest.importorskip('jieba')
from fastapi.testclient import TestClient
from logimate.api import create_app
from logimate.retrieval import KnowledgeSearch, ROOT
from scripts.smoke_milvus_api import compare_response, exact_service
from scripts.verify_milvus_api import CRITICAL, GATE, joint_runtime, validate_gate


def test_joint_profile_is_scoped_readonly_and_milvus_only(tmp_path):
    path = joint_runtime(tmp_path, 'local-verification', tmp_path / 'public-cache', 18081)
    api = json.loads(path.read_text(encoding='utf-8'))['services']['api']
    assert api['environment']['LOGIMATE_DENSE_BACKEND'] == 'milvus'
    assert api['environment']['LOGIMATE_MILVUS_URI'] == 'http://milvus:19530'
    assert api['ports'] == ['127.0.0.1:18081:8000']
    assert api['volumes'][0]['read_only'] is True
    assert api['volumes'][0]['target'] == '/models'
    assert api['depends_on'] == {'milvus': {'condition': 'service_healthy'}}
    assert not any('KEY' in name or 'PASSWORD' in name for name in api['environment'])
    assert 'container_name' not in api


@pytest.mark.parametrize('change', ['status', 'cleanup', 'steps', 'comparisons', 'input'])
def test_joint_gate_rejects_incomplete_or_drifted_baseline(change):
    report = json.loads((ROOT / GATE).read_text(encoding='utf-8'))
    # This tests the historical gate against its frozen inputs, not the evolving P6 workspace.
    hashes = {name: report['input_sha256'][name] for name in CRITICAL}
    validate_gate(report, hashes)
    if change == 'status': report['status'] = 'failed'
    if change == 'cleanup': report['cleanup_passed'] = False
    if change == 'steps': report['steps'][0]['exit_code'] = 1
    if change == 'comparisons': report['after_restart'].pop()
    if change == 'input': hashes[CRITICAL[0]] = 'drifted'
    with pytest.raises(ValueError): validate_gate(report, hashes)


def test_exact_comparison_does_not_inherit_milvus_env_or_change_server_setting(monkeypatch):
    monkeypatch.setenv('LOGIMATE_DENSE_BACKEND', 'milvus')
    assert exact_service().dense_backend is None
    import os
    assert os.environ['LOGIMATE_DENSE_BACKEND'] == 'milvus'


def test_api_fails_closed_then_recovers_without_recreating_app(monkeypatch):
    from logimate import retrieval
    class Encoder:
        def query_embed(self, queries): return [[1.0] + [0.0] * 511]
    class FlakyMilvus:
        name = 'milvus-hnsw'
        failed = True
        def search(self, vector, candidates, *args):
            if self.failed: raise RuntimeError('explicit simulated DB outage with PRIVATE text')
            return [(c['chunk_id'], 0.9 - i * .01) for i, c in enumerate(candidates[:10])]
    backend = FlakyMilvus()
    service = KnowledgeSearch(encoder=Encoder(), dense_backend=backend)
    monkeypatch.setattr(retrieval, 'KnowledgeSearch', lambda: service)
    with TestClient(create_app(token='joint-unit-test')) as client:
        headers = {'Authorization': 'Bearer joint-unit-test'}
        for mode in ('dense', 'hybrid'):
            response = client.post('/v1/knowledge/search', headers=headers, json={'query': '退款', 'mode': mode})
            assert response.status_code == 503 and 'hits' not in response.json()
            assert 'PRIVATE' not in response.text
        keyword = client.post('/v1/knowledge/search', headers=headers, json={'query': '退款', 'mode': 'keyword'})
        assert keyword.status_code == 200 and keyword.json()['backend'] == 'in-memory-exact'
        backend.failed = False
        recovered = client.post('/v1/knowledge/search', headers=headers, json={'query': '退款', 'mode': 'dense'})
        assert recovered.status_code == 200 and recovered.json()['backend'] == 'milvus-hnsw'


@pytest.mark.parametrize('change', ['backend', 'corpus', 'tenant', 'duplicate'])
def test_http_comparison_rejects_false_success_or_evidence(change):
    expected = KnowledgeSearch().search('退款', 'demo-a', mode='keyword')
    body = copy.deepcopy(expected)
    body['backend'] = 'milvus-hnsw'
    # For a dense response the comparison expects Milvus, regardless of the exact oracle backend.
    expected['mode'] = body['mode'] = 'dense'
    if change == 'backend': body['backend'] = 'in-memory-exact'
    if change == 'corpus': body['corpus_sha256'] = 'stale'
    if change == 'tenant': body['hits'][0]['tenant_id'] = 'demo-b'
    if change == 'duplicate': body['hits'].append(copy.deepcopy(body['hits'][0]))
    with pytest.raises(AssertionError): compare_response(body, expected, 'dense')

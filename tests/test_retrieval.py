import json
from pathlib import Path

import pytest
pytest.importorskip('jieba')
from fastapi.testclient import TestClient
from logimate import retrieval as r
from logimate.api import create_app
from scripts.evaluate_retrieval import score


def test_filters_apply_before_bm25_and_dense_scoring():
    corpus = json.loads(r.CORPUS.read_text(encoding='utf-8'))
    pieces = r.chunks(corpus)
    allowed = [c for c in pieces if r.eligible(c,'demo-a','2026-10-28')]
    assert len(allowed) == 12 and len(pieces) == 16
    assert not any(c['section_id'] in {'foreign','draft','expired','future'} for c in allowed)
    assert all(c['domain']=='warehouse' for c in allowed if r.eligible(c,'demo-a','2026-10-28','warehouse'))
    assert not r.eligible(pieces[0], 'demo-b', '2026-10-28')


def test_exact_identifiers_are_not_prefix_tokens():
    assert 'sku-ax7' in r.tokenize('查询SKU-AX7')
    assert 'sku-ax7' not in r.tokenize('查询SKU-AX70')


def test_heading_table_and_chunk_limit_preserve_context():
    corpus=json.loads(r.CORPUS.read_text(encoding='utf-8'))
    corpus['documents']=[corpus['documents'][6]]
    output=r.chunks(corpus,max_chars=100)
    assert all(len(c['body'])<=100 and '晚到赔付' in c['title_path'] for c in output)
    table=next(c['body'] for c in output if '| 材料 |' in c['body'])
    assert '| 送达回执 |' in table
    assert len({c['chunk_id'] for c in output})==len(output)


def test_oversized_table_refuses_silent_header_loss():
    corpus=json.loads(r.CORPUS.read_text(encoding='utf-8'))
    corpus['documents']=[corpus['documents'][0]]
    corpus['documents'][0]['sections'][0]['body']='| 表头 |\n'+'| 资料 |\n'*60
    with pytest.raises(ValueError,match='table too large'):r.chunks(corpus,max_chars=100)


def test_rrf_uses_rank_and_does_not_mix_raw_score_units():
    ranking=r.rrf([[('a',10000),('b',.001)],[('b',.4),('c',.3)]],60)
    assert ranking[0][0]=='b' and ranking[0][1]==pytest.approx(1/62+1/61)
    with pytest.raises(ValueError):r.rrf([[('a',1),('a',.5)]])


def test_metrics_distinguish_recall_hit_mrr_and_precision():
    row=score(['b','c'],['a','b','x','y','z'])
    assert row=={'recall_at_5':.5,'hit_at_5':1.0,'mrr':.5,'gold_precision_at_5':.2}
    with pytest.raises(ValueError):score([],['a'])


def test_stale_corpus_is_rejected(tmp_path):
    index=json.loads(r.INDEX.read_text(encoding='utf-8')); index['corpus_sha256']='bad'
    path=tmp_path/'index.json';path.write_text(json.dumps(index),encoding='utf-8')
    with pytest.raises(ValueError,match='stale'):r.KnowledgeSearch(path)


def test_missing_model_fails_closed_but_keyword_stays_available(monkeypatch):
    service=r.KnowledgeSearch()
    monkeypatch.setattr(r,'load_encoder',lambda **kw: (_ for _ in ()).throw(RuntimeError('missing model')))
    assert service.search('退款','demo-a',mode='keyword')['hits']
    with pytest.raises(RuntimeError):service.search('退款','demo-a')


def test_knowledge_api_auth_input_and_unavailable_state(monkeypatch):
    with TestClient(create_app(token='test-search-token')) as client:
        path='/v1/knowledge/search';headers={'Authorization':'Bearer test-search-token'}
        assert client.post(path,json={'query':'退款'}).status_code==401
        for body in [{'query':'退款','tenant_id':'demo-b'},{'query':' '},{'query':'退款','top_k':11},{'query':'退款','mode':'magic'}]:
            assert client.post(path,json=body,headers=headers).status_code==422
        response=client.post(path,json={'query':'退款','mode':'keyword'},headers=headers)
        assert response.status_code==200 and response.json()['simulated']
        assert all(c['tenant_id']=='demo-a' for c in response.json()['hits'])
    monkeypatch.setattr(r,'INDEX',Path('does-not-exist'))
    monkeypatch.setattr(r,'KnowledgeSearch',lambda: (_ for _ in ()).throw(FileNotFoundError()))
    with TestClient(create_app(token='test-search-token')) as client:
        assert client.post(path,json={'query':'退款'},headers=headers).status_code==503

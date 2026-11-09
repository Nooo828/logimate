import json
from io import BytesIO
from urllib.error import HTTPError
import pytest
pytest.importorskip('jieba')
from logimate.milvus_backend import MilvusANN,MilvusREST
from logimate.retrieval import KnowledgeSearch,eligible


class FakeREST:
    def __init__(self):self.calls=[];self.response=[]
    def call(self,endpoint,body):
        self.calls.append((endpoint,body))
        return {'has':False} if endpoint=='collections/has' else self.response


def fixture():
    index=KnowledgeSearch().index;client=FakeREST();backend=MilvusANN(index,client)
    candidates=[c for c in index['chunks'] if eligible(c,'demo-a',index['as_of'],'warehouse')]
    client.response=[{'id':c['chunk_id'],'distance':.9-i*.1,'tenant_id':c['tenant_id'],'domain':c['domain'],
        'published':True,'live_from':int(c['valid_from'].replace('-','')),'live_to':99991231,'index_sha':backend.digest} for i,c in enumerate(candidates)]
    return backend,client,candidates


def test_filters_version_and_hnsw_are_sent_to_server():
    b,c,rows=fixture();b.search([1.0]*512,rows,'demo-a','2026-10-28','warehouse')
    body=c.calls[-1][1]
    for part in ['tenant_id == "demo-a"','published == true','live_from <= 20261028','live_to > 20261028','domain == "warehouse"',b.digest]:assert part in body['filter']
    assert body['searchParams']['params']['ef']==64 and body['consistencyLevel']=='Strong'
    b.provision()
    creates=[body for endpoint,body in c.calls if endpoint=='collections/create']
    assert len(creates)==2 and all(x['indexParams'][0]['params']['index_type']=='HNSW' for x in creates)


@pytest.mark.parametrize('change',['tenant','version','missing','duplicate','unknown','nan'])
def test_untrusted_or_incomplete_ann_response_is_rejected(change):
    b,c,rows=fixture()
    if change=='tenant':c.response[0]['tenant_id']='demo-b'
    if change=='version':c.response[0]['index_sha']='old'
    if change=='missing':c.response.pop()
    if change=='duplicate':c.response[-1]=dict(c.response[0])
    if change=='unknown':c.response[0]['id']='foreign-chunk'
    if change=='nan':c.response[0]['distance']=float('nan')
    with pytest.raises(RuntimeError):b.search([1.0]*512,rows,'demo-a','2026-10-28','warehouse')


def test_identity_expression_injection_rejected_before_io():
    b,c,rows=fixture()
    with pytest.raises(ValueError):b.search([1]*512,rows,'demo-a" or true','2026-10-28')
    assert c.calls==[]


def test_remote_error_body_never_read(monkeypatch):
    class Private(BytesIO):
        def read(self,*a):pytest.fail('remote error body read')
    def fail(*a,**kw):raise HTTPError('https://private.invalid',503,'secret',{},Private())
    monkeypatch.setattr('logimate.milvus_backend.urlopen',fail)
    with pytest.raises(RuntimeError) as e:MilvusREST(token='private').call('entities/search',{})
    assert 'private' not in str(e.value)


def test_backend_failure_never_falls_back_to_local_vectors():
    class Encoder:
        def query_embed(self,*a):return [[1.0]+[0.0]*511]
    class FailedANN:
        name='explicit-test-double'
        def search(self,*a):raise RuntimeError('explicit injected outage')
    service=KnowledgeSearch(encoder=Encoder(),dense_backend=FailedANN())
    with pytest.raises(RuntimeError):service.search('退款','demo-a')
    assert service.search('退款','demo-a',mode='keyword')['hits']

"""Real HTTP retrieval smoke; requests real local CPU embeddings, no LLM."""
import argparse
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def check(base, token):
    rows=[]
    for mode in ['keyword','dense','hybrid']:
        req=Request(base+'/v1/knowledge/search',data=json.dumps({'query':'退货入库48小时未退款','mode':mode}).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with urlopen(req,timeout=30) as response: body=json.load(response)
        assert body['simulated'] and any(c['section_id']=='refund' for c in body['hits'])
        assert all(c['tenant_id']=='demo-a' and c['published'] and c['valid_from']<=body['as_of'] and (c['valid_to'] is None or body['as_of']<c['valid_to']) for c in body['hits'])
        rows.append({'mode':mode,'ids':[c['chunk_id'] for c in body['hits']],'elapsed_ms':body['elapsed_ms']})
    for payload,auth,expected in [({'query':'退款'},False,401),({'query':'退款','tenant_id':'demo-b'},True,422)]:
        req=Request(base+'/v1/knowledge/search',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+token} if auth else {})})
        try:response=urlopen(req,timeout=5)
        except HTTPError as e:response=e
        with response: assert response.code==expected
    return {'passed':True,'real_cpu_embeddings':True,'real_llm_calls':False,'checks':rows,'auth_guards':2}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',default='http://127.0.0.1:8000');p.add_argument('--token',default='local-demo-token');a=p.parse_args()
    print(json.dumps(check(a.base,a.token)))

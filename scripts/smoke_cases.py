"""Actual HTTP synthetic persistent cases; demo tokens, no model calls or business writes."""
import argparse
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError

READ_TOKEN = 'case-reader-verification-only'
REVIEW_TOKEN = 'case-review-verification-only'


def call(base, path, body=None, *, key=None, review=False, token=None, expected=200):
    headers = {'Authorization':'Bearer '+(token or (REVIEW_TOKEN if review else READ_TOKEN)), 'Content-Type':'application/json'}
    if key is not None: headers['Idempotency-Key'] = key
    request = Request(base+path, data=None if body is None else json.dumps(body).encode(), headers=headers)
    try: response = urlopen(request, timeout=10)
    except HTTPError as exc: response = exc
    with response: status, result = response.code, json.load(response)
    row = {'http_status':status,'body':result}
    assert status == expected and result.get('simulated') is True, row
    return row


def check(base, mode, case_id=None):
    rows = []
    def req(*args, **kw):
        row = call(base, *args, **kw); rows.append(row); return row['body']
    if mode == 'create':
        response = req('/v1/cases', {'order_id':'SIM-006'}, key='docker-create-0001', expected=201)
        case_id = response['case']['case_id']
        assert response['case']['state']=='waiting_human' and response['case']['version']==1
        assert 'state_conflict' in response['case']['reasons']
        repeat = req('/v1/cases', {'order_id':'SIM-006'}, key='docker-create-0001')
        assert repeat['replayed'] and repeat['case']==response['case']
        req('/v1/cases', {'order_id':'SIM-001'}, key='docker-create-0001', expected=409)
        req('/v1/cases', {'order_id':'SIM-006','tenant_id':'demo-b'}, key='docker-guard-0001', expected=422)
        req('/v1/cases', {'order_id':'SIM-006'}, token='wrong', key='docker-guard-0002', expected=401)
        req('/v1/cases', {'order_id':'SIM-006'}, expected=422)
        req('/v1/cases/'+case_id+'/decisions', {'expected_version':1,'action':'confirm_review'}, key='docker-guard-0003', expected=401)
        req('/v1/cases/00000000-0000-0000-0000-000000000001', expected=404)
    else:
        path = '/v1/cases/'+case_id
        before = req(path)
        if mode == 'paused':
            assert before['case']['state']=='waiting_human' and before['case']['version']==1 and len(before['events'])==1
        elif mode == 'decide':
            first = req(path+'/decisions', {'expected_version':1,'action':'request_evidence','note':'Synthetic manual evidence request'},
                key='docker-evidence-0001', review=True)
            assert first['case']['state']=='waiting_human' and first['case']['version']==2
            req(path+'/decisions', {'expected_version':1,'action':'confirm_review'}, key='docker-stale-0001', review=True, expected=409)
            last = req(path+'/decisions', {'expected_version':2,'action':'confirm_review','note':'Synthetic review complete'}, key='docker-decision-0001', review=True)
            assert last['case']['state']=='reviewed' and last['case']['version']==3 and len(last['events'])==3
            assert last['case']['snapshot']==before['case']['snapshot'] and last['case']['diagnosis']==before['case']['diagnosis']
            assert [e['review']['action'] for e in last['events'][1:]]==['request_evidence','confirm_review']
        elif mode == 'reviewed':
            assert before['case']['state']=='reviewed' and before['case']['version']==3 and len(before['events'])==3
            replay = req(path+'/decisions', {'expected_version':2,'action':'confirm_review','note':'Synthetic review complete'}, key='docker-decision-0001', review=True)
            assert replay['replayed'] and replay['case']==before['case'] and len(replay['events'])==3
            original = req('/v1/cases', {'order_id':'SIM-006'}, key='docker-create-0001')
            assert original['replayed'] and original['case']['version']==1 and original['case']['case_id']==case_id
            assert req(path)['case']['version']==3
    return {'status':'passed','mode':mode,'case_id':case_id,'real_llm_calls':False,'checks':rows}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',default='http://127.0.0.1:18085')
    parser.add_argument('--mode',choices=['create','paused','decide','reviewed'],required=True)
    parser.add_argument('--case-id')
    args=parser.parse_args()
    print(json.dumps(check(args.base,args.mode,args.case_id),ensure_ascii=False))

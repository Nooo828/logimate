"""P7.3 actual API analysis/native review lifecycle. Fixture/live mode is server controlled."""
import argparse
import json
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from scripts.smoke_cases import READ_TOKEN, REVIEW_TOKEN

QUERY='请从四个业务域整体排查SIM-006退货状态冲突。'
PATH='/v1/graph/mcp/review/queries'


def call(base,path,body=None,*,key=None,review=False,token=None,expected=200):
    headers={'Authorization':'Bearer '+(token or (REVIEW_TOKEN if review else READ_TOKEN)),'Content-Type':'application/json'}
    if key is not None:headers['Idempotency-Key']=key
    request=Request(base+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
    try:response=urlopen(request,timeout=90)
    except HTTPError as exc:response=exc
    with response:row={'http_status':response.code,'body':json.load(response)}
    # Preserve the safe API envelope even if a later acceptance assertion fails.
    print(json.dumps({'path':path,'response':row},ensure_ascii=False),file=sys.stderr,flush=True)
    assert row['http_status']==expected and row['body'].get('simulated') is True,row
    return row


def check(base,mode,analysis_id=None,*,live=False):
    rows=[]
    def req(*args,**kwargs):
        row=call(base,*args,**kwargs);rows.append(row);return row['body']
    if mode=='create':
        req(PATH,{'query':QUERY,'tenant_id':'demo-b'},key='analysis-guard-0001',expected=422)
        req(PATH,{'query':QUERY,'planner_mode':'fixture'},key='analysis-guard-0002',expected=422)
        req(PATH,{'query':QUERY},token='wrong',key='analysis-guard-0003',expected=401)
        req(PATH,{'query':QUERY},expected=422)
        result=req(PATH,{'query':QUERY},key='analysis-create-0001')
        analysis_id=result['analysis_id']
        assert result['planner_mode']==('live' if live else 'fixture') and result['analysis_status']=='answered'
        assert result['thread']['status']=='waiting_human' and result['thread']['capture_count']==1
        case=result['thread']['case'];source=case['analysis_receipt']['result']
        assert source['implementation']=='langgraph-five-agent-mcp-shared-policy-v2'
        assert source['policy_context']['status']=='context_only' and source['policy_degraded'] is False
        assert len(source['provider_calls'])==5 and set(source['records'])=={'order','logistics','warehouse','aftersales'}
        assert 'state_conflict' in case['reasons']
        assert {v['code'] for v in case['diagnosis']['findings']}=={'RETURN_CONFLICT'}
        if live:
            assert result['model_responses_this_request']==5 and result['recorded_usage']['total_tokens']>0
            assert all(c.get('response_id') for c in source['provider_calls'])
        else:assert result['model_responses_this_request']==0 and result['recorded_usage']['total_tokens']==0
        tid=result['thread']['thread_id'];case_id=case['case_id']
        req('/v1/review-threads/'+tid+'/resume',{'expected_version':1,'action':'confirm_review'},key='analysis-role-0001',expected=401)
        req('/v1/cases/'+case_id+'/decisions',{'expected_version':1,'action':'confirm_review'},key='analysis-bypass-0001',review=True,expected=409)
    else:
        result=req('/v1/analysis-reviews/'+analysis_id)
        assert result['model_responses_this_request']==0
        replay=req(PATH,{'query':QUERY},key='analysis-create-0001')
        assert replay['replayed'] and replay['analysis_id']==analysis_id and replay['model_attempts_this_request']==0
        assert replay['receipt_sha256']==result['receipt_sha256']
        req(PATH,{'query':'different'},key='analysis-create-0001',expected=409)
        tid=result['thread']['thread_id'];path='/v1/review-threads/'+tid
        if mode=='resume':
            original=result['thread']['case'];assert result['thread']['status']=='waiting_human'
            first=req(path+'/resume',{'expected_version':1,'action':'request_evidence','note':'Check original synthetic record'},key='analysis-review-0001',review=True)
            assert first['status']=='waiting_human' and first['version']==2
            final=req(path+'/resume',{'expected_version':2,'action':'confirm_review','note':'Synthetic review complete'},key='analysis-review-0002',review=True)
            assert final['status']=='reviewed' and final['version']==3
            assert final['case']['analysis_receipt']==original['analysis_receipt']
            assert final['case']['snapshot']==original['snapshot'] and final['case']['diagnosis']==original['diagnosis']
            result=req('/v1/analysis-reviews/'+analysis_id)
        elif mode=='reviewed':
            assert result['thread']['status']=='reviewed' and result['thread']['version']==3
            repeat=req(path+'/resume',{'expected_version':2,'action':'confirm_review','note':'Synthetic review complete'},key='analysis-review-0002',review=True)
            assert repeat['replayed'] and repeat['version']==3
        assert result['thread']['status']=='reviewed'
        assert result['thread']['case']['analysis_receipt']['sha256']==result['receipt_sha256']
    return {'status':'passed','mode':mode,'analysis_id':analysis_id,'view':result,'checks':rows,
        'real_llm_calls':live and mode=='create','new_provider_responses':5 if live and mode=='create' else 0,
        'new_provider_tokens':result['recorded_usage']['total_tokens'] if live and mode=='create' else 0}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base',default='http://127.0.0.1:18087')
    p.add_argument('--mode',choices=['create','resume','reviewed'],required=True);p.add_argument('--analysis-id')
    p.add_argument('--live',action='store_true');a=p.parse_args()
    print(json.dumps(check(a.base,a.mode,a.analysis_id,live=a.live),ensure_ascii=False))

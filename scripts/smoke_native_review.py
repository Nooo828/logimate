"""Native thread HTTP protocol; reads frozen synthetic case, never calls a model."""
import argparse
import json
from scripts.smoke_cases import call


def check(base,mode,thread_id=None):
    rows=[]
    def req(*args,**kwargs):
        row=call(base,*args,**kwargs);rows.append(row);return row['body']
    if mode=='create':
        case=req('/v1/cases',{'order_id':'SIM-006'},key='native-case-0001',expected=201)['case']
        result=req('/v1/review-threads',{'case_id':case['case_id']},key='native-start-0001')
        thread_id=result['thread_id'];assert result['status']=='waiting_human' and result['version']==1
        repeat=req('/v1/review-threads',{'case_id':case['case_id']},key='native-start-0001')
        assert repeat['replayed'] and repeat['thread_id']==thread_id
        req('/v1/review-threads',{'case_id':case['case_id'],'checkpoint_id':'forbidden'},key='native-invalid-0001',expected=422)
        req('/v1/review-threads/'+thread_id+'/resume',{'expected_version':1,'action':'confirm_review'},key='native-role-0001',expected=401)
        req('/v1/cases/'+case['case_id']+'/decisions',{'expected_version':1,'action':'confirm_review'},key='native-bypass-0001',review=True,expected=409)
    else:
        path='/v1/review-threads/'+thread_id;result=req(path)
        if mode=='paused':assert result['status']=='waiting_human' and result['version']==1
        elif mode=='decide':
            original=result['case']
            result=req(path+'/resume',{'expected_version':1,'action':'request_evidence','note':'Need synthetic source'},key='native-resume-0001',review=True)
            assert result['status']=='waiting_human' and result['version']==2
            replay=req(path+'/resume',{'expected_version':1,'action':'request_evidence','note':'Need synthetic source'},key='native-resume-0001',review=True)
            assert replay['replayed'] and len(replay['decisions'])==1
            req(path+'/resume',{'expected_version':1,'action':'confirm_review'},key='native-stale-0001',review=True,expected=409)
            result=req(path+'/resume',{'expected_version':2,'action':'confirm_review','note':'Synthetic review complete'},key='native-resume-0002',review=True)
            assert result['status']=='reviewed' and result['version']==3 and len(result['decisions'])==2
            assert result['case']['snapshot']==original['snapshot'] and result['case']['diagnosis']==original['diagnosis']
        elif mode=='reviewed':
            assert result['status']=='reviewed' and result['version']==3 and len(result['decisions'])==2
            replay=req(path+'/resume',{'expected_version':2,'action':'confirm_review','note':'Synthetic review complete'},key='native-resume-0002',review=True)
            assert replay['replayed'] and len(replay['decisions'])==2
            created=req('/v1/review-threads',{'case_id':result['case']['case_id']},key='native-start-0001')
            assert created['replayed'] and created['version']==1
            result=req(path);assert result['version']==3
    assert result['capture_count']==1 and result['checkpoint_id']
    return {'status':'passed','mode':mode,'thread_id':thread_id,'view':result,'checks':rows,
        'native_checkpoint':True,'real_llm_calls':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base',default='http://127.0.0.1:18086')
    p.add_argument('--mode',choices=['create','paused','decide','reviewed'],required=True);p.add_argument('--thread-id')
    a=p.parse_args();print(json.dumps(check(a.base,a.mode,a.thread_id),ensure_ascii=False))

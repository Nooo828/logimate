"""P8.9b no-key synthetic HTTP + native review demonstration. Reviewer is scripted."""
import argparse,asyncio,json,os,secrets,socket,sys,time
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request,urlopen,ProxyHandler,build_opener
from urllib.error import HTTPError

ROOT=Path(os.environ.get('LOGIMATE_PROJECT_ROOT',str(Path(__file__).resolve().parents[1])))
sys.path.insert(0,str(ROOT))
from logimate.api import create_app
from logimate.cases import CaseStore
from logimate.native_review import NativeReview
import p88_load as integrity


def check_diagnosis(body,expected):
    codes={f['code'] for f in body['findings']}
    if codes!=set(expected):raise AssertionError('diagnosis differs from frozen contract')
    ids={e['evidence_id'] for e in body['evidence']}
    if any(not set(f['evidence_ids'])<=ids for f in body['findings']):raise AssertionError('unsupported finding')


async def demonstrate(out):
    import uvicorn
    reader=secrets.token_urlsafe(32);reviewer=secrets.token_urlsafe(32)
    result={'phase':'P8.9b-no-key-demonstration','status':'running','simulated':True,'scripted_reviewer':True,
        'real_model_calls':0,'real_mcp':False,'real_cpu_embeddings':False,'real_native_langgraph':True,
        'transport':'actual loopback HTTP','checks':[],'api_instances':0,'application_freeze':integrity.freeze_check(),
        'runner_sha256':integrity.sha(Path(__file__))}
    source_hash=integrity.sha(ROOT/'data/synthetic_v1.json');thread=None;captured=None
    opener=build_opener(ProxyHandler({}))
    def request(base,name,path,body=None,*,expected=200,key=None,role='reader'):
        headers={'Content-Type':'application/json'}
        if role!='none':headers['Authorization']='Bearer '+(reviewer if role=='reviewer' else reader)
        if key:headers['Idempotency-Key']=key
        req=Request(base+path,headers=headers,data=None if body is None else json.dumps(body).encode())
        try:r=opener.open(req,timeout=10)
        except HTTPError as e:r=e
        with r:status,payload=r.status,json.load(r)
        result['checks'].append({'name':name,'path':path,'request':body,'http_status':status,'expected_status':expected,'body':payload})
        integrity.write(out/'progress.json',result)
        if status!=expected:raise AssertionError(name+': unexpected HTTP status')
        print(name+': HTTP '+str(status),flush=True)
        return payload
    try:
        for phase in ('capture','resume','verify'):
            with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
            base=f'http://127.0.0.1:{port}';store=CaseStore(out/'cases.sqlite3')
            app=create_app(token=reader,review_token=reviewer,case_store=store,native_review_service=NativeReview(store))
            server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning',access_log=False))
            task=asyncio.create_task(server.serve());result['api_instances']+=1
            async def call(name,path,body=None,**kw):return await asyncio.to_thread(request,base,name,path,body,**kw)
            try:
                deadline=time.monotonic()+20
                while not server.started:
                    if task.done():await task;raise RuntimeError('API failed to start')
                    if time.monotonic()>deadline:raise TimeoutError('API startup')
                    await asyncio.sleep(.02)
                if phase=='capture':
                    normal=await call('正常订单','/v1/diagnoses',{'order_id':'SIM-001'})
                    check_diagnosis(normal,[]);assert normal['status']=='clear'
                    refund=await call('超时退款与财务线索','/v1/diagnoses',{'order_id':'SIM-002','intent':'refund_delay'})
                    check_diagnosis(refund,['REFUND_OVERDUE','FINANCE_FAILURE_RECORDED']);assert refund['needs_human']
                    missing=await call('缺证据不能推断未退款','/v1/diagnoses',{'order_id':'SIM-008','intent':'refund_delay'})
                    assert missing['needs_human'] and 'FINANCE_FAILURE_RECORDED' not in {f['code'] for f in missing['findings']}
                    await call('未授权访问拦截','/v1/orders/SIM-002',role='none',expected=401)
                    foreign=await call('跨租户订单不可见','/v1/orders/SIM-009',expected=404)
                    absent=await call('不存在订单同样不可见','/v1/orders/SIM-999',expected=404)
                    assert foreign==absent
                    await call('请求体不能改租户','/v1/diagnoses',{'order_id':'SIM-002','tenant_id':'demo-b'},expected=422)
                    await call('缺少密钥时模型入口关闭','/v1/agent/queries',{'query':'查询SIM-002'},expected=503)
                    case=(await call('冲突单进入持久工单','/v1/cases',{'order_id':'SIM-006'},key='demo-case-0001',expected=201))['case']
                    assert case['state']=='waiting_human'
                    native=await call('原生LangGraph暂停','/v1/review-threads',{'case_id':case['case_id']},key='demo-thread-0001')
                    thread=native['thread_id'];captured=native['case']
                    assert native['status']=='waiting_human' and native['capture_count']==1
                    await call('读者不能代审核','/v1/review-threads/'+thread+'/resume',{'expected_version':1,'action':'confirm_review'},key='demo-unauthorized-0001',expected=401)
                    await call('工单接口不能绕过原生线程','/v1/cases/'+case['case_id']+'/decisions',{'expected_version':1,'action':'confirm_review'},key='demo-bypass-0001',role='reviewer',expected=409)
                elif phase=='resume':
                    view=await call('重建API后仍等待人工','/v1/review-threads/'+thread)
                    assert view['status']=='waiting_human' and view['capture_count']==1
                    pending=await call('模拟审核员要求补证','/v1/review-threads/'+thread+'/resume',{'expected_version':1,'action':'request_evidence','note':'演示代演：仅请求核实，未添加业务事实'},key='demo-review-0001',role='reviewer')
                    assert pending['status']=='waiting_human' and pending['version']==2
                    await call('旧版本审核命令被拒','/v1/review-threads/'+thread+'/resume',{'expected_version':1,'action':'confirm_review'},key='demo-stale-0001',role='reviewer',expected=409)
                    done=await call('模拟审核员确认审核记录','/v1/review-threads/'+thread+'/resume',{'expected_version':2,'action':'confirm_review','note':'演示代演：审核记录完成，不代表业务异常已解决'},key='demo-review-0002',role='reviewer')
                    assert done['status']=='reviewed' and done['version']==3 and len(done['decisions'])==2
                    assert done['case']['snapshot']==captured['snapshot'] and done['case']['diagnosis']==captured['diagnosis']
                else:
                    done=await call('再次重建API保留审核结果','/v1/review-threads/'+thread)
                    assert done['status']=='reviewed' and done['version']==3 and done['capture_count']==1
                    replay=await call('重复审核命令不重复执行','/v1/review-threads/'+thread+'/resume',{'expected_version':2,'action':'confirm_review','note':'演示代演：审核记录完成，不代表业务异常已解决'},key='demo-review-0002',role='reviewer')
                    assert replay['replayed'] and len(replay['decisions'])==2
                    result['thread_id']=thread
            finally:
                server.should_exit=True;await task
                result.setdefault('api_shutdowns',[]).append(phase)
        assert integrity.sha(ROOT/'data/synthetic_v1.json')==source_hash
        assert integrity.freeze_check()==result['application_freeze']
        result.update(status='passed',source_unchanged=True,application_unchanged=True)
    except Exception as e:result.update(status='failed',error_type=type(e).__name__);raise
    finally:
        integrity.write(out/'summary.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path);a=p.parse_args()
    out=a.output or ROOT/'docs/evidence/p8'/datetime.now(timezone.utc).strftime('demo-local-%Y%m%dT%H%M%S%f')
    out.mkdir(parents=True,exist_ok=False)
    settings={'DEEPSEEK_API_KEY':'','LOGIMATE_RESILIENT_RETRIEVAL':'0','LOGIMATE_CASE_DB':'','LOGIMATE_NATIVE_REVIEW':'0','LOGIMATE_ANALYSIS_REVIEW':'0','LANGSMITH_TRACING':'false','LANGCHAIN_TRACING_V2':'false'}
    try:
        with patch.dict(os.environ,settings):r=asyncio.run(demonstrate(out))
        rows=['# LogiMate 无需密钥模拟演示','', '**全部为自建模拟数据，审核员由脚本代演。** 本次真实运行HTTP、LangGraph中断恢复及SQLite，不调用模型、MCP、嵌入或生产服务。','', '| 演示步骤 | HTTP |','|---|---:|']
        rows += [f'| {c["name"]} | {c["http_status"]} |' for c in r['checks']]
        rows += ['','三个API实例在同一演示进程内依次创建/关闭，共用独立SQLite文件；不是操作系统进程或容器重启。审核未修改原诊断和业务快照，未执行退款/库存操作。','', '原rules-v1响应保留阶段一的历史limitations字符串；后续持久审核是另一个接口/模块，本演示不改写原响应。真实模型及MCP能力见已冻结历史验收，本演示不代替其成绩。']
        (out/'report.md').write_text('\n'.join(rows)+'\n',encoding='utf-8')
        print('Status: '+r['status'],flush=True);return 0
    finally:print('Evidence: '+str(out),flush=True)


if __name__=='__main__':raise SystemExit(main())

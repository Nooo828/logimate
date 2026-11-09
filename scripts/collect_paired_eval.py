"""P8.2 same-process, same-input three-profile collection. No automatic resume/retry."""
import argparse
import asyncio
from datetime import datetime,timezone
import getpass
import hashlib,json,os
from pathlib import Path
import re,socket,time
import platform
from importlib.metadata import version
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import uvicorn
from logimate.api import create_app
from logimate.deepseek import DeepSeek
from logimate.end_to_end_eval import score
from logimate.evaluation_run import RunLedger,collect_cell,execution_plan
from logimate.five_agent import run_five_agent
from logimate.retrieval import ROOT
from logimate.single_agent import run_agent


class CalibrationPlanner:
    """Explicit no-model planner; heuristics from query/schema, not gold lookup."""
    def complete(self,messages,tools=None,**kwargs):
        if tools is None:
            query=messages[-1]['content'];oid=re.search(r'SIM-[0-9]{3}',query).group()
            domains=['order'] if '查询' in query else ['order','warehouse'] if 'SKU' in query else ['order','logistics','warehouse','aftersales']
            message={'content':json.dumps({'action':'read','order_ids':[oid],'domains':domains})}
        else:
            body=next(json.loads(m['content']) for m in messages if m['role']=='user')
            oid=body.get('order_id') or body['route']['order_ids'][0]
            names=['read_'+d for d in body['required_domains']] if 'required_domains' in body else [tools[0]['function']['name']]
            message={'tool_calls':[{'id':str(i),'type':'function','function':{'name':n,'arguments':json.dumps({'order_id':oid})}} for i,n in enumerate(names)]}
        return message,{'model':'explicit-p82-calibration-planner-no-LLM','usage':{'total_tokens':0}}


def http(base,path,body=None):
    req=Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'Authorization':'Bearer p82-owned-collector','Content-Type':'application/json'})
    try:r=urlopen(req,timeout=90)
    except HTTPError as exc:r=exc
    with r:return r.code,json.load(r)


def input_hashes():
    return {q.relative_to(ROOT).as_posix():hashlib.sha256(q.read_bytes()).hexdigest()
        for pattern in ('logimate/*.py','scripts/collect_paired_eval.py','eval/e2e_v1.json','data/**/*.json','prompts/*.txt','requirements*.lock') for q in ROOT.glob(pattern)}


def export(out,ledger,dataset,planner,status):
    rows=ledger.rows();accounting=ledger.accounting()
    result={'status':status,'planner_mode':planner,'accounting':accounting,'unfinished_cells':ledger.unfinished(),
        'stop_reason':ledger.stop_reason,'real_llm_calls':planner=='live' and accounting['attempts_started']>0,
        'known_provider_responses':accounting['responses_recorded'] if planner=='live' else 0,
        'known_tokens':accounting['known_tokens'],'billing_is_complete':accounting['billing_unknown_attempts']==0,
        'temperature':0,'max_provider_attempts':ledger.max_attempts,'token_stop_after_observed_usage':ledger.max_tokens,
        'no_automatic_retry_or_resume':True,'dataset_exposure':'previously_used_calibration_not_holdout',
        'latency_scope':'application execution; model + tools + shared MCP startup included; CPU warmup excluded'}
    result['shared_policy_degraded_cells']=sum(row['mode']=='shared' and row['response'].get('policy_degraded') is True for row in rows)
    if status=='collected' and not result['billing_is_complete']:result['status']='collected_billing_incomplete'
    (out/'responses.json').write_text(ledger.safe_text(rows),encoding='utf-8')
    try:
        scores=score(dataset,rows,['single','five','shared'],planner,provenance='new_run')
        (out/'scores.json').write_text(ledger.safe_text(scores),encoding='utf-8');result['scores_complete']=scores['complete']
        result['strict_matches']={m:s['counts']['end_to_end_exact'] for m,s in scores['summary'].items()}
    except Exception as exc:
        result.update(status='scoring_rejected',scores_complete=False,score_error_type=type(exc).__name__)
        (out/'scores.json').write_text(ledger.safe_text({'status':'scoring_rejected','complete':False,'error_type':type(exc).__name__}),encoding='utf-8')
    result['input_sha256']=input_hashes()
    initial=json.loads((out/'run.json').read_text(encoding='utf-8')) if (out/'run.json').exists() else {}
    result['input_sha256_initial']=initial.get('input_sha256')
    result['input_unchanged']=result['input_sha256_initial']==result['input_sha256']
    if status=='collected' and not result['input_unchanged']:result['status']='input_drift'
    result['output_sha256']={n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in ('responses.json','scores.json','ledger.sqlite3') if (out/n).exists()}
    (out/'summary.json').write_text(ledger.safe_text(result),encoding='utf-8')
    return result


async def collect(out,dataset,ledger,planner,provider_factory):
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}';active={'provider':None}
    def shared_provider():
        if active['provider'] is None:raise RuntimeError('no authorized collection cell')
        return active['provider']
    settings={'LOGIMATE_RESILIENT_RETRIEVAL':'1','LOGIMATE_DENSE_BACKEND':'exact','LOGIMATE_LEXICAL_CACHE':str(out/'lexical-cache'),
        'LOGIMATE_CASE_DB':'','LOGIMATE_NATIVE_REVIEW':'0','LOGIMATE_ANALYSIS_REVIEW':'0'}
    with patch.dict(os.environ,settings),patch('logimate.deepseek.DeepSeek',shared_provider):
        app=create_app(token='p82-owned-collector',shared_url=base+'/internal/p6/search')
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
        task=asyncio.create_task(server.serve());status='interrupted'
        try:
            deadline=time.monotonic()+45
            while not server.started:
                if task.done():await task;raise RuntimeError('server startup failed')
                if time.monotonic()>deadline:raise TimeoutError('API startup')
                await asyncio.sleep(.02)
            while True:
                _,health=await asyncio.to_thread(http,base,'/v1/knowledge/resilient/health')
                if health.get('primary',{}).get('ready') and health.get('keyword',{}).get('ready'):break
                if time.monotonic()>deadline:raise TimeoutError('CPU warmup')
                await asyncio.sleep(.1)
            (out/'warmup.json').write_text(ledger.safe_text(health),encoding='utf-8')
            plan=execution_plan(dataset['cases']);(out/'plan.json').write_text(ledger.safe_text([{'case_id':c['case_id'],'mode':m} for c,m in plan]),encoding='utf-8')
            for case,mode in plan:
                if ledger.stop_reason:status='stopped';break
                provider=ledger.provider(provider_factory(),case['case_id'],mode,planner)
                def execute():
                    if mode=='single':return run_agent(provider,case['query'],'demo-a')
                    if mode=='five':return run_five_agent(provider,case['query'],'demo-a')
                    active['provider']=provider
                    try:return http(base,'/v1/graph/mcp/resilient/queries',{'query':case['query']})[1]
                    finally:active['provider']=None
                result=await asyncio.to_thread(collect_cell,ledger,case,mode,planner,execute)
                export(out,ledger,dataset,planner,'running')
                print(f"{case['case_id']} {mode}: {result['status']}",flush=True)
            else:status='collected'
        finally:server.should_exit=True;await task
    return status


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--live',action='store_true');p.add_argument('--output',type=Path)
    p.add_argument('--max-attempts',type=int,default=90,choices=range(1,201));p.add_argument('--max-tokens',type=int,default=60000,choices=range(1,200001));a=p.parse_args()
    out=a.output or ROOT/'docs/evidence/p8'/datetime.now(timezone.utc).strftime('paired-%Y%m%dT%H%M%S%f')
    out=out.resolve();out.mkdir(parents=True,exist_ok=False)  # Refuse rerun/resume into an old external-attempt ledger.
    key=(os.environ.get('DEEPSEEK_API_KEY') or getpass.getpass('DeepSeek API key (hidden, 18 planned calibration executions): ')).strip() if a.live else ''
    if a.live and not key:raise ValueError('empty key; no external attempts')
    planner='live' if a.live else 'fixture';model=os.environ.get('LOGIMATE_MODEL','deepseek-flash')
    dataset=json.loads((ROOT/'eval/e2e_v1.json').read_text(encoding='utf-8'))
    ledger=RunLedger(out/'ledger.sqlite3',max_attempts=a.max_attempts,max_tokens=a.max_tokens,secret=key);status='interrupted'
    (out/'run.json').write_text(ledger.safe_text({'input_sha256':input_hashes(),'planner_mode':planner,
        'model':model if a.live else 'explicit-p82-calibration-planner-no-LLM','python':platform.python_version(),'platform':platform.platform(),
        'versions':{n:version(n) for n in ('langgraph','mcp','fastapi','fastembed','onnxruntime')},
        'started_at_utc':datetime.now(timezone.utc).isoformat(),'dense_backend':'exact','max_attempts':a.max_attempts,'max_tokens_observed_stop':a.max_tokens}),encoding='utf-8')
    try:status=asyncio.run(collect(out,dataset,ledger,planner,lambda:DeepSeek(api_key=key,model=model) if a.live else CalibrationPlanner()))
    except Exception as exc:print('Collection error: '+type(exc).__name__,flush=True);status='failed'
    finally:
        report=export(out,ledger,dataset,planner,status)
        print('Status: '+report['status']);print('Evidence: '+str(out),flush=True)
    return 0 if report['status']=='collected' and report.get('scores_complete') else 1


if __name__=='__main__':raise SystemExit(main())

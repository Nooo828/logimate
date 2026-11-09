"""P8.5 dev-only real three-profile collection. No holdout option or automatic retries."""
import argparse,asyncio,getpass,hashlib,json,os,platform,re,secrets,socket,time
from datetime import datetime,timezone
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import uvicorn
from logimate.api import create_app
from logimate.boundary_eval import score
from logimate.deepseek import DeepSeek
from logimate.dev_collection import select_dev,InputGuard
from logimate.evaluation_run import RunLedger,collect_cell,execution_plan
from logimate.five_agent import run_five_agent
from logimate.single_agent import run_agent
from logimate.retrieval import ROOT
from scripts.collect_paired_eval import CalibrationPlanner


class DevPlanner(CalibrationPlanner):
    """Explicit fixture heuristics, no case IDs, family labels, holdout or gold lookup."""
    def complete(self,messages,tools=None,**kwargs):
        if tools is not None:return super().complete(messages,tools=tools,**kwargs)
        q=messages[-1]['content'];ids=list(dict.fromkeys(re.findall(r'(?<![A-Za-z0-9-])SIM-[0-9]{3}(?![A-Za-z0-9-])',q)));domains=[]
        if any(s in q for s in ('执行','改成','写入','管理员','系统授权','发起一笔退款')):action='refuse'
        elif '转人工' in q or '交给人工' in q:action='human'
        elif len(ids)!=1:action='clarify'
        elif '四域' in q or all(s in q for s in ('订单','物流','仓储','售后')):action='read';domains=['order','logistics','warehouse','aftersales']
        elif any(s in q for s in ('库存','实盘','账面')):action='read';domains=['warehouse']
        elif '订单状态' in q or '履约节点' in q:action='read';domains=['order']
        else:action='clarify'
        return {'content':json.dumps({'action':action,'order_ids':ids,'domains':domains})},{'model':'explicit-p85-dev-planner-no-LLM','usage':{'total_tokens':0}}


def input_hashes():
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
        for pattern in ('logimate/*.py','scripts/collect_boundary_dev.py','scripts/collect_paired_eval.py','eval/boundary_v2*.json','data/**/*.json','prompts/*.txt','requirements*.lock') for p in ROOT.glob(pattern)}


def export(out,ledger,data,planner,status,selected):
    rows=ledger.rows();allowed={c.case_id for c in selected};accounting=ledger.accounting()
    with ledger.db() as db:attempt_ids=[r['case_id'] for r in db.execute('SELECT case_id FROM attempts')]
    outside=sum(cid not in allowed for cid in attempt_ids)
    held={c['case_id'] for c in data['cases'] if c['split']=='holdout'}
    held_attempts=sum(cid in held for cid in attempt_ids)
    dev_only=not outside and all(r['case_id'] in allowed for r in rows) and all(r['case_id'] in allowed for r in ledger.unfinished())
    packet={'dataset_sha256':hashlib.sha256((ROOT/'eval/boundary_v2.json').read_bytes()).hexdigest(),'split':'dev','planner_mode':planner,'rows':rows}
    report={'status':status,'split':'dev','planned_cells':48,'planner_mode':planner,'dev_only_verified':dev_only,
        'non_dev_attempts_detected':outside,'holdout_target_model_calls':held_attempts if planner=='live' else 0,
        'unknown_case_attempts_detected':sum(cid not in allowed and cid not in held for cid in attempt_ids),
        'accounting':accounting,'known_provider_responses':accounting['responses_recorded'] if planner=='live' else 0,
        'known_tokens':accounting['known_tokens'],'billing_is_complete':accounting['billing_unknown_attempts']==0,
        'unfinished_cells':ledger.unfinished(),'stop_reason':ledger.stop_reason,'max_attempts':ledger.max_attempts,
        'token_stop_after_observed_usage':ledger.max_tokens,'dataset_exposure':'author_visible_pre_run_dev_known_synthetic_fixtures',
        'shared_policy_degraded_cells':sum(r['mode']=='shared' and r['response'].get('policy_degraded') is True for r in rows),
        'latency_scope':'application model/tool execution and MCP startup; shared CPU prewarm excluded'}
    (out/'predictions.json').write_text(ledger.safe_text(packet),encoding='utf-8')
    try:
        scores=score(data,packet,ROOT);(out/'scores.json').write_text(ledger.safe_text(scores),encoding='utf-8')
        report.update(scores_complete=scores['complete'],task_matches={m:v['counts']['task_exact'] for m,v in scores['summary'].items()},
            boundary_failures={m:v['counts']['boundary_failure'] for m,v in scores['summary'].items()},
            unverified_usage_rows=sum(v['counts']['unverified_usage_rows'] for v in scores['summary'].values()))
    except Exception as exc:
        report.update(status='scoring_rejected',scores_complete=False,score_error_type=type(exc).__name__)
        (out/'scores.json').write_text(ledger.safe_text({'status':'scoring_rejected','complete':False}),encoding='utf-8')
    if status=='collected' and not report['billing_is_complete']:report['status']='collected_billing_incomplete'
    if not dev_only:report['status']='unexpected_split_rejected'
    report['input_sha256']=input_hashes()
    initial=json.loads((out/'run.json').read_text(encoding='utf-8')) if (out/'run.json').exists() else {}
    report['input_unchanged']=initial.get('input_sha256')==report['input_sha256']
    report['input_sha256_initial']=initial.get('input_sha256')
    if status=='collected' and not report['input_unchanged']:report['status']='input_drift'
    report['output_sha256']={n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in ('predictions.json','scores.json','ledger.sqlite3','run.json','selected_inputs.json','plan.json') if (out/n).exists()}
    (out/'summary.json').write_text(ledger.safe_text(report),encoding='utf-8');return report


def http(base,token,path,body=None):
    req=Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:r=urlopen(req,timeout=90)
    except HTTPError as exc:r=exc
    with r:return r.code,json.load(r)


async def collect(out,data,selected,ledger,planner,factory):
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}';token=secrets.token_urlsafe(32);active={'provider':None}
    def shared_provider():
        if active['provider'] is None:raise RuntimeError('No selected development cell')
        return active['provider']
    settings={'LOGIMATE_RESILIENT_RETRIEVAL':'1','LOGIMATE_DENSE_BACKEND':'exact','LOGIMATE_LEXICAL_CACHE':str(out/'lexical-cache'),
        'LOGIMATE_CASE_DB':'','LOGIMATE_NATIVE_REVIEW':'0','LOGIMATE_ANALYSIS_REVIEW':'0'}
    with patch.dict(os.environ,settings),patch('logimate.deepseek.DeepSeek',shared_provider):
        app=create_app(token=token,shared_url=base+'/internal/p6/search');server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
        task=asyncio.create_task(server.serve());status='interrupted'
        try:
            deadline=time.monotonic()+45
            while not server.started:
                if task.done():await task;raise RuntimeError('API startup failed')
                if time.monotonic()>deadline:raise TimeoutError('API startup')
                await asyncio.sleep(.02)
            while True:
                _,h=await asyncio.to_thread(http,base,token,'/v1/knowledge/resilient/health')
                if h.get('primary',{}).get('ready') and h.get('keyword',{}).get('ready'):break
                if time.monotonic()>deadline:raise TimeoutError('CPU warmup')
                await asyncio.sleep(.1)
            (out/'warmup.json').write_text(ledger.safe_text(h),encoding='utf-8')
            cases=[{'case_id':c.case_id,'query':c.query} for c in selected];plan=execution_plan(cases)
            (out/'plan.json').write_text(ledger.safe_text([{'case_id':c['case_id'],'mode':m} for c,m in plan]),encoding='utf-8')
            for case,mode in plan:
                if ledger.stop_reason:status='stopped';break
                provider=InputGuard(ledger.provider(factory(),case['case_id'],mode,planner),case['query'])
                def execute():
                    if mode=='single':return run_agent(provider,case['query'],'demo-a')
                    if mode=='five':return run_five_agent(provider,case['query'],'demo-a')
                    active['provider']=provider
                    try:return http(base,token,'/v1/graph/mcp/resilient/queries',{'query':case['query']})[1]
                    finally:active['provider']=None
                r=await asyncio.to_thread(collect_cell,ledger,case,mode,planner,execute)
                export(out,ledger,data,planner,'running',selected)
                print(f"{case['case_id']} {mode}: {r['status']}",flush=True)
            else:status='collected'
        finally:server.should_exit=True;await task
    return status


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--split',choices=['dev'],default='dev');p.add_argument('--live',action='store_true');p.add_argument('--output',type=Path)
    p.add_argument('--max-attempts',type=int,default=120,choices=range(1,201));p.add_argument('--max-tokens',type=int,default=60000,choices=range(1,200001));a=p.parse_args()
    data,selected=select_dev(ROOT,split=a.split)  # Freeze and split checks occur before key input.
    out=(a.output or ROOT/'docs/evidence/p8'/datetime.now(timezone.utc).strftime('boundary-dev-%Y%m%dT%H%M%S%f')).resolve();out.mkdir(parents=True,exist_ok=False)
    key=(os.environ.get('DEEPSEEK_API_KEY') or getpass.getpass('DeepSeek API key (hidden, dev only, 48 executions): ')).strip() if a.live else ''
    if a.live and not key:raise ValueError('empty key; no external attempts')
    planner='live' if a.live else 'fixture';model=os.environ.get('LOGIMATE_MODEL','deepseek-flash')
    ledger=RunLedger(out/'ledger.sqlite3',max_attempts=a.max_attempts,max_tokens=a.max_tokens,secret=key);status='interrupted'
    (out/'selected_inputs.json').write_text(ledger.safe_text([{'case_id':c.case_id,'query':c.query} for c in selected]),encoding='utf-8')
    (out/'run.json').write_text(ledger.safe_text({'split':'dev','holdout_execution_enabled':False,'input_sha256':input_hashes(),
        'planner_mode':planner,'model':model if a.live else 'explicit-p85-dev-planner-no-LLM','python':platform.python_version(),'platform':platform.platform(),
        'versions':{n:version(n) for n in ('langgraph','mcp','fastapi','fastembed','onnxruntime')},'started_at_utc':datetime.now(timezone.utc).isoformat()}),encoding='utf-8')
    try:status=asyncio.run(collect(out,data,selected,ledger,planner,lambda:DeepSeek(api_key=key,model=model) if a.live else DevPlanner()))
    except Exception as exc:status='failed';print('Collection error: '+type(exc).__name__,flush=True)
    finally:
        report=export(out,ledger,data,planner,status,selected);print('Status: '+report['status']);print('Evidence: '+str(out),flush=True)
    return 0 if report['status']=='collected' and report.get('scores_complete') and not report.get('unverified_usage_rows') else 1


if __name__=='__main__':raise SystemExit(main())

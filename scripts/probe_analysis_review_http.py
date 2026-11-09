"""Actual MCP/shared CPU + native review, explicitly fixture planner; no paid model."""
import argparse
import asyncio
import hashlib
import json
import os
import socket
import tempfile
import time
from pathlib import Path
from datetime import datetime,timezone
from unittest.mock import patch
import uvicorn

from logimate.api import create_app
from logimate.cases import CaseStore
from logimate.native_review import NativeReview
from logimate.retrieval import ROOT
from scripts.smoke_cases import READ_TOKEN,REVIEW_TOKEN,call
from scripts.smoke_analysis_review import check
from scripts.verify_mcp_graph import FixturePlanner


async def probe(out):
    results=[];analysis_id=None
    for index,mode in enumerate(('create','resume','reviewed')):
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}';store=CaseStore(out/'cases.sqlite3')
        settings={'LOGIMATE_ANALYSIS_REVIEW':'1','LOGIMATE_RESILIENT_RETRIEVAL':'1' if index==0 else '0',
            'LOGIMATE_DENSE_BACKEND':'exact','LOGIMATE_LEXICAL_CACHE':str(out/'lexical-cache')}
        factory=FixturePlanner if index==0 else lambda:(_ for _ in ()).throw(AssertionError('cached receipt must not create a model'))
        with patch.dict(os.environ,settings),patch('logimate.deepseek.DeepSeek',factory):
            app=create_app(token=READ_TOKEN,review_token=REVIEW_TOKEN,case_store=store,native_review_service=NativeReview(store),
                shared_url=base+'/internal/p6/search',analysis_planner_mode='fixture')
            server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
            task=asyncio.create_task(server.serve())
            try:
                deadline=time.monotonic()+30
                while not server.started:
                    if task.done():await task;raise RuntimeError('server startup failed')
                    if time.monotonic()>deadline:raise TimeoutError('API startup')
                    await asyncio.sleep(.01)
                if index==0:
                    while True:
                        h=(await asyncio.to_thread(call,base,'/v1/knowledge/resilient/health'))['body']
                        if h.get('primary',{}).get('ready') and h.get('keyword',{}).get('ready'):break
                        if time.monotonic()>deadline:raise TimeoutError('CPU worker warmup')
                        await asyncio.sleep(.05)
                result=await asyncio.to_thread(check,base,mode,analysis_id)
                analysis_id=result['analysis_id'];results.append(result)
                (out/'progress.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
            finally:server.should_exit=True;await task
    return {'status':'passed','api_instances':3,'real_tcp_http':True,'real_mcp_stdio':True,'real_cpu_embeddings':True,
        'planner_test_double':True,'real_llm_calls':False,'replay_with_model_and_retrieval_disabled':True,'results':results}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stdout',action='store_true');a=p.parse_args()
    out=Path(tempfile.mkdtemp(prefix='logimate-p73-')) if a.stdout else ROOT/'docs/evidence/p7'/datetime.now(timezone.utc).strftime('analysis-http-%Y%m%dT%H%M%S%f')
    out.mkdir(parents=True,exist_ok=True);result={'status':'failed','real_llm_calls':False}
    try:result=asyncio.run(probe(out))
    finally:
        result['input_sha256']={q.relative_to(ROOT).as_posix():hashlib.sha256(q.read_bytes()).hexdigest()
            for pattern in ('logimate/*.py','scripts/*analysis*review*.py','scripts/smoke_cases.py','scripts/verify_mcp_graph.py','data/**/*.json') for q in ROOT.glob(pattern)}
        if a.stdout:print(json.dumps(result,ensure_ascii=False))
        else:
            (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print('Status: '+result['status']);print('Evidence: '+str(out))
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())

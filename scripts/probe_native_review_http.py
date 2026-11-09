"""Three actual API instances share official SQLite checkpoint files and control inbox."""
import asyncio
import hashlib
import json
import socket
from datetime import datetime,timezone
import uvicorn
from logimate.api import create_app
from logimate.cases import CaseStore
from logimate.native_review import NativeReview
from logimate.retrieval import ROOT
from scripts.smoke_cases import READ_TOKEN,REVIEW_TOKEN
from scripts.smoke_native_review import check


async def probe(out):
    results=[];thread_id=None;initial_checkpoint=None
    for modes in [('create',),('paused','decide'),('reviewed',)]:
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        store=CaseStore(out/'cases.sqlite3');workflow=NativeReview(store)
        app=create_app(token=READ_TOKEN,review_token=REVIEW_TOKEN,case_store=store,native_review_service=workflow)
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
        task=asyncio.create_task(server.serve())
        try:
            while not server.started:
                if task.done():await task;raise RuntimeError('API startup failed')
                await asyncio.sleep(.01)
            for mode in modes:
                result=await asyncio.to_thread(check,f'http://127.0.0.1:{port}',mode,thread_id)
                thread_id=result['thread_id'];results.append(result)
                if mode=='create':initial_checkpoint=result['view']['checkpoint_id']
                if mode=='paused':assert result['view']['checkpoint_id']==initial_checkpoint
                (out/'progress.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        finally:server.should_exit=True;await task
    return {'status':'passed','api_instances':3,'real_tcp_http':True,'official_sqlite_saver':True,
        'same_pending_checkpoint_after_new_api_instance':True,'real_llm_calls':False,'results':results}


if __name__=='__main__':
    out=ROOT/'docs/evidence/p7'/datetime.now(timezone.utc).strftime('native-http-%Y%m%dT%H%M%S%f');out.mkdir(parents=True)
    result={'status':'failed','real_llm_calls':False}
    try:result=asyncio.run(probe(out))
    finally:
        result['input_sha256']={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for pattern in ('logimate/*.py','scripts/*native*review*.py','scripts/smoke_cases.py','requirements-checkpoint.lock','data/synthetic_v1.json') for p in ROOT.glob(pattern)}
        (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Status: '+result['status']);print('Evidence: '+str(out))

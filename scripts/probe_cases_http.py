"""Real loopback TCP, disk DB and distinct API instances; no Docker/model claim."""
import asyncio
import hashlib
import json
import socket
from datetime import datetime, timezone
import uvicorn
from logimate.api import create_app
from logimate.cases import CaseStore
from logimate.retrieval import ROOT
from scripts.smoke_cases import check, READ_TOKEN, REVIEW_TOKEN


async def probe(out):
    results=[]; case_id=None
    for modes in [('create',),('paused','decide'),('reviewed',)]:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        app=create_app(token=READ_TOKEN,review_token=REVIEW_TOKEN,case_store=CaseStore(out/'cases.sqlite3'))
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
        task=asyncio.create_task(server.serve())
        try:
            while not server.started:
                if task.done(): await task;raise RuntimeError('API startup failed')
                await asyncio.sleep(.01)
            for mode in modes:
                response=await asyncio.to_thread(check,f'http://127.0.0.1:{port}',mode,case_id)
                results.append(response);case_id=response['case_id']
                (out/'progress.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        finally:
            server.should_exit=True;await task
    return {'status':'passed','real_tcp_http':True,'api_instances':3,'database':'actual SQLite file',
        'real_llm_calls':False,'results':results}


if __name__=='__main__':
    out=ROOT/'docs/evidence/p7'/datetime.now(timezone.utc).strftime('http-%Y%m%dT%H%M%S%f');out.mkdir(parents=True)
    result={'status':'failed','real_llm_calls':False}
    try: result=asyncio.run(probe(out))
    finally:
        result['input_sha256']={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for pattern in ('logimate/*.py','scripts/*cases*.py','data/synthetic_v1.json') for p in ROOT.glob(pattern)}
        (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Status: '+result['status']);print('Evidence: '+str(out))

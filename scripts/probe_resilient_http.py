"""Owned loopback Uvicorn + actual CPU roles; exact backend, no LLM or Milvus claim."""
import asyncio
import hashlib
import json
import os
import socket
import time
from datetime import datetime, timezone
from unittest.mock import patch

import uvicorn
from logimate.api import create_app
from logimate.retrieval import ROOT
from scripts.smoke_resilient import HEALTH, SEARCH, budget_check, request


async def probe(out):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    token = 'owned-local-p6-http-probe'
    with patch.dict(os.environ, {'LOGIMATE_RESILIENT_RETRIEVAL': '1', 'LOGIMATE_DENSE_BACKEND': 'exact',
                                'LOGIMATE_LEXICAL_CACHE': str(out / 'lexical-cache')}):
        app = create_app(token=token)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
        task = asyncio.create_task(server.serve())
        started = time.perf_counter()
        base = f'http://127.0.0.1:{port}'
        try:
            while not server.started:
                if task.done(): await task; raise RuntimeError('Uvicorn startup failed')
                await asyncio.sleep(.01)
            while True:
                h = await asyncio.to_thread(request, base, token, HEALTH)
                if h['body'].get('primary', {}).get('ready') and h['body'].get('keyword', {}).get('ready'): break
                if time.perf_counter() - started > 25: raise TimeoutError('CPU HTTP readiness')
                await asyncio.sleep(.02)
            warmup = (time.perf_counter() - started) * 1000
            rows = []
            for query in ('退货入库48小时未退款', 'SKU-AX7库存', '签收争议'):
                row = await asyncio.to_thread(request, base, token, SEARCH, {'query': query})
                budget_check(row)
                assert row['status_code'] == 200 and not row['body']['degraded']
                assert row['body']['backend'] == 'in-memory-exact'
                rows.append(row)
            invalid = await asyncio.to_thread(request, base, token, SEARCH, {'query': '退款', 'tenant_id': 'demo-b'})
            assert invalid['status_code'] == 422
            h = await asyncio.to_thread(request, base, token, HEALTH)
            assert h['body']['metrics']['requests'] == 3 and h['body']['active_requests'] == 0
            return {'status': 'passed', 'real_tcp_http': True, 'real_cpu_embeddings': True,
                    'real_milvus': False, 'real_llm_calls': False, 'backend': 'exact',
                    'startup_ms_excluded_from_retrieval_budget': warmup, 'checks': rows,
                    'invalid_tenant_status': invalid['status_code'], 'health': h['body']}
        finally:
            server.should_exit = True
            await task  # Lifespan closes/reaps both owned workers before leaving.


if __name__ == '__main__':
    out = ROOT / 'docs/evidence/p6' / datetime.now(timezone.utc).strftime('http-local-%Y%m%dT%H%M%S%f')
    out.mkdir(parents=True, exist_ok=False)
    try: result = asyncio.run(probe(out))
    except Exception as exc:
        result = {'status': 'failed', 'error_type': type(exc).__name__}; raise
    finally:
        result['input_sha256'] = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for pattern in ('logimate/*.py', 'scripts/probe_resilient_http.py', 'scripts/smoke_resilient.py', 'data/knowledge/*.json')
            for p in ROOT.glob(pattern)}
        (out / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Evidence: ' + str(out))
    print('Status: ' + result['status'])

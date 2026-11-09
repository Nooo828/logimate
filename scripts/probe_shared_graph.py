"""Real TCP + SDK stdio + CPU. Planner is an explicit no-LLM double; no key needed."""
import argparse
import asyncio
import hashlib
import json
import os
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import uvicorn
from logimate.api import create_app
from logimate.retrieval import ROOT
from scripts.smoke_resilient import request, HEALTH
from scripts.verify_mcp_graph import FixturePlanner, canonical

GRAPH = '/v1/graph/mcp/resilient/queries'


class ObservedPlanner(FixturePlanner):
    def complete(self, messages, tools=None, **kwargs):
        if tools:
            context = json.loads(messages[-1]['content']).get('policy_context')
            if context is not None:
                assert context['not_order_evidence'] and 'retrieval' in context
                assert context['retrieval']['served_mode'] == ('keyword' if context['retrieval']['degraded'] else 'hybrid')
        return super().complete(messages, tools=tools, **kwargs)


async def probe(out, scenario):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    token = 'owned-p62-probe-public-demo'
    with patch.dict(os.environ, {'LOGIMATE_RESILIENT_RETRIEVAL': '1', 'LOGIMATE_LEXICAL_CACHE': str(out / 'lexical-cache')}), \
         patch('logimate.deepseek.DeepSeek', ObservedPlanner):
        app = create_app(token=token, shared_url=base + '/internal/p6/search')
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
        task = asyncio.create_task(server.serve())
        rows = []
        async def call(path, body=None): return await asyncio.to_thread(request, base, token, path, body)
        async def ready():
            end = time.perf_counter()+25
            while time.perf_counter() < end:
                health = (await call(HEALTH))['body']
                if health['primary']['ready'] and health['keyword']['ready']: return health
                await asyncio.sleep(.05)
            raise TimeoutError('shared worker readiness')
        try:
            while not server.started:
                if task.done(): await task; raise RuntimeError('server startup failed')
                await asyncio.sleep(.01)
            await ready()
            invalid = await call('/internal/p6/search', {'query': '退款'})
            assert invalid['status_code'] == 401
            count = 6 if scenario == 'outage' else 2
            for i in range(count):
                await ready()  # Warm-up outside graph/request measurements, recorded separately in P6.1.
                query = '仅查询SIM-001的订单状态。' if scenario == 'outage' else '四域排查SIM-008退货入库48小时未退款'
                legacy = await call('/v1/graph/mcp/queries', {'query': query})
                row = await call(GRAPH, {'query': query}); rows.append(row)
                (out/'progress.json').write_text(json.dumps({'rows': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
                if row['status_code'] != 200: raise AssertionError('shared graph failed: ' + json.dumps(row, ensure_ascii=False))
                body = row['body']
                assert canonical(body) == canonical(legacy['body'])
                assert body['policy_degraded'] == (scenario == 'outage')
                assert all(e['domain'] != 'policy' for e in (body.get('diagnosis') or {}).get('evidence', []))
                assert len(body['policy_consumption']) == (1 if scenario == 'outage' else 4)
                for result in body['policy_context']['payload']['results'].values():
                    r = result['retrieval']
                    assert r['retrieval_budget_ms'] == 800 and r['retrieval_elapsed_ms'] <= 800 and not r['deadline_overrun']
            health = (await call(HEALTH))['body']
            assert health['metrics']['requests'] == (6 if scenario == 'outage' else 8)
            assert health['active_requests'] == 0
            if scenario == 'outage':
                assert health['circuit']['state'] == 'open'
                last = rows[-1]['body']['policy_context']['payload']['results']['order']['retrieval']
                assert last['degradation_reason'] == 'circuit_open' and not any(a['stage']=='primary' for a in last['attempts'])
            return {'status': 'passed', 'real_tcp_http': True, 'real_mcp_stdio': True,
                'real_cpu_embeddings': True, 'real_llm_calls': False, 'planner_test_double': True,
                'scenario': scenario, 'backend': os.environ.get('LOGIMATE_DENSE_BACKEND', 'exact'),
                'new_mcp_session_per_graph': True, 'rows': rows, 'shared_health': health,
                'internal_public_token_rejected': invalid['status_code']}
        finally:
            server.should_exit = True
            await task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=['normal', 'outage'], default='normal')
    parser.add_argument('--stdout', action='store_true')
    parser.add_argument('--closed-port-fault', action='store_true', help='Explicit local closed-port Milvus fault, no real DB')
    args = parser.parse_args()
    out = Path(tempfile.mkdtemp(prefix='logimate-p62-')) if args.stdout else ROOT / 'docs/evidence/p6' / datetime.now(timezone.utc).strftime('shared-local-%Y%m%dT%H%M%S%f')
    out.mkdir(parents=True, exist_ok=True)
    result = {'status': 'failed', 'real_llm_calls': False}
    try:
        if args.closed_port_fault:
            if args.scenario != 'outage': raise ValueError('closed port requires outage scenario')
            with socket.socket() as closed:
                closed.bind(('127.0.0.1', 0)); fault_port = closed.getsockname()[1]
                # Bound without listening: exclusively owned refusal target, never a real DB.
                with patch.dict(os.environ, {'LOGIMATE_DENSE_BACKEND': 'milvus', 'LOGIMATE_MILVUS_URI': f'http://127.0.0.1:{fault_port}'}):
                    result = asyncio.run(probe(out, args.scenario))
            result.update(real_milvus=False, closed_port_fault=True)
        else: result = asyncio.run(probe(out, args.scenario))
    except Exception as exc:
        result['error_type'] = type(exc).__name__
        raise
    finally:
        result['input_sha256'] = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for pattern in ('logimate/*.py', 'scripts/probe_shared_graph.py', 'scripts/verify_mcp_graph.py',
                'scripts/smoke_resilient.py', 'prompts/*.txt', 'data/synthetic_v1.json', 'data/knowledge/*.json') for p in ROOT.glob(pattern)}
        if args.stdout: print(json.dumps(result, ensure_ascii=False))
        else:
            (out/'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print('Status: '+result['status']); print('Evidence: '+str(out))
    return 0 if result['status']=='passed' else 1


if __name__ == '__main__': raise SystemExit(main())

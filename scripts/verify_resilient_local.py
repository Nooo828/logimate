"""Real CPU workers plus labelled closed-port and stalled-process faults, no LLM."""
import asyncio
import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
from pathlib import Path
import time
from datetime import datetime, timezone
from unittest.mock import patch

from logimate.retrieval import ROOT
from logimate.resilient_retrieval import ResilientRetrieval, WorkerSlot


async def wait_ready(service):
    started = time.perf_counter()
    await service.start()
    while not service.primary.ready or not service.keyword.ready:
        if service._fatal(): raise RuntimeError('worker startup integrity error')
        if time.perf_counter() - started > 25: raise TimeoutError('CPU worker readiness exceeded startup deadline')
        await asyncio.sleep(.02)
    return round((time.perf_counter() - started) * 1000, 3)


def fault_slot(role):
    return WorkerSlot(role, command=[sys.executable, '-m', 'scripts.retrieval_fault_worker', '--role', role])


async def scenarios(out, report):
    rows = report['scenarios'] = {}
    def checkpoint():
        (out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    # Separate lexical caches avoid mutating the user's existing tokenizer cache.
    lexical = str(out / 'lexical-cache')
    with patch.dict(os.environ, {'LOGIMATE_DENSE_BACKEND': 'exact', 'LOGIMATE_LEXICAL_CACHE': lexical}):
        service = ResilientRetrieval()
    try:
        cold = await service.search({'query': '退款'}, 'demo-a')
        assert cold.status_code == 503
        startup = await wait_ready(service)
        results = []
        for query, domain in [('退货入库48小时未退款', None), ('SKU-AX7库存', 'warehouse'), ('签收争议', 'logistics')]:
            result = await service.search({'query': query, 'domain': domain}, 'demo-a')
            assert result.status_code == 200 and not result.body['degraded'] and not result.body['deadline_overrun']
            assert result.body['retrieval_elapsed_ms'] <= 800
            results.append(result.body)
        rows['normal_exact_cpu'] = {'startup_ms_excluded_from_request_budget': startup, 'cold_status': cold.status_code,
                                    'requests': results, 'health': service.snapshot()}
        checkpoint()
    finally: await service.close()

    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    with patch.dict(os.environ, {'LOGIMATE_DENSE_BACKEND': 'milvus', 'LOGIMATE_MILVUS_URI': f'http://127.0.0.1:{port}',
                                'LOGIMATE_LEXICAL_CACHE': lexical}):
        service = ResilientRetrieval()
    try:
        startup = await wait_ready(service)
        results = []
        rows['closed_port_fault'] = {'real_milvus_server': False, 'fault': 'temporarily closed local port',
                                    'startup_ms_excluded': startup, 'requests': results}
        for _ in range(6):
            # Worker cancellation/rewarm is not a new dependency attempt. Poll readiness outside the request budget.
            began = time.monotonic()
            while not service.primary.ready:
                if time.monotonic() - began > 25: raise TimeoutError('primary did not rewarm')
                await asyncio.sleep(.02)
            response = await service.search({'query': '退货未退款'}, 'demo-a')
            results.append(response.body)
            rows['closed_port_fault']['health'] = service.snapshot(); checkpoint()
            assert response.status_code == 200 and response.body['degraded']
            assert response.body['served_mode'] == 'keyword' and response.body['retrieval_elapsed_ms'] <= 800
        assert results[-1]['degradation_reason'] == 'circuit_open'
        assert service.metrics['primary_failure'] + service.metrics['primary_timeout'] == 5
    finally: await service.close()

    for both in (False, True):
        with patch.dict(os.environ, {'LOGIMATE_DENSE_BACKEND': 'exact', 'LOGIMATE_LEXICAL_CACHE': lexical}):
            service = ResilientRetrieval(primary=fault_slot('primary'), keyword=fault_slot('keyword') if both else None)
        try:
            await wait_ready(service)
            old_process = service.primary.process
            response = await service.search({'query': '退货未退款'}, 'demo-a')
            assert old_process.returncode is not None and service.primary.cancelled_jobs == 1
            assert response.body['retrieval_elapsed_ms'] <= 800 and not response.body['deadline_overrun']
            if both: assert response.status_code == 503 and 'hits' not in response.body
            else: assert response.status_code == 200 and response.body['degraded'] and response.body['served_mode'] == 'keyword'
            rows['dual_stall' if both else 'primary_stall'] = {'fault_process_test_double': True,
                'real_keyword_cpu': not both, 'status_code': response.status_code,
                'old_process_reaped': old_process.returncode is not None, 'response': response.body}
            checkpoint()
        finally: await service.close()
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stdout', action='store_true', help='Container probe: temporary evidence directory, JSON stdout')
    args = parser.parse_args()
    out = Path(tempfile.mkdtemp(prefix='logimate-p6-probe-')) if args.stdout else ROOT / 'docs/evidence/p6' / datetime.now(timezone.utc).strftime('local-%Y%m%dT%H%M%S%f')
    if not args.stdout: out.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'real_llm_calls': False, 'real_cpu_embeddings': True,
              'faults_are_explicit_test_doubles': True,
              'input_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for pattern in ['logimate/*.py', 'scripts/verify_resilient_local.py', 'scripts/retrieval_fault_worker.py',
                                'data/knowledge/*.json'] for p in ROOT.glob(pattern)}}
    try:
        asyncio.run(scenarios(out, report)); report['status'] = 'passed'
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__)
        raise
    finally:
        (out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if not args.stdout: print('Evidence: ' + str(out), flush=True)
    if args.stdout: print(json.dumps(report, ensure_ascii=False))
    else: print('Status: ' + report['status'], flush=True)


if __name__ == '__main__': main()

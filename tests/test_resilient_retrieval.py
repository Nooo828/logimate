"""Explicit fault doubles, real owned-process cancellation, and API lifecycle contracts."""
import asyncio
import copy
import json
import sys

import pytest
pytest.importorskip('jieba')
from fastapi.testclient import TestClient
from logimate.api import create_app
from logimate.circuit import CircuitBreaker
from logimate.retrieval import KnowledgeSearch, MODEL, eligible
from logimate.resilient_retrieval import (Admission, Budget, DependencyFailure, IntegrityFailure,
    ResilientRetrieval, WorkerSlot, worker_environment)


@pytest.fixture(autouse=True)
def exact_backend(monkeypatch): monkeypatch.setenv('LOGIMATE_DENSE_BACKEND', 'exact')


def fake_body(role, tenant, args):
    index = KnowledgeSearch(dense_backend=False).index
    rows = [c for c in index['chunks'] if eligible(c, tenant, index['as_of'], args.domain)][:args.top_k]
    return {'simulated': True, 'corpus_id': index['corpus_id'], 'corpus_sha256': index['corpus_sha256'],
            'as_of': index['as_of'], 'rrf_k': 60, 'backend': 'in-memory-exact',
            'mode': 'hybrid' if role == 'primary' else 'keyword', 'embedding_model': MODEL if role == 'primary' else None,
            'hits': [{**c, 'score': .5, 'rank': i + 1} for i, c in enumerate(rows)], 'elapsed_ms': 0,
            'limitations': ['Explicit fake result for controller tests only.']}


class FakeSlot:
    def __init__(self, role, *, error=None, delay=0, change=None, ready=True):
        self.role, self.error, self.delay, self.change = role, error, delay, change
        self.ready, self.busy, self.closed, self.error_kind = ready, False, False, None
        self.calls, self.cancelled_jobs = 0, 0
        self.began = None
    def schedule_warm(self): pass
    async def request(self, tenant, args):
        self.calls += 1; self.busy = True
        if self.began: self.began.set()
        try:
            await asyncio.sleep(self.delay)
            if self.error: raise self.error('explicit injected failure')
            body = fake_body(self.role, tenant, args)
            if self.change: self.change(body)
            return body
        except asyncio.CancelledError:
            self.cancelled_jobs += 1; raise
        finally: self.busy = False
    async def close(self): self.closed = True
    def snapshot(self): return {'ready': self.ready, 'busy': self.busy, 'cancelled_jobs': self.cancelled_jobs}


def test_budget_and_primary_success_have_no_keyword_work():
    assert Budget().total == .8
    with pytest.raises(ValueError): Budget(total=.1)
    async def scenario():
        primary, keyword = FakeSlot('primary'), FakeSlot('keyword')
        service = ResilientRetrieval(primary=primary, keyword=keyword)
        response = await service.search({'query': '退款'}, 'demo-a')
        assert response.status_code == 200 and response.body['served_mode'] == 'hybrid'
        assert not response.body['degraded'] and keyword.calls == 0
        assert service.admission.active == 0 and service.breaker.snapshot()['failures'] == 0
    asyncio.run(scenario())


def test_default_800ms_timeout_cancels_before_explicit_keyword_fallback():
    async def scenario():
        primary, keyword = FakeSlot('primary', delay=2), FakeSlot('keyword')
        service = ResilientRetrieval(primary=primary, keyword=keyword)
        response = await service.search({'query': '退款'}, 'demo-a')
        assert response.status_code == 200 and response.body['degraded']
        assert response.body['served_mode'] == 'keyword' and response.body['mode'] == 'keyword'
        assert response.body['degradation_reason'] == 'primary_timeout'
        assert primary.cancelled_jobs == 1 and not primary.busy and keyword.calls == 1
        assert response.body['retrieval_elapsed_ms'] <= 800 and not response.body['deadline_overrun']
        assert service.breaker.snapshot()['failures'] == 1
    asyncio.run(scenario())


def test_both_unavailable_return_no_successful_evidence():
    async def scenario():
        service = ResilientRetrieval(primary=FakeSlot('primary', error=DependencyFailure),
                                    keyword=FakeSlot('keyword', error=DependencyFailure))
        response = await service.search({'query': '退款'}, 'demo-a')
        assert response.status_code == 503 and 'hits' not in response.body
        assert response.body['degradation_reason'] == 'both_paths_unavailable'
    asyncio.run(scenario())


def test_circuit_is_shared_across_requests_and_skips_primary_while_open():
    async def scenario():
        primary = FakeSlot('primary', error=DependencyFailure)
        service = ResilientRetrieval(primary=primary, keyword=FakeSlot('keyword'))
        for _ in range(5): assert (await service.search({'query': '退款'}, 'demo-a')).status_code == 200
        response = await service.search({'query': '退款'}, 'demo-b')
        assert primary.calls == 5 and response.body['circuit']['state'] == 'open'
        assert response.body['degradation_reason'] == 'circuit_open'
        assert all(h['tenant_id'] == 'demo-b' for h in response.body['hits'])
    asyncio.run(scenario())


def test_sliding_window_half_open_single_probe_and_epoch_fencing():
    now = [0.0]
    breaker = CircuitBreaker(clock=lambda: now[0])
    stale = breaker.acquire()
    for _ in range(5): breaker.record(breaker.acquire(), success=False)
    assert breaker.snapshot()['state'] == 'open' and breaker.acquire() is None
    now[0] = 10
    probe = breaker.acquire()
    assert probe.probe and breaker.acquire() is None
    breaker.record(stale, success=True)
    assert breaker.snapshot()['state'] == 'half_open'
    breaker.record(probe, success=True)
    assert breaker.snapshot()['state'] == 'closed' and breaker.snapshot()['samples'] == 0
    breaker.record(probe, success=False)
    assert breaker.snapshot()['state'] == 'closed'
    breaker.record(breaker.acquire(), success=False)
    now[0] += 31
    assert breaker.snapshot()['samples'] == 0


def test_failed_probe_reopens_and_cancelled_probe_can_be_replaced():
    now = [0.0]; b = CircuitBreaker(minimum=1, clock=lambda: now[0])
    b.record(b.acquire(), success=False); now[0] = 10
    probe = b.acquire(); b.record(probe, success=False)
    assert b.acquire() is None
    now[0] = 20; probe = b.acquire(); b.abandon(probe)
    assert b.acquire().probe


@pytest.mark.parametrize('change', ['tenant', 'stale', 'nan', 'extra', 'published-type', 'worker-contract'])
def test_integrity_errors_are_sticky_and_never_fallback(change):
    def mutate(body):
        if change == 'tenant': body['hits'][0]['tenant_id'] = 'demo-b'
        if change == 'stale': body['corpus_sha256'] = 'old'
        if change == 'nan': body['hits'][0]['score'] = float('nan')
        if change == 'extra': body['order_paid'] = True
        if change == 'published-type': body['hits'][0]['published'] = 1
    async def scenario():
        primary = FakeSlot('primary', change=mutate, error=IntegrityFailure if change == 'worker-contract' else None)
        keyword = FakeSlot('keyword'); service = ResilientRetrieval(primary=primary, keyword=keyword)
        first = await service.search({'query': '退款'}, 'demo-a')
        second = await service.search({'query': '退款'}, 'demo-a')
        assert first.status_code == second.status_code == 503
        assert first.body['degradation_reason'] == 'integrity_failure'
        assert keyword.calls == 0 and primary.calls == 1 and service.breaker.snapshot()['samples'] == 0
    asyncio.run(scenario())


def test_admission_is_bounded_per_process_and_per_tenant_without_queue():
    now = [0.0]; admission = Admission(active_limit=2, per_tenant=2, global_rate=3, clock=lambda: now[0])
    assert admission.enter('demo-a') is None and admission.enter('demo-b') is None
    assert admission.enter('demo-a') == 'capacity'
    admission.leave(); admission.leave()
    assert admission.enter('demo-a') is None; admission.leave()
    assert admission.enter('demo-a') == 'rate'
    now[0] = 61
    assert admission.enter('demo-a') is None; admission.leave()
    assert admission.enter('untrusted') == 'forbidden'


def test_caller_cancellation_does_not_fallback_or_count_dependency_failure():
    async def scenario():
        primary = FakeSlot('primary', delay=30); primary.began = asyncio.Event()
        keyword = FakeSlot('keyword'); service = ResilientRetrieval(primary=primary, keyword=keyword)
        task = asyncio.create_task(service.search({'query': '退款'}, 'demo-a'))
        await primary.began.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert primary.cancelled_jobs == 1 and keyword.calls == 0
        assert service.breaker.snapshot()['samples'] == 0 and service.admission.active == 0
        assert not service.tasks
    asyncio.run(scenario())


def test_cold_requests_never_initialize_or_wait_for_model():
    async def scenario():
        primary, keyword = FakeSlot('primary', ready=False), FakeSlot('keyword', ready=False)
        service = ResilientRetrieval(primary=primary, keyword=keyword)
        response = await service.search({'query': '退款'}, 'demo-a')
        assert response.status_code == 503 and primary.calls == keyword.calls == 0
        keyword.ready = True
        response = await service.search({'query': '退款'}, 'demo-a')
        assert response.status_code == 200 and response.body['degradation_reason'] == 'primary_not_ready'
    asyncio.run(scenario())


def test_new_api_requires_enable_auth_and_closed_schema(monkeypatch):
    monkeypatch.delenv('LOGIMATE_RESILIENT_RETRIEVAL', raising=False)
    h = {'Authorization': 'Bearer reliability-test'}; url = '/v1/knowledge/resilient/search'
    with TestClient(create_app(token='reliability-test')) as client:
        assert client.post(url, json={'query': '退款'}).status_code == 401
        for extra in [{'tenant_id': 'demo-b'}, {'mode': 'keyword'}, {'budget': 99}, {'fault': 'timeout'}]:
            assert client.post(url, json={'query': '退款', **extra}, headers=h).status_code == 422
        assert client.post(url, json={'query': '退款'}, headers=h).status_code == 503
    service = ResilientRetrieval(primary=FakeSlot('primary'), keyword=FakeSlot('keyword'))
    with TestClient(create_app(token='reliability-test', resilient_service=service)) as client:
        response = client.post(url, json={'query': '退款'}, headers=h)
        assert response.status_code == 200 and not response.json()['degraded']
        assert client.get('/v1/knowledge/resilient/health', headers=h).json()['metrics']['requests'] == 1
    assert service.closed


def test_role_environment_excludes_model_secrets_and_keyword_db_credentials(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'explicit-test-model-key')
    monkeypatch.setenv('LOGIMATE_MILVUS_TOKEN', 'explicit-test-db-key')
    assert 'DEEPSEEK_API_KEY' not in worker_environment('primary')
    assert worker_environment('primary')['LOGIMATE_MILVUS_TOKEN'] == 'explicit-test-db-key'
    assert 'LOGIMATE_MILVUS_TOKEN' not in worker_environment('keyword')


def test_actual_slow_process_is_reaped_before_keyword_response():
    code = "import json,sys,time; print(json.dumps({'type':'ready','role':'primary'}),flush=True); sys.stdin.readline(); time.sleep(30)"
    async def scenario():
        slot = WorkerSlot('primary', command=[sys.executable, '-u', '-c', code])
        service = ResilientRetrieval(primary=slot, keyword=FakeSlot('keyword'))
        await service.start()
        try:
            await asyncio.wait_for(slot.boot_task, timeout=5)
            process = slot.process
            response = await service.search({'query': '退款'}, 'demo-a')
            assert response.status_code == 200 and response.body['served_mode'] == 'keyword'
            assert process.returncode is not None and slot.cancelled_jobs == 1
            assert response.body['retrieval_elapsed_ms'] <= 800
        finally: await service.close()
        assert slot.process is None
    asyncio.run(scenario())


def test_os_cleanup_overrun_is_reported_and_never_clamped_to_success():
    class SlowCleanup(FakeSlot):
        async def request(self, tenant, args):
            try: await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(.1)
                raise
    async def scenario():
        service = ResilientRetrieval(primary=SlowCleanup('primary'), keyword=FakeSlot('keyword'),
            budget=Budget(total=.08, primary=.04, fallback=.02, reserve=.02))
        result = await service.search({'query': '退款'}, 'demo-a')
        assert result.status_code == 503 and result.body['deadline_overrun']
        assert result.body['retrieval_elapsed_ms'] > 80 and 'hits' not in result.body
    asyncio.run(scenario())


def test_capacity_degradation_does_not_count_as_dependency_failure():
    async def scenario():
        primary = FakeSlot('primary'); primary.busy = True
        service = ResilientRetrieval(primary=primary, keyword=FakeSlot('keyword'))
        result = await service.search({'query': '退款'}, 'demo-a')
        assert result.status_code == 200 and result.body['degradation_reason'] == 'primary_busy'
        assert primary.calls == 0 and service.breaker.snapshot()['samples'] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('status,kind', [(401, IntegrityFailure), (403, IntegrityFailure), (503, DependencyFailure)])
def test_milvus_http_classification_preserves_auth_vs_operational_boundary(monkeypatch, status, kind):
    from urllib.error import HTTPError
    from logimate.milvus_backend import MilvusREST, MilvusIntegrityError, MilvusUnavailable
    def failure(*args, **kwargs): raise HTTPError('http://local.invalid', status, 'private response', {}, None)
    monkeypatch.setattr('logimate.milvus_backend.urlopen', failure)
    with pytest.raises(MilvusIntegrityError if kind is IntegrityFailure else MilvusUnavailable):
        MilvusREST().call('entities/search', {})

"""Explicit controller doubles; separate CLI probe exercises real TCP/MCP/CPU."""
import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient
from logimate.api import create_app
from logimate.mcp_client import server_parameters
from logimate.policy_tools import PolicyArguments, checksum
from logimate.shared_policy import SharedPolicyGateway, validate_connection, validate_result
from logimate.resilient_retrieval import DependencyFailure, ResilientRetrieval
from logimate.tool_gateway import Principal
from test_resilient_retrieval import FakeSlot

PRINCIPAL = Principal('demo-a', frozenset(['order', 'warehouse', 'logistics', 'aftersales']))
CONNECTION = {'url': 'http://127.0.0.1:8000/internal/p6/search', 'token': 'test-only-capability-' + 'x'*32}


def context(monkeypatch, *, broken=False):
    monkeypatch.setenv('LOGIMATE_DENSE_BACKEND', 'exact')
    service = ResilientRetrieval(primary=FakeSlot('primary', error=DependencyFailure if broken else None), keyword=FakeSlot('keyword'))
    args = PolicyArguments(query='退款', domains=['order'])
    async def run():
        async def thread_fetch(function, connection, arguments):
            result = await service.search(arguments, 'demo-a')
            return result.body
        monkeypatch.setattr('logimate.shared_policy.asyncio.to_thread', thread_fetch)
        return await SharedPolicyGateway(PRINCIPAL, CONNECTION).execute(args.model_dump())
    return service, args, asyncio.run(run())


@pytest.mark.parametrize('url', ['http://example.com/internal/p6/search', 'http://127.0.0.1:8000/evil',
    'https://127.0.0.1:8000/internal/p6/search', 'http://secret@127.0.0.1:8000/internal/p6/search',
    'http://127.0.0.1:8000/internal/p6/search?tenant=demo-b'])
def test_callback_is_fixed_loopback_not_user_ssrf(url):
    with pytest.raises(ValueError): validate_connection({**CONNECTION, 'url': url})


def test_shared_mcp_never_receives_model_database_or_unprovisioned_callback_secrets(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'do-not-forward')
    monkeypatch.setenv('LOGIMATE_MILVUS_TOKEN', 'do-not-forward')
    monkeypatch.setenv('LOGIMATE_MCP_SHARED_TOKEN', 'untrusted-global-value')
    env = server_parameters(PRINCIPAL, knowledge=True, shared_retrieval=CONNECTION).env
    assert env['LOGIMATE_MCP_SHARED_TOKEN'] == CONNECTION['token']
    assert 'DEEPSEEK_API_KEY' not in env and 'LOGIMATE_MILVUS_TOKEN' not in env
    assert 'LOGIMATE_RETRIEVAL_CACHE' not in env
    assert 'LOGIMATE_MCP_SHARED_TOKEN' not in server_parameters(PRINCIPAL, knowledge=True).env


@pytest.mark.parametrize('broken', [False, True])
def test_shared_result_preserves_explicit_modes_and_source_validation(monkeypatch, broken):
    service, args, data = context(monkeypatch, broken=broken)
    assert data['ok'] and data['degraded'] is broken
    assert validate_result(data, args, PRINCIPAL) == data
    assert service.metrics['requests'] == 1


@pytest.mark.parametrize('field,value', [('degraded', False), ('retrieval_budget_ms', 900),
    ('deadline_overrun', True), ('served_mode', 'hybrid'), ('retrieval_elapsed_ms', float('nan'))])
def test_mcp_rejects_hidden_degradation_or_budget_tampering(monkeypatch, field, value):
    _, args, data = context(monkeypatch, broken=True)
    data['payload']['results']['order']['retrieval'][field] = value
    if field != 'retrieval_elapsed_ms': data['payload_sha256'] = checksum(data['payload'])
    with pytest.raises((ValueError, KeyError)): validate_result(data, args, PRINCIPAL)


def test_mcp_rechecks_tenant_even_with_recomputed_digest(monkeypatch):
    _, args, data = context(monkeypatch)
    result = data['payload']['results']['order']
    result['retrieval']['hits'][0]['tenant_id'] = 'demo-b'
    result['hits'][0]['tenant_id'] = 'demo-b'
    data['payload_sha256'] = checksum(data['payload'])
    with pytest.raises(ValueError): validate_result(data, args, PRINCIPAL)


def test_internal_callback_rejects_public_demo_token_and_tenant_override(monkeypatch):
    monkeypatch.delenv('LOGIMATE_RESILIENT_RETRIEVAL', raising=False)
    with TestClient(create_app(token='public-demo')) as client:
        assert client.post('/internal/p6/search', json={'query': '退款'}, headers={'Authorization': 'Bearer public-demo'}).status_code == 401
        assert client.post('/v1/graph/mcp/resilient/queries', json={'query': 'SIM-002'}, headers={'Authorization': 'Bearer public-demo'}).status_code == 503
        assert client.post('/v1/graph/mcp/resilient/queries', json={'query': 'SIM-002', 'tenant_id': 'demo-b'}, headers={'Authorization': 'Bearer public-demo'}).status_code == 422


def test_new_gateways_share_actual_dependency_attempt_window(monkeypatch):
    monkeypatch.setenv('LOGIMATE_DENSE_BACKEND', 'exact')
    primary = FakeSlot('primary', error=DependencyFailure)
    service = ResilientRetrieval(primary=primary, keyword=FakeSlot('keyword'))
    async def run():
        async def thread_fetch(function, connection, arguments):
            return (await service.search(arguments, 'demo-a')).body
        monkeypatch.setattr('logimate.shared_policy.asyncio.to_thread', thread_fetch)
        rows = [await SharedPolicyGateway(PRINCIPAL, CONNECTION).execute({'query': '退款', 'domains': ['order']}) for _ in range(6)]
        assert all(row['ok'] and row['degraded'] for row in rows)
        assert primary.calls == 5 and service.breaker.snapshot()['state'] == 'open'
        assert rows[-1]['payload']['results']['order']['retrieval']['degradation_reason'] == 'circuit_open'
    asyncio.run(run())


def test_missing_model_does_not_search_or_silently_use_fixture_planner(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    service = ResilientRetrieval(primary=FakeSlot('primary'), keyword=FakeSlot('keyword'))
    with TestClient(create_app(token='public-demo', resilient_service=service)) as client:
        result = client.post('/v1/graph/mcp/resilient/queries', json={'query':'SIM-008'}, headers={'Authorization':'Bearer public-demo'})
        assert result.status_code == 503 and service.metrics['requests'] == 0

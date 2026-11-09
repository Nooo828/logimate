"""Explicit planners and policy doubles for graph boundaries; separate protocol smoke is real CPU."""
import asyncio
import json

import pytest
pytest.importorskip('jieba')
from fastapi.testclient import TestClient
from logimate.api import create_app
from logimate.five_agent import run_five_agent, run_tool_graph
from logimate.graph_tools import LocalToolReader
from logimate.policy_tools import PolicyGateway
from logimate.repository import Repository
from scripts.verify_mcp_graph import FixturePlanner, canonical
from test_policy_tools import fake_runner


class ContextPlanner(FixturePlanner):
    def __init__(self): self.context_seen = []
    def complete(self, messages, tools=None, **kwargs):
        if tools:
            user = json.loads(messages[-1]['content'])
            context = user['policy_context']
            assert context['not_order_evidence'] and context['status'] == 'context_only'
            assert '不是实时订单观察' in messages[0]['content']
            assert len(context['hits']) <= 2
            self.context_seen.append(tools[0]['function']['name'])
        return super().complete(messages, tools=tools, **kwargs)


def test_all_four_experts_receive_validated_context_but_diagnosis_uses_only_records(monkeypatch):
    monkeypatch.setenv('LOGIMATE_DENSE_BACKEND', 'exact')
    repo, planner = Repository(), ContextPlanner()
    with LocalToolReader('demo-a', repo, knowledge=True) as reader:
        reader.policy_gateway = PolicyGateway(reader.principal, runner=fake_runner)
        enhanced = run_five_agent(planner, '四域核对SIM-008退货未退款', 'demo-a', repo,
                                  tool_reader=reader, knowledge_reader=reader)
    baseline = run_tool_graph(FixturePlanner(), '四域核对SIM-008退货未退款', 'demo-a', transport='local-gateway')
    assert canonical(enhanced) == canonical(baseline)
    assert len(planner.context_seen) == 4 and len(enhanced['policy_consumption']) == 4
    assert len(enhanced['provider_calls']) == 5 and len(enhanced['tool_calls']) == 4
    assert enhanced['events'].index('policy_context') < enhanced['events'].index('order')
    assert enhanced['events'].count('aggregate') == 1
    assert {f['code'] for f in enhanced['diagnosis']['findings']} == {'REFUND_OVERDUE'}
    assert 'FINANCE_FAILURE_RECORDED' not in str(enhanced['diagnosis'])
    assert set(enhanced['records']) == {'order', 'logistics', 'warehouse', 'aftersales'}
    assert all(e['domain'] != 'policy' for e in enhanced['diagnosis']['evidence'])


def test_policy_failure_stops_before_business_reads_and_preserves_usage(monkeypatch):
    monkeypatch.setenv('LOGIMATE_DENSE_BACKEND', 'exact')
    async def unavailable(*args): raise RuntimeError('explicit unavailable DB')
    repo = Repository()
    with LocalToolReader('demo-a', repo, knowledge=True) as reader:
        reader.policy_gateway = PolicyGateway(reader.principal, runner=unavailable)
        result = run_five_agent(FixturePlanner(), 'SIM-002', 'demo-a', repo,
                                 tool_reader=reader, knowledge_reader=reader)
    assert result['status'] == 'unavailable' and result['diagnosis'] is None
    assert not result['records'] and not result['tool_calls']
    assert len(result['provider_calls']) == 1 and result['policy_transport_audit'][0]['status'] == 'failed'
    assert result['policy_transport_audit'][0]['error_code'] == 'RETRIEVAL_UNAVAILABLE'
    assert result['policy_transport_audit'][0]['server_trace_id']


@pytest.mark.parametrize('action', ['human', 'refuse', 'clarify'])
def test_nonread_route_never_searches_policies_or_business_tools(action):
    class ActionPlanner:
        def complete(self, *args, **kwargs):
            return {'content': json.dumps({'action': action, 'order_ids': ['SIM-002'], 'domains': []})}, {'usage': {}}
    class ForbiddenReader:
        transport = 'explicit-forbidden-test-double'
        def search_policies(self, *args, **kwargs): pytest.fail('unneeded policy search')
        def read(self, *args, **kwargs): pytest.fail('unneeded business read')
    result = run_five_agent(ActionPlanner(), 'SIM-002', 'demo-a', tool_reader=ForbiddenReader(),
                             knowledge_reader=ForbiddenReader())
    assert result['status'] == action and result['policy_context']['status'] == 'not_requested'


def test_new_api_requires_auth_closed_input_and_real_model_configuration(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    with TestClient(create_app(token='policy-unit-token')) as client:
        url = '/v1/graph/mcp/knowledge/queries'; h = {'Authorization': 'Bearer policy-unit-token'}
        assert client.post(url, json={'query': 'SIM-002'}).status_code == 401
        for body in [{'query': 'SIM-002', 'tenant_id': 'demo-b'}, {'query': 'SIM-002', 'backend': 'exact'},
                     {'query': 'SIM-002', 'transport': 'local'}, {'query': ' '}, {'query': 'x' * 501}]:
            assert client.post(url, json=body, headers=h).status_code == 422
        assert client.post(url, json={'query': 'SIM-002'}, headers=h).status_code == 503

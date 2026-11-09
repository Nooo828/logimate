from io import BytesIO
import json
import socket
import ssl
from urllib.error import HTTPError, URLError

import pytest

from logimate.deepseek import DeepSeek, ModelUnavailable, safe_failure
from logimate.five_agent import run_five_agent
from scripts.verify_mcp_graph import FixturePlanner


def test_first_model_failure_records_stage_and_attempt_without_vendor_text():
    class Broken:
        def complete(self, *args, **kwargs):
            raise ModelUnavailable('private token or vendor body', category='provider_http', http_status=429)
    r = run_five_agent(Broken(), 'SIM-002', 'demo-a')
    assert r['status'] == 'unavailable' and r['events'] == ['coordinator_failed', 'aggregate']
    assert r['provider_calls'] == [] and r['usage']['total_tokens'] == 0
    assert r['model_called'] and len(r['model_attempts']) == 1
    assert r['model_attempts'][0]['error'] == {'category': 'provider_http', 'http_status': 429}
    assert r['failure_details'] == [{'agent': 'coordinator', 'stage': 'model_route', 'category': 'provider_http', 'http_status': 429}]
    assert 'private token' not in json.dumps(r)


def test_expert_model_and_tool_failure_are_distinguished():
    class BrokenExpert(FixturePlanner):
        def complete(self, messages, tools=None, **kwargs):
            if tools:
                raise ModelUnavailable('private upstream reason', category='timeout')
            return super().complete(messages, tools=tools, **kwargs)
    r = run_five_agent(BrokenExpert(), 'SIM-002', 'demo-a')
    assert len(r['provider_calls']) == 1 and len(r['model_attempts']) == 2
    assert r['failure_details'] == [{'agent': 'order', 'stage': 'model_plan', 'category': 'timeout'}]
    assert r['diagnosis'] is None


def test_budget_exhaustion_is_not_counted_as_model_attempt():
    r = run_five_agent(FixturePlanner(), 'SIM-002', 'demo-a', budget_seconds=-1)
    assert not r['model_called'] and r['model_attempts'] == []
    assert r['failure_details'][0]['category'] == 'budget'


def test_error_body_is_never_read_and_only_status_is_serialized(monkeypatch):
    class SensitiveBody(BytesIO):
        def read(self, *args, **kwargs):
            pytest.fail('provider error body read')
    error = HTTPError('https://private-token.invalid', 429, 'sensitive reason', {}, SensitiveBody())
    def fail(*args, **kwargs): raise error
    monkeypatch.setattr('logimate.deepseek.urlopen', fail)
    with pytest.raises(ModelUnavailable) as caught:
        DeepSeek(api_key='explicit-test-secret').complete([])
    assert safe_failure(caught.value) == {'category': 'provider_http', 'http_status': 429}


@pytest.mark.parametrize('reason,category', [
    (socket.gaierror('private DNS details'), 'dns'),
    (ssl.SSLError('private TLS details'), 'tls'),
    (TimeoutError('private timeout details'), 'timeout'),
    ('https://user:password@private-proxy', 'network')])
def test_network_classification_does_not_echo_sensitive_reason(monkeypatch, reason, category):
    def fail(*args, **kwargs): raise URLError(reason)
    monkeypatch.setattr('logimate.deepseek.urlopen', fail)
    with pytest.raises(ModelUnavailable) as caught:
        DeepSeek(api_key='explicit-test-secret').complete([])
    assert safe_failure(caught.value) == {'category': category}
    assert 'private' not in str(caught.value) and 'password' not in str(caught.value)


def test_arbitrary_exception_metadata_is_filtered():
    exc = ModelUnavailable('secret', category='secret-category', http_status='secret-status')
    assert safe_failure(exc) == {'category': 'unavailable'}

"""Failed paid-request accounting must remain visible without logging auth headers."""
import json
from io import BytesIO
import pytest
pytest.importorskip('jieba')
from scripts import smoke_policy_graph as smoke


@pytest.mark.parametrize('expected', [200, 503])
def test_http_probe_preserves_usage_on_success_or_failed_expectation(monkeypatch, capsys, expected):
    payload = {'simulated': True, 'status': 'unavailable', 'trace_id': 'test-trace',
               'usage': {'total_tokens': 17}, 'provider_calls': [{'response_id': 'test-provider-response',
                   'usage': {'total_tokens': 17}}], 'model_attempts': [], 'failure_details': []}
    class Response(BytesIO): code = 503
    monkeypatch.setattr(smoke, 'urlopen', lambda *a, **k: Response(json.dumps(payload).encode()))
    if expected == 200:
        with pytest.raises(AssertionError):
            smoke.call('http://127.0.0.1', 'private-test-token', smoke.PATH, {'query': 'SIM-008'}, expected=expected)
    else:
        assert smoke.call('http://127.0.0.1', 'private-test-token', smoke.PATH, {'query': 'SIM-008'}, expected=expected) == payload
    captured = capsys.readouterr()
    assert 'test-provider-response' in captured.err and '17' in captured.err
    assert 'private-test-token' not in captured.out + captured.err

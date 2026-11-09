"""Regression for the real shared-container failure: prior successes delay opening."""
import pytest
from logimate.circuit import CircuitBreaker
from scripts import smoke_resilient as smoke


@pytest.mark.parametrize('previous_successes,failures_needed', [(0, 5), (4, 6), (7, 11)])
def test_outage_waits_for_ratio_then_proves_next_request_skips_primary(monkeypatch, previous_successes, failures_needed):
    breaker = CircuitBreaker(clock=lambda: 0)
    for _ in range(previous_successes): breaker.record(breaker.acquire(), success=True)
    attempts = []
    def request(base, token, path, body=None):
        if path == smoke.HEALTH: return {'body': {'circuit': breaker.snapshot()}}
        permit = breaker.acquire()
        if permit is not None:
            breaker.record(permit, success=False)
            stages, reason = [{'stage': 'primary', 'status': 'timeout_cancelled'}, {'stage': 'keyword'}], 'primary_timeout'
        else: stages, reason = [{'stage': 'keyword'}], 'circuit_open'
        row = {'status_code': 200, 'body': {'retrieval_budget_ms': 800, 'retrieval_elapsed_ms': 620,
            'deadline_overrun': False, 'degraded': True, 'served_mode': 'keyword', 'mode': 'keyword',
            'backend': 'in-memory-exact', 'degradation_reason': reason, 'attempts': stages,
            'circuit': breaker.snapshot()}}
        attempts.append(row)
        return row
    monkeypatch.setattr(smoke, 'request', request)
    monkeypatch.setattr(smoke, 'wait_ready', lambda *args: None)
    result = smoke.check('explicit-http-double', 'test', 'outage')
    assert result['status'] == 'passed' and len(attempts) == failures_needed + 1
    assert result['initial_circuit']['samples'] == previous_successes
    # The request that opens the circuit did perform primary work; do not relabel it.
    assert attempts[-2]['body']['degradation_reason'] == 'primary_timeout'
    assert attempts[-2]['body']['circuit']['state'] == 'open'
    assert attempts[-1]['body']['degradation_reason'] == 'circuit_open'
    assert not any(a['stage'] == 'primary' for a in attempts[-1]['body']['attempts'])


def test_outage_observation_is_bounded_and_does_not_pass_without_a_skip(monkeypatch):
    ticks = [0]
    breaker = CircuitBreaker(clock=lambda: ticks[0])
    calls = []
    def request(base, token, path, body=None):
        if path == smoke.HEALTH: return {'body': {'circuit': breaker.snapshot()}}
        ticks[0] += 31  # Each sample expires before the next; circuit must stay closed.
        breaker.record(breaker.acquire(), success=False)
        calls.append(1)
        return {'status_code': 200, 'body': {'retrieval_budget_ms': 800, 'retrieval_elapsed_ms': 620,
            'deadline_overrun': False, 'degraded': True, 'served_mode': 'keyword', 'mode': 'keyword',
            'backend': 'in-memory-exact', 'degradation_reason': 'primary_timeout',
            'attempts': [{'stage': 'primary'}], 'circuit': breaker.snapshot()}}
    monkeypatch.setattr(smoke, 'request', request)
    monkeypatch.setattr(smoke, 'wait_ready', lambda *args: None)
    with pytest.raises(AssertionError, match='No circuit-open skip'):
        smoke.check('explicit-http-double', 'test', 'outage')
    assert len(calls) == 16

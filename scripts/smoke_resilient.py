"""Real HTTP P6 checks; host time and server retrieval budget are reported separately."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SEARCH = '/v1/knowledge/resilient/search'
HEALTH = '/v1/knowledge/resilient/health'


def request(base, token, path, body=None, *, auth=True):
    headers = {'Content-Type': 'application/json'}
    if auth: headers['Authorization'] = 'Bearer ' + token
    req = Request(base + path, headers=headers, data=None if body is None else json.dumps(body).encode())
    started = time.perf_counter()
    try: response = urlopen(req, timeout=10)
    except HTTPError as exc: response = exc
    with response: status, payload = response.code, json.load(response)
    assert payload['simulated'] is True
    return {'status_code': status, 'host_http_elapsed_ms': round((time.perf_counter() - started) * 1000, 3), 'body': payload}


def wait_ready(base, token):
    deadline = time.monotonic() + 45
    while True:
        row = request(base, token, HEALTH)
        h = row['body']
        if h.get('integrity_failed') or h.get('initialization_error'): raise AssertionError('retrieval startup integrity/config error')
        if h.get('primary', {}).get('ready') and h.get('keyword', {}).get('ready'): return h
        if time.monotonic() > deadline: raise TimeoutError('CPU/keyword readiness exceeded startup allowance')
        time.sleep(.05)


def budget_check(row):
    body = row['body']
    assert body['retrieval_budget_ms'] == 800 and not body['deadline_overrun'], row
    assert body['retrieval_elapsed_ms'] <= 800, row
    if row['status_code'] != 200: assert 'hits' not in body


def check(base, token, mode):
    rows = []
    if mode == 'guards':
        for body, auth, status in [({'query': '退款'}, False, 401),
            ({'query': '退款', 'tenant_id': 'demo-b'}, True, 422),
            ({'query': '退款', 'mode': 'keyword'}, True, 422),
            ({'query': '退款', 'budget': 99}, True, 422),
            ({'query': '退款', 'fault': 'stall'}, True, 422),
            ({'query': ' ', 'top_k': 5}, True, 422), ({'query': '退款', 'top_k': 11}, True, 422)]:
            row = request(base, token, SEARCH, body, auth=auth)
            assert row['status_code'] == status, row
            rows.append(row)
    elif mode == 'normal':
        startup = wait_ready(base, token)
        for query, domain in [('退货入库48小时未退款', None), ('SKU-AX7库存', 'warehouse'), ('签收争议', 'logistics')]:
            row = request(base, token, SEARCH, {'query': query, 'domain': domain})
            budget_check(row)
            assert row['status_code'] == 200 and not row['body']['degraded'], row
            assert row['body']['served_mode'] == 'hybrid' and row['body']['backend'] == 'milvus-hnsw', row
            assert all(h['tenant_id'] == 'demo-a' for h in row['body']['hits'])
            rows.append(row)
    elif mode == 'outage':
        # Previous direct searches and graph domains share this sliding window.
        # A dependency failure that opens the circuit still has reason=primary_timeout;
        # only a subsequent request can demonstrate that primary work was skipped.
        initial_circuit = request(base, token, HEALTH)['body']['circuit']
        deadline = time.monotonic() + 90
        for _ in range(16):
            wait_ready(base, token)  # Model restart is outside an individual retrieval request.
            if time.monotonic() >= deadline: raise TimeoutError('outage circuit observation exceeded allowance')
            row = request(base, token, SEARCH, {'query': '退货未退款'})
            budget_check(row)
            assert row['status_code'] == 200 and row['body']['degraded'], row
            assert row['body']['served_mode'] == row['body']['mode'] == 'keyword', row
            assert row['body']['backend'] == 'in-memory-exact'
            rows.append(row)
            if row['body']['degradation_reason'] == 'circuit_open': break
        else:
            raise AssertionError({'reason': 'No circuit-open skip within bounded observation',
                                  'initial_circuit': initial_circuit, 'rows': rows})
        assert rows[-1]['body']['circuit']['state'] == 'open'
        assert all(a['stage'] != 'primary' for a in rows[-1]['body']['attempts'])
    elif mode == 'recover':
        wait_ready(base, token)
        deadline = time.monotonic() + 20
        while request(base, token, HEALTH)['body']['circuit']['retry_after_seconds'] > 0:
            if time.monotonic() >= deadline: raise TimeoutError('circuit did not allow probe')
            time.sleep(.05)
        row = request(base, token, SEARCH, {'query': '退货未退款'})
        budget_check(row)
        assert row['status_code'] == 200 and not row['body']['degraded'], row
        assert row['body']['circuit']['state'] == 'closed'
        rows.append(row)
    elif mode == 'burst':
        def one(_): return request(base, token, SEARCH, {'query': '退货未退款'})
        with ThreadPoolExecutor(max_workers=10) as pool: rows.extend(pool.map(one, range(20)))
        for row in rows:
            assert row['status_code'] in (200, 429, 503); budget_check(row)
        # Reach the actual per-tenant sliding quota even if earlier capacity rejects did not consume a credit.
        for _ in range(35):
            row = one(None); rows.append(row); budget_check(row)
            if row['status_code'] == 429 and row['body']['degradation_reason'] == 'admission_rate': break
        else: raise AssertionError('process/tenant request quota did not reject excess traffic')
        h = request(base, token, HEALTH)['body']
        assert h['active_requests'] == 0 and not h['primary']['busy'] and not h['keyword']['busy']
    health = request(base, token, HEALTH)['body']
    return {'status': 'passed', 'real_llm_calls': False, 'mode': mode, 'checks': rows, 'health': health,
            **({'initial_circuit': initial_circuit} if mode == 'outage' else {}),
            'host_http_is_not_server_retrieval_budget': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:18083')
    parser.add_argument('--token', default='milvus-joint-verification-only')
    parser.add_argument('--mode', choices=['guards', 'normal', 'outage', 'recover', 'burst'], required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.base, args.token, args.mode), ensure_ascii=False))

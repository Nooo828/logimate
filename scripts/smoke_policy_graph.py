"""Container fixture/real-model acceptance; fixture mode never calls an LLM."""
import argparse
import json
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from logimate.five_agent import run_tool_graph
from scripts.verify_mcp_graph import FixturePlanner, canonical
from scripts.verify_policy_graph import CASES, check

PATH = '/v1/graph/mcp/knowledge/queries'


def fixture_check(outage=False):
    if outage:
        response = run_tool_graph(FixturePlanner(), CASES[1][1], 'demo-a', with_knowledge=True)
        assert response['status'] == 'unavailable' and response['diagnosis'] is None
        assert not response['records'] and not response['tool_calls']
        assert len(response['provider_calls']) == 1
        response.update(planner_test_double=True, model_called=False)
        baseline = run_tool_graph(FixturePlanner(), CASES[1][1], 'demo-a')
        assert baseline['status'] == 'answered'  # Explicit legacy route remains independent of Milvus.
        baseline.update(planner_test_double=True, model_called=False)
        return {'status': 'passed', 'real_llm_calls': False, 'planner_test_double': True,
                'enhanced': response, 'legacy': baseline}
    result = check(FixturePlanner(), both=False)
    for row in result['rows']:
        case = next(c for c in CASES if c[0] == row['case'])
        baseline = run_tool_graph(FixturePlanner(), case[1], 'demo-a')
        assert canonical(baseline) == canonical(row['response'])
        row['legacy_equal'] = True
        baseline.update(planner_test_double=True, model_called=False)
        row['legacy_response'] = baseline
    return result


def call(base, token, path, body, expected=200, auth=True):
    headers = {'Content-Type': 'application/json'}
    if auth: headers['Authorization'] = 'Bearer ' + token
    request = Request(base + path, headers=headers, data=json.dumps(body).encode())
    try: response = urlopen(request, timeout=100)
    except HTTPError as exc: response = exc
    with response: status, payload = response.code, json.load(response)
    # Keep completed provider accounting even if a later assertion fails. No request headers/keys.
    print(json.dumps({'path': path, 'http_status': status, 'status': payload.get('status'),
        'trace_id': payload.get('trace_id'), 'usage': payload.get('usage'),
        'provider_calls': payload.get('provider_calls'), 'model_attempts': payload.get('model_attempts'),
        'failure_details': payload.get('failure_details'), 'policy_transport_audit': payload.get('policy_transport_audit')},
        ensure_ascii=False), file=sys.stderr, flush=True)
    if status != expected:
        print(json.dumps({'unexpected_http_status': status, 'expected': expected,
            'failure_details': payload.get('failure_details'), 'model_attempts': payload.get('model_attempts'),
            'policy_transport_audit': payload.get('policy_transport_audit')}), flush=True)
        raise AssertionError('Policy graph HTTP failed; safe details saved above')
    assert payload['simulated'] is True
    return payload


def guards(base, token):
    rows = []
    for name, body, auth, key, status in [
        ('no-auth', {'query': 'SIM-002'}, False, token, 401),
        ('wrong-auth', {'query': 'SIM-002'}, True, 'wrong-demo-token', 401),
        ('tenant', {'query': 'SIM-002', 'tenant_id': 'demo-b'}, True, token, 422),
        ('backend', {'query': 'SIM-002', 'backend': 'exact'}, True, token, 422),
        ('transport', {'query': 'SIM-002', 'transport': 'local'}, True, token, 422),
        ('empty', {'query': ' '}, True, token, 422),
        ('oversize', {'query': 'x' * 501}, True, token, 422),
    ]:
        call(base, key, PATH, body, expected=status, auth=auth)
        rows.append({'name': name, 'http_status': status})
    return {'status': 'passed', 'real_llm_calls': False, 'checks': rows}


def live_check(base, token, restart=False, outage=False):
    rows = []
    for name, query, _, codes in ([CASES[1]] if restart or outage else CASES[:2]):
        if outage:
            response = call(base, token, PATH, {'query': query}, expected=503)
            assert response['status'] == 'unavailable' and response['diagnosis'] is None
            assert len(response['provider_calls']) == 1 and not response['tool_calls']
            rows.append({'case': name, 'enhanced': response})
            continue
        enhanced = call(base, token, PATH, {'query': query})
        assert enhanced['implementation'] == 'langgraph-five-agent-mcp-policy-v1'
        assert enhanced['status'] == 'answered' and enhanced['model_called']
        assert len(enhanced['policy_consumption']) == 4 and len(enhanced['provider_calls']) == 5
        assert enhanced['usage']['total_tokens'] > 0
        assert all(c.get('response_id') for c in enhanced['provider_calls'])
        assert {f['code'] for f in enhanced['diagnosis']['findings']} == codes
        assert all(v['backend'] == 'milvus-hnsw' for v in enhanced['policy_context']['payload']['results'].values())
        row = {'case': name, 'enhanced': enhanced}
        if not restart:
            legacy = call(base, token, '/v1/graph/mcp/queries', {'query': query})
            assert canonical(legacy) == canonical(enhanced)
            assert legacy['usage']['total_tokens'] > 0 and all(c.get('response_id') for c in legacy['provider_calls'])
            row.update(legacy=legacy, legacy_equal=True)
        rows.append(row)
    responses = [row[key] for row in rows for key in ('enhanced', 'legacy') if key in row]
    return {'status': 'passed', 'real_llm_calls': True, 'rows': rows,
            'provider_responses': sum(len(r['provider_calls']) for r in responses),
            'total_tokens': sum(r['usage']['total_tokens'] for r in responses)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True, choices=['fixture', 'outage-fixture', 'guards', 'live', 'live-restart', 'live-outage'])
    parser.add_argument('--base', default='http://127.0.0.1:8000')
    parser.add_argument('--token', default='milvus-joint-verification-only')
    args = parser.parse_args()
    if args.mode in ('fixture', 'outage-fixture'): result = fixture_check(args.mode == 'outage-fixture')
    elif args.mode == 'guards': result = guards(args.base, args.token)
    else: result = live_check(args.base, args.token, restart=args.mode == 'live-restart', outage=args.mode == 'live-outage')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__': main()

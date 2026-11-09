"""P6.2 guards or real model paired graph HTTP; no credential input here."""
import argparse
import json
from scripts import smoke_policy_graph as previous
from scripts.verify_mcp_graph import canonical

PATH = '/v1/graph/mcp/resilient/queries'


def live(base, token, mode):
    query = '请从四个业务域整体排查SIM-008退货入库48小时未退款，核实订单、物流、仓储和售后。'
    enhanced = previous.call(base, token, PATH, {'query': query})
    assert enhanced['implementation'] == 'langgraph-five-agent-mcp-shared-policy-v2'
    assert enhanced['status'] == 'answered' and enhanced['policy_degraded'] == (mode == 'live-outage')
    assert len(enhanced['policy_consumption']) == 4 and len(enhanced['provider_calls']) == 5
    assert {f['code'] for f in enhanced['diagnosis']['findings']} == {'REFUND_OVERDUE'}
    assert all(e['domain'] != 'policy' for e in enhanced['diagnosis']['evidence'])
    for result in enhanced['policy_context']['payload']['results'].values():
        body = result['retrieval']
        assert body['degraded'] == (mode == 'live-outage')
        assert body['served_mode'] == ('keyword' if mode == 'live-outage' else 'hybrid')
        assert body['retrieval_budget_ms'] == 800 and body['retrieval_elapsed_ms'] <= 800 and not body['deadline_overrun']
    results = {'enhanced': enhanced}
    if mode != 'live-restart':
        legacy = previous.call(base, token, '/v1/graph/mcp/queries', {'query': query})
        assert canonical(legacy) == canonical(enhanced)
        results.update(legacy=legacy, legacy_equal=True)
    values = [results[k] for k in ('enhanced', 'legacy') if k in results]
    assert all(r['usage']['total_tokens'] > 0 and all(c.get('response_id') for c in r['provider_calls']) for r in values)
    return {'status': 'passed', 'real_llm_calls': True, **results,
        'provider_responses': sum(len(r['provider_calls']) for r in values),
        'total_tokens': sum(r['usage']['total_tokens'] for r in values)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:8000')
    parser.add_argument('--token', default='milvus-joint-verification-only')
    parser.add_argument('--mode', choices=['guards', 'live', 'live-outage', 'live-restart'], required=True)
    args = parser.parse_args()
    if args.mode == 'guards':
        previous.PATH = PATH
        result = previous.guards(args.base, args.token)
        previous.call(args.base, args.token, '/internal/p6/search', {'query': '退款'}, expected=401)
    else: result = live(args.base, args.token, args.mode)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__': main()

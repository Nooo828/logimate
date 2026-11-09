"""Real MCP/CPU policy graph; planner is an explicit no-LLM double unless --live."""
import argparse
import getpass
import hashlib
import json
import os
from datetime import datetime, timezone

from logimate.deepseek import DeepSeek
from logimate.five_agent import run_tool_graph
from logimate.retrieval import ROOT
from scripts.verify_mcp_graph import FixturePlanner, canonical

CASES = [
    ('finance', '请从四个业务域整体排查SIM-002退货后未退款。', 'answered', {'REFUND_OVERDUE', 'FINANCE_FAILURE_RECORDED'}),
    ('no-cause', '请从四个业务域整体排查SIM-008退货后未退款。', 'answered', {'REFUND_OVERDUE'}),
    ('missing', '请从四个业务域整体排查SIM-005。', 'answered', {'MISSING_DATA'}),
    ('conflict', '请从四个业务域整体排查SIM-006。', 'answered', {'RETURN_CONFLICT'}),
]


def check(provider, *, live=False, both=True, cases=None):
    rows = []
    for name, query, status, codes in cases or CASES:
        pair = {}
        for transport in (('local-gateway', 'mcp-stdio') if both else ('mcp-stdio',)):
            response = run_tool_graph(provider, query, 'demo-a', transport=transport, with_knowledge=True)
            assert response['status'] == status, (name, transport, response['status'], response.get('failure_details'))
            assert {f['code'] for f in response['diagnosis']['findings']} == codes
            assert len(response['policy_transport_audit']) == 1
            assert response['policy_context']['status'] == 'context_only'
            assert len(response['policy_consumption']) == 4 and len(response['provider_calls']) == 5
            assert len(response['tool_calls']) == 4
            assert response['events'].index('policy_context') < response['events'].index('order')
            policy_ids = {h['evidence_id'] for v in response['policy_context']['payload']['results'].values() for h in v['hits']}
            order_ids = {e['evidence_id'] for e in response['diagnosis']['evidence']}
            assert not policy_ids & order_ids
            if not live:
                response.update(planner_test_double=True, model_called=False)
            pair[transport] = response
            rows.append({'case': name, 'transport': transport, 'expected_match': True, 'response': response})
        if both:
            assert canonical(pair['local-gateway']) == canonical(pair['mcp-stdio'])
            assert pair['local-gateway']['policy_context']['payload'] == pair['mcp-stdio']['policy_context']['payload']
    return {'status': 'passed', 'real_mcp_transport': True, 'real_cpu_embeddings': True,
            'real_llm_calls': live, 'planner_test_double': not live, 'rows': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--mcp-only', action='store_true')
    parser.add_argument('--stdout', action='store_true')
    args = parser.parse_args()
    if args.live and not os.environ.get('DEEPSEEK_API_KEY'):
        os.environ['DEEPSEEK_API_KEY'] = getpass.getpass('DeepSeek API key (hidden, this run only): ')
    provider = DeepSeek() if args.live else FixturePlanner()
    result = check(provider, live=args.live, both=not args.mcp_only)
    if args.stdout:
        print(json.dumps(result, ensure_ascii=False))
        return
    out = ROOT / 'docs/evidence/p5' / datetime.now(timezone.utc).strftime('policy-graph-%Y%m%dT%H%M%S%f')
    out.mkdir(parents=True, exist_ok=False)
    result['input_sha256'] = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for pattern in ('logimate/*.py', 'scripts/verify_policy_graph.py', 'data/knowledge/*.json') for p in ROOT.glob(pattern)}
    (out / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Status: passed\nEvidence: ' + str(out))


if __name__ == '__main__': main()

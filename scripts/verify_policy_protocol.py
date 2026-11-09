"""Real stdio MCP policy discovery, scoped retrieval, closed schema and legacy checks."""
import argparse
import asyncio
import json
from datetime import datetime, timezone

from logimate.mcp_client import connect, server_parameters
from logimate.policy_tools import NAME, PolicyArguments, validate_result
from logimate.retrieval import ROOT
from logimate.rules import DOMAINS
from logimate.tool_gateway import Principal, TOOLS


async def check():
    principal = Principal('demo-a', frozenset(DOMAINS))
    checks = []
    params = server_parameters(principal, knowledge=True)
    assert not {'DEEPSEEK_API_KEY', 'LANGSMITH_API_KEY'} & params.env.keys()
    checks.append('MCP child excludes model and tracing secrets')
    async with connect(principal, knowledge=True) as (session, initialized):
        assert initialized.server_info.version == '0.5.0'
        listed = await session.list_tools()
        assert {t.name for t in listed.tools} == set(TOOLS) | {NAME}
        tool = next(t for t in listed.tools if t.name == NAME)
        assert tool.input_schema == PolicyArguments.model_json_schema()
        assert tool.annotations.model_dump(by_alias=True)['readOnlyHint'] is True
        checks.append('five tools, versioned readonly policy schema')
        args = PolicyArguments(query='退货入库后未退款的核对规则', domains=['aftersales'])
        remote = await session.call_tool(NAME, args.model_dump(), read_timeout_seconds=12)
        assert not remote.is_error
        payload = validate_result(remote.structured_content, args, principal)
        checks.append('real policy retrieval, source hash, tenant/time/domain validation')
        for arguments in [
            {'query': '退款', 'domains': ['aftersales'], 'tenant_id': 'demo-b'},
            {'query': '退款', 'domains': ['aftersales'], 'backend': 'exact'},
            {'query': '退款', 'domains': ['aftersales', 'aftersales']},
            {'query': ' ', 'domains': ['aftersales']},
        ]:
            invalid = await session.call_tool(NAME, arguments)
            assert invalid.is_error and invalid.structured_content['error'] == 'INVALID_ARGUMENTS'
            checks.append('invalid schema rejected before retrieval: ' + str(len(checks)))
    async with connect(Principal('demo-a', frozenset({'order'})), knowledge=True) as (session, _):
        denied = await session.call_tool(NAME, {'query': '退款', 'domains': ['aftersales']})
        assert denied.is_error and denied.structured_content['error'] == 'FORBIDDEN'
        checks.append('policy domain ACL enforced through actual protocol')
    async with connect(principal) as (session, initialized):
        assert initialized.server_info.version == '0.4.0'
        assert {t.name for t in (await session.list_tools()).tools} == set(TOOLS)
        checks.append('legacy endpoint still exposes four read tools only')
    return {'status': 'passed', 'real_mcp_transport': True, 'real_cpu_embeddings': True,
            'real_llm_calls': False, 'checks': checks, 'policy_response': payload}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stdout', action='store_true')
    args = parser.parse_args()
    result = asyncio.run(check())
    if args.stdout: print(json.dumps(result, ensure_ascii=False))
    else:
        out = ROOT / 'docs/evidence/p5' / datetime.now(timezone.utc).strftime('policy-protocol-%Y%m%dT%H%M%S%f')
        out.mkdir(parents=True, exist_ok=False)
        (out / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Status: passed\nEvidence: ' + str(out))

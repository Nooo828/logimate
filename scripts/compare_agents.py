"""Paired real DeepSeek comparison, strictly gated on accepted P2 container evidence."""
import argparse
import getpass
import hashlib
import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path

from logimate.deepseek import DeepSeek
from logimate.five_agent import run_five_agent
from logimate.single_agent import run_agent

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    {"id":"refund-finance","query":"SIM-002退货入库后未退款，请核对四个业务域。","status":"answered","codes":["REFUND_OVERDUE","FINANCE_FAILURE_RECORDED"]},
    {"id":"refund-no-cause","query":"SIM-008退货后没退款，请核对四个业务域的原因证据。","status":"answered","codes":["REFUND_OVERDUE"]},
    {"id":"order-only","query":"查询SIM-001当前订单状态。","status":"answered","codes":[]},
    {"id":"stock-only","query":"核对SIM-004对应SKU的预期和实盘库存。","status":"answered","codes":[]},
    {"id":"missing-domain","query":"请从四个业务域整体排查SIM-005。","status":"answered","codes":["MISSING_DATA"]},
    {"id":"return-conflict","query":"请从四个业务域核对SIM-006退货和退款情况，暴露冲突。","status":"answered","codes":["RETURN_CONFLICT"]},
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats',type=int,default=1,choices=range(1,4))
    args = parser.parse_args()
    candidates=[]
    for p in (ROOT/'docs/evidence/docker-stage').glob('*/summary.json'):
        s=json.loads(p.read_text(encoding='utf-8'))
        if s.get('acceptance_phase')=='P2-live-agent' and s.get('status')=='passed' and s.get('cleanup_passed'):
            candidates.append(p)
    if not candidates:
        raise RuntimeError('P2 container acceptance has not passed; P3 comparison forbidden')
    gate=sorted(candidates)[-1]
    accepted=json.loads(gate.read_text(encoding='utf-8'))
    if not all(s['exit_code']==0 for s in accepted['steps']):
        raise RuntimeError('P2 container gate contains a failed step')
    for name in ('logimate/single_agent.py','logimate/deepseek.py','logimate/repository.py','logimate/rules.py',
                 'prompts/route-v1.txt','prompts/tools-v1.txt','data/synthetic_v1.json'):
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=accepted['input_sha256'].get(name):
            raise RuntimeError('Frozen P2 baseline changed: '+name)
    if not os.environ.get('DEEPSEEK_API_KEY'):
        os.environ['DEEPSEEK_API_KEY']=getpass.getpass('DeepSeek API key (hidden, this run only): ')
    provider=DeepSeek()
    out=ROOT/'docs/evidence/p3'/datetime.now(timezone.utc).strftime('comparison-%Y%m%dT%H%M%S%f')
    out.mkdir(parents=True,exist_ok=False)
    result={'p2_gate':str(gate.relative_to(ROOT)),'p2_gate_sha256':hashlib.sha256(gate.read_bytes()).hexdigest(),
            'model':provider.model,'cases':CASES,'repeats':args.repeats,'rows':[],
            'business_data':'synthetic-v1','real_model_calls':True,
            'input_sha256':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                 for pattern in ('logimate/*.py','prompts/*.txt','data/synthetic_v1.json','requirements.lock') for p in ROOT.glob(pattern)}}
    def save():
        (out/'comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    for repeat in range(args.repeats):
        for i,case in enumerate(CASES):
            # Alternating order limits simple ordering/cache bias; not a randomized trial.
            modes=[('single',run_agent),('five',run_five_agent)]
            if (i+repeat)%2:modes.reverse()
            for mode,fn in modes:
                response=fn(provider,case['query'],'demo-a')
                codes=sorted(f['code'] for f in (response.get('diagnosis') or {}).get('findings',[]))
                expected_domains={'order'} if case['id']=='order-only' else {'order','warehouse'} if case['id']=='stock-only' else {'order','logistics','warehouse','aftersales'}
                tools_correct={c['domain'] for c in response['tool_calls']}==expected_domains
                passed=response['status']==case['status'] and codes==sorted(case['codes']) and tools_correct
                result['rows'].append({'case_id':case['id'],'repeat':repeat,'mode':mode,'expected_match':passed,'response':response})
                save()
                print(f"{case['id']} {mode}: {response['status']}, match={passed}, calls={len(response['provider_calls'])}",flush=True)
    summaries={}
    for mode in ['single','five']:
        rows=[r for r in result['rows'] if r['mode']==mode]
        summaries[mode]={'n':len(rows),'expected_match':sum(r['expected_match'] for r in rows),
            'provider_responses':sum(len(r['response']['provider_calls']) for r in rows),
            'total_tokens':sum(r['response']['usage']['total_tokens'] for r in rows),
            'median_elapsed_ms':statistics.median(r['response']['elapsed_ms'] for r in rows),
            'per_case':[{ 'case_id':r['case_id'],'total_tokens':r['response']['usage']['total_tokens'],'elapsed_ms':r['response']['elapsed_ms']} for r in rows]}
    result['summary']=summaries
    result['limitations']=['Small synthetic sample; no statistical significance or production latency claim.',
        'Same route prompt/model/data/rules; expert prompts and tool scope differ, so effect is not attributable to LangGraph alone.',
        'Stock-only checks record retrieval; no cross-domain diagnosis required. Final findings remain deterministic rules.']
    save()
    print(json.dumps({'evidence':str(out),'summary':summaries},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()

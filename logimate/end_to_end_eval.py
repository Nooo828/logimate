"""Offline scoring only. Planned denominators include missing and failed executions."""
import hashlib
import math
from .models import Diagnosis
from .repository import Repository
from .routing_eval import Route
from .rules import DOMAINS, diagnose

MODES={'single','five','shared'}
IMPLEMENTATIONS={'single':'single-agent-v1','five':'langgraph-five-agent-v1','shared':'langgraph-five-agent-mcp-shared-policy-v2'}


def query_hash(text):return hashlib.sha256(text.encode('utf-8')).hexdigest()


def percentiles(values):
    values=sorted(values)
    return {'n':len(values),'method':'empirical_nearest_rank',**{f'p{p}':values[math.ceil(p/100*len(values))-1] if values else None for p in (50,95,99)}}


def score(dataset,rows,modes,planner_mode,*,provenance):
    if dataset.get('simulated') is not True or dataset.get('split')!='calibration':raise ValueError('unsupported dataset scope')
    cases=dataset['cases'];ids=[c['case_id'] for c in cases]
    if not cases or len(ids)!=len(set(ids)):raise ValueError('empty or duplicate case IDs')
    if not modes or len(modes)!=len(set(modes)) or set(modes)-MODES:raise ValueError('invalid modes')
    if planner_mode not in ('live','fixture'):raise ValueError('invalid planner mode')
    if provenance not in ('new_run','historical_reuse'):raise ValueError('explicit provenance required')
    for c in cases:
        route=Route.model_validate(c['route'])
        if route.action!='read' or len(route.order_ids)!=1 or not route.domains or len(route.domains)!=len(set(route.domains)):raise ValueError('invalid calibration gold')
    cells={};seen_ids=set();repo=Repository()
    for row in rows:
        key=(row['case_id'],row['mode'])
        if key[0] not in ids or key[1] not in modes or key in cells:raise ValueError('unknown or duplicate execution cell')
        case=next(c for c in cases if c['case_id']==key[0])
        if row.get('query_sha256')!=query_hash(case['query']) or row.get('planner_mode')!=planner_mode:raise ValueError('query or planner mismatch')
        r=row['response']
        if r.get('simulated') is not True or r.get('dataset_sha256')!=repo.sha256:raise ValueError('unverified business dataset')
        if r.get('implementation')!=IMPLEMENTATIONS[key[1]]:raise ValueError('mode/implementation mismatch')
        calls=r.get('provider_calls',[]);totals={k:0 for k in ('prompt_tokens','completion_tokens','total_tokens')}
        if planner_mode=='live' and r.get('status')=='answered' and not calls:raise ValueError('answered without provider receipt')
        for call in calls:
            if planner_mode=='fixture' and (call.get('provider')=='deepseek' or call.get('response_id')):raise ValueError('real receipt mislabeled as fixture')
            for k in totals:
                value=call.get('usage',{}).get(k,0)
                if type(value) is not int or value<0:raise ValueError('invalid usage')
                totals[k]+=value
            if planner_mode=='live':
                rid=call.get('response_id')
                if call.get('provider')!='deepseek' or not isinstance(rid,str) or not rid or rid in seen_ids:raise ValueError('unidentified or reused provider receipt')
                seen_ids.add(rid)
        if r.get('usage')!=totals or (planner_mode=='fixture' and any(totals.values())):raise ValueError('inconsistent accounting')
        elapsed=r.get('elapsed_ms')
        if isinstance(elapsed,bool) or not isinstance(elapsed,(int,float)) or not math.isfinite(elapsed) or elapsed<0:raise ValueError('invalid elapsed time')
        cells[key]=r
    summaries={};failures=[]
    for mode in modes:
        counts=dict(planned=len(cases),observed=0,missing=0,unavailable=0,needs_human=0,route_exact=0,expert_exact=0,diagnosis_exact=0,end_to_end_exact=0)
        latencies=[];tokens=responses=0
        for c in cases:
            r=cells.get((c['case_id'],mode))
            if r is None:
                counts['missing']+=1;failures.append({'case_id':c['case_id'],'mode':mode,'reason':'missing_execution'});continue
            counts['observed']+=1;latencies.append(r['elapsed_ms']);tokens+=r['usage']['total_tokens']
            responses+=len(r.get('provider_calls',[])) if planner_mode=='live' else 0
            counts['unavailable']+=int(r.get('status')=='unavailable')
            d=r.get('diagnosis');counts['needs_human']+=int(r.get('status') in ('needs_human','human') or bool(d and d.get('needs_human')))
            try:
                route=Route.model_validate(r.get('route')).model_dump();gold=c['route']
                route_ok=(route['action']==gold['action'] and set(route['order_ids'])==set(gold['order_ids']) and set(route['domains'])==set(gold['domains'])
                    and len(route['order_ids'])==len(set(route['order_ids'])) and len(route['domains'])==len(set(route['domains'])))
            except ValueError:route_ok=False
            expected=repo.get(c['route']['order_ids'][0],'demo-a').model_dump(mode='json')
            domains={*c['route']['domains'],'order'};records={k:expected[k] for k in domains}
            tool_domains=[t.get('domain') for t in r.get('tool_calls',[])]
            expert_ok=r.get('records')==records and set(tool_domains)==domains and len(tool_domains)==len(domains)
            diagnosis_ok=False
            try:
                if c['needs_human'] is None:diagnosis_ok=d is None
                elif d is not None:
                    actual=Diagnosis.model_validate(d).model_dump(mode='json')
                    truth=diagnose(repo.get(c['route']['order_ids'][0],'demo-a'),as_of=repo.dataset.as_of,dataset_id=repo.dataset.dataset_id,intent='overall').model_dump(mode='json')
                    actual.pop('trace_id');truth.pop('trace_id')
                    diagnosis_ok=(actual==truth and sorted(f['code'] for f in d['findings'])==sorted(c['codes']) and d['needs_human'] is c['needs_human'])
            except (ValueError,KeyError,TypeError):pass
            if d is not None and r.get('records')!=records:diagnosis_ok=False  # Correct-looking claims without returned evidence are not grounded.
            ok=r.get('status')==c['status'] and route_ok and expert_ok and diagnosis_ok
            for key,value in [('route_exact',route_ok),('expert_exact',expert_ok),('diagnosis_exact',diagnosis_ok),('end_to_end_exact',ok)]:counts[key]+=int(value)
            if not ok:failures.append({'case_id':c['case_id'],'mode':mode,'reason':'mismatch','route':route_ok,'expert':expert_ok,'diagnosis':diagnosis_ok,'status':r.get('status')})
        summaries[mode]={'counts':counts,'rates':{k:counts[k]/len(cases) for k in ('route_exact','expert_exact','diagnosis_exact','end_to_end_exact','needs_human','unavailable')},
            'latency_ms_observed_including_failures':percentiles(latencies),'recorded_provider_responses':responses,'recorded_tokens':tokens,
            'new_provider_responses_in_this_scoring_process':0,'new_model_calls_in_this_scoring_process':False}
    return {'simulated':True,'dataset_id':dataset['dataset_id'],'split':'calibration','planner_mode':planner_mode,'provenance':provenance,
        'complete':all(v['counts']['missing']==0 for v in summaries.values()),'summary':summaries,'failures':failures,
        'limitations':['Previously exposed synthetic acceptance cases; not holdout accuracy or business time savings.',
            'No model is executed by this scorer. Recorded cost is tokens only; no invented currency pricing.',
            'Empirical quantiles on observed executions include failures; missing executions have no invented latency.',
            'Mode labels are supplied by the collector; raw file hashes must accompany externally supplied predictions.']}

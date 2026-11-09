"""Descriptive paired analysis of recorded answers; no model execution or causal claim."""
import copy
from statistics import median
from .end_to_end_eval import score

MODES=['single','five','shared']


def verify_original_reproduction(calculated,stored):
    if calculated.get('provenance')!='historical_reuse' or stored.get('provenance')!='new_run':raise ValueError('unexpected provenance transition')
    # Only the fact that this is a later offline reuse changes; every metric must reproduce exactly.
    if {**calculated,'provenance':'new_run'}!=stored:raise ValueError('original score reproduction mismatch')


def validate_amendment(original,revised):
    expected=copy.deepcopy(original)
    expected['dataset_id']='synthetic-e2e-calibration-v1.1'
    expected['revision']=revised['revision']
    case=next(c for c in expected['cases'] if c['case_id']=='stock-only')
    if case['route']['domains']!=['order','warehouse']:raise ValueError('unexpected parent stock contract')
    case['route']['domains']=['warehouse']
    if expected!=revised:raise ValueError('amendment changes more than documented route domains')
    if revised['revision']['case_id']!='stock-only' or revised['revision']['field']!='route.domains':raise ValueError('invalid amendment metadata')
    if revised['revision']['old']!=['order','warehouse'] or revised['revision']['new']!=['warehouse']:raise ValueError('amendment description mismatch')


def analyze(original,revised,rows,*,planner_mode='live'):
    validate_amendment(original,revised)
    before=score(original,rows,MODES,planner_mode,provenance='historical_reuse')
    after=score(revised,rows,MODES,planner_mode,provenance='historical_reuse')
    if not before['complete'] or not after['complete']:raise ValueError('paired analysis requires a complete planned matrix')
    by_key={(r['case_id'],r['mode']):r['response'] for r in rows}
    failures=lambda scores:{(r['case_id'],r['mode']) for r in scores['failures']}
    old_fail,new_fail=failures(before),failures(after)
    changes=[{'case_id':c['case_id'],'mode':m,'original_passed':(c['case_id'],m) not in old_fail,
        'revised_passed':(c['case_id'],m) not in new_fail} for c in original['cases'] for m in MODES
        if ((c['case_id'],m) in old_fail)!=((c['case_id'],m) in new_fail)]
    pairs=[]
    for reference,comparison in [('single','five'),('single','shared'),('five','shared')]:
        deltas=[]
        for case in original['cases']:
            cid=case['case_id'];a,b=by_key[cid,reference],by_key[cid,comparison]
            deltas.append({'case_id':cid,'tokens_comparison_minus_reference':b['usage']['total_tokens']-a['usage']['total_tokens'],
                'latency_ms_comparison_minus_reference':round(b['elapsed_ms']-a['elapsed_ms'],3),
                'original_success_delta':int((cid,comparison) not in old_fail)-int((cid,reference) not in old_fail),
                'revised_success_delta':int((cid,comparison) not in new_fail)-int((cid,reference) not in new_fail)})
        pairs.append({'reference':reference,'comparison':comparison,'n_paired_cases':len(deltas),'deltas':deltas,
            'tokens_delta_total':sum(d['tokens_comparison_minus_reference'] for d in deltas),
            'tokens_delta_median':median(d['tokens_comparison_minus_reference'] for d in deltas),
            'latency_ms_delta_median':round(median(d['latency_ms_comparison_minus_reference'] for d in deltas),3),
            'comparison_slower_cases':sum(d['latency_ms_comparison_minus_reference']>0 for d in deltas),
            'original_success_wins_losses_ties':{k:sum((d['original_success_delta']>0 if k=='wins' else d['original_success_delta']<0 if k=='losses' else d['original_success_delta']==0) for d in deltas) for k in ('wins','losses','ties')}})
    return {'simulated':True,'analysis_kind':'post_hoc_descriptive_calibration','new_model_calls':0,'new_provider_responses':0,
        'original_scores':before,'revised_scores':after,'gold_amendment_effects':changes,'paired_comparisons':pairs,
        'recorded_provider_responses':sum(s['recorded_provider_responses'] for s in before['summary'].values()),
        'recorded_tokens':sum(s['recorded_tokens'] for s in before['summary'].values()),
        'limitations':['Same recorded answers rescored against a documented prompt-contract correction; not new model accuracy.',
            'Six previously exposed synthetic cases, one execution per mode per case; no significance/production/holdout claim.',
            'Different expert prompts, tool scope, protocol and retrieval; differences cannot be attributed to LangGraph alone.',
            'Nearest-rank P50 by mode differs from the median of paired differences; no currency cost or business time saving inferred.']}


def markdown(result):
    lines=['# P8.3 recorded paired calibration analysis','',
        'Offline historical reuse only; new model calls: 0. Dataset: six previously exposed synthetic calibration cases.','',
        '| Mode | Original strict | Contract-aligned rescore | Recorded responses | Recorded tokens | Empirical P50 ms (n=6) |',
        '|---|---:|---:|---:|---:|---:|']
    for m in MODES:
        old=result['original_scores']['summary'][m];new=result['revised_scores']['summary'][m]
        lines.append(f"| {m} | {old['counts']['end_to_end_exact']}/6 | {new['counts']['end_to_end_exact']}/6 | {old['recorded_provider_responses']} | {old['recorded_tokens']} | {old['latency_ms_observed_including_failures']['p50']:.3f} |")
    lines+=['','## Paired descriptive deltas','',
        'Positive deltas mean the comparison uses more tokens or takes longer than the reference. All executions, including failures, stay in the matrix.','',
        '| Comparison / reference | Total token delta | Median paired time delta ms | Slower cases | Original success wins/losses/ties |',
        '|---|---:|---:|---:|---|']
    for p in result['paired_comparisons']:
        w=p['original_success_wins_losses_ties']
        lines.append(f"| {p['comparison']} / {p['reference']} | {p['tokens_delta_total']} | {p['latency_ms_delta_median']:.3f} | {p['comparison_slower_cases']}/6 | {w['wins']}/{w['losses']}/{w['ties']} |")
    lines+=['','## Gold correction','',
        'The frozen prompt routes SKU inventory to warehouse. The executor adds order as a tool dependency. Original stock gold included both domains; v1.1 changes only that routing field. Original scores are retained. This correction was identified after seeing the answers and is explicitly post hoc.','',
        'Inspect the paired deltas and strict totals together. These observations describe only the recorded cases; neither a corrected gold label nor a latency/token delta establishes general model superiority.','',
        '## Limits','']+['- '+x for x in result['limitations']]
    return '\n'.join(lines)+'\n'

"""Offline historical reproduction and gold-amendment guardrails; no model execution."""
import copy,json
from pathlib import Path
import pytest
from logimate.paired_analysis import analyze,validate_amendment,markdown,verify_original_reproduction

ROOT=Path(__file__).resolve().parents[1]
pytestmark=pytest.mark.skipif(not (ROOT/'docs/evidence/p8/paired-docker-20261105T074259913219/runtime/live/responses.json').exists(), reason='Private historical live receipts intentionally excluded from public distribution')


@pytest.fixture
def inputs():
    original=json.loads((ROOT/'eval/e2e_v1.json').read_text(encoding='utf-8'))
    revised=json.loads((ROOT/'eval/e2e_v1_1.json').read_text(encoding='utf-8'))
    rows=json.loads((ROOT/'docs/evidence/p8/paired-docker-20261105T074259913219/runtime/live/responses.json').read_text(encoding='utf-8'))
    return original,revised,rows


def test_original_scores_remain_and_contract_rescore_is_explicit_post_hoc(inputs):
    old,new,rows=inputs;snapshot=copy.deepcopy(inputs);result=analyze(old,new,rows)
    assert inputs==snapshot and result['new_model_calls']==result['new_provider_responses']==0
    assert [result['original_scores']['summary'][m]['counts']['end_to_end_exact'] for m in ('single','five','shared')]==[5,5,5]
    assert [result['revised_scores']['summary'][m]['counts']['end_to_end_exact'] for m in ('single','five','shared')]==[6,6,6]
    assert len(result['gold_amendment_effects'])==3 and all(x['case_id']=='stock-only' for x in result['gold_amendment_effects'])
    assert result['recorded_tokens']==34635 and result['recorded_provider_responses']==62


@pytest.mark.parametrize('change',['query','codes','other_case','metadata','extra_domain'])
def test_revision_cannot_silently_change_other_gold_or_questions(inputs,change):
    old,new,_=inputs
    if change=='query':new['cases'][3]['query']='modified'
    if change=='codes':new['cases'][3]['codes']=['STOCK_MISMATCH']
    if change=='other_case':new['cases'][0]['needs_human']=False
    if change=='metadata':new['revision']['new']=['order']
    if change=='extra_domain':new['cases'][3]['route']['domains'].append('aftersales')
    with pytest.raises(ValueError):validate_amendment(old,new)


def test_missing_pair_fails_instead_of_comparing_different_denominators(inputs):
    old,new,rows=inputs
    with pytest.raises(ValueError):analyze(old,new,rows[:-1])


def test_paid_response_reuse_is_rejected(inputs):
    old,new,rows=inputs
    rows[1]['response']['provider_calls'][0]['response_id']=rows[0]['response']['provider_calls'][0]['response_id']
    with pytest.raises(ValueError):analyze(old,new,rows)


def test_paired_deltas_are_per_case_not_subtracted_profile_medians(inputs):
    result=analyze(*inputs);a,b,c=result['paired_comparisons']
    assert [p['tokens_delta_total'] for p in (a,b,c)]==[2967,10959,7992]
    assert [p['comparison_slower_cases'] for p in (a,b,c)]==[5,6,5]
    assert all(p['original_success_wins_losses_ties']=={'wins':0,'losses':0,'ties':6} for p in (a,b,c))
    assert a['latency_ms_delta_median']!=4479.561-3251.530
    assert 'post hoc' in markdown(result) and 'new model calls: 0' in markdown(result)


def test_contract_amendment_does_not_automatically_award_stock_success(inputs):
    old,new,rows=inputs
    for row in rows:
        if row['case_id']=='stock-only':row['response']['route']['domains']=['order','warehouse']
    result=analyze(old,new,rows)
    assert result['original_scores']['summary']['single']['counts']['end_to_end_exact']==6
    assert result['revised_scores']['summary']['single']['counts']['end_to_end_exact']==5


def test_only_provenance_may_change_when_reproducing_original_scores(inputs):
    result=analyze(*inputs)['original_scores'];stored=copy.deepcopy(result);stored['provenance']='new_run'
    verify_original_reproduction(result,stored)
    stored['summary']['single']['counts']['end_to_end_exact']=6
    with pytest.raises(ValueError):verify_original_reproduction(result,stored)

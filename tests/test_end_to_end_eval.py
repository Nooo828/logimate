"""Scorer counterexamples; these manufactured responses are not model evaluation."""
import copy,json
from pathlib import Path
import pytest
from logimate.end_to_end_eval import score,query_hash,percentiles
from logimate.repository import Repository
from logimate.rules import diagnose

DATA=json.loads((Path(__file__).resolve().parents[1]/'eval/e2e_v1.json').read_text(encoding='utf-8'))


def row(case_id='return-conflict'):
    c=next(c for c in DATA['cases'] if c['case_id']==case_id);repo=Repository();s=repo.get(c['route']['order_ids'][0],'demo-a')
    records={d:s.model_dump(mode='json')[d] for d in c['route']['domains']}
    r={'simulated':True,'implementation':'langgraph-five-agent-mcp-shared-policy-v2','dataset_sha256':repo.sha256,'route':c['route'],'status':'answered',
        'tool_calls':[{'domain':d} for d in records],'records':records,'provider_calls':[],'usage':{'prompt_tokens':0,'completion_tokens':0,'total_tokens':0},'elapsed_ms':10,
        'diagnosis':diagnose(s,as_of=repo.dataset.as_of,dataset_id=repo.dataset.dataset_id,intent='overall').model_dump(mode='json') if c['needs_human'] is not None else None}
    return {'case_id':case_id,'mode':'shared','planner_mode':'fixture','query_sha256':query_hash(c['query']),'response':r}


def run(rows):return score(DATA,rows,['single','five','shared'],'fixture',provenance='new_run')


def test_missing_cells_remain_in_denominator_and_have_no_fabricated_latency():
    r=run([row()]);s=r['summary']['shared']
    assert not r['complete'] and s['counts']['planned']==6 and s['counts']['observed']==1 and s['counts']['missing']==5
    assert s['rates']['end_to_end_exact']==1/6 and s['latency_ms_observed_including_failures']['n']==1
    assert r['summary']['single']['latency_ms_observed_including_failures']['p95'] is None


@pytest.mark.parametrize('case_id',[c['case_id'] for c in DATA['cases']])
def test_explicit_fixture_gold_is_scoreable_without_counting_model_calls(case_id):
    s=run([row(case_id)])['summary']['shared']
    assert s['counts']['end_to_end_exact']==1 and s['recorded_provider_responses']==0 and s['recorded_tokens']==0


def test_unavailable_execution_is_observed_failure_and_is_in_latency_sample():
    r=row();r['response'].update(status='unavailable',route=None,records={},diagnosis=None,tool_calls=[],elapsed_ms=123)
    s=run([r])['summary']['shared']
    assert s['counts']['observed']==1 and s['counts']['unavailable']==1 and s['counts']['end_to_end_exact']==0
    assert s['latency_ms_observed_including_failures']['p99']==123


@pytest.mark.parametrize('change',['query','duplicate','unknown','negative_tokens','fake_tokens','nonfinite_latency','wrong_dataset','wrong_planner'])
def test_bad_cells_cannot_be_silently_ignored(change):
    r=row();rows=[r]
    if change=='query':r['query_sha256']='wrong'
    if change=='duplicate':rows.append(copy.deepcopy(r))
    if change=='unknown':r['case_id']='not-in-manifest'
    if change=='negative_tokens':r['response']['provider_calls']=[{'usage':{'total_tokens':-1}}]
    if change=='fake_tokens':r['response']['usage']['total_tokens']=10
    if change=='nonfinite_latency':r['response']['elapsed_ms']=float('nan')
    if change=='wrong_dataset':r['response']['dataset_sha256']='wrong'
    if change=='wrong_planner':r['planner_mode']='live'
    with pytest.raises(ValueError):run(rows)


def test_diagnosis_without_returned_source_records_fails_grounding():
    r=row();r['response']['records']['warehouse']['actual_stock']+=1
    s=run([r])['summary']['shared'];assert s['counts']['diagnosis_exact']==0 and s['counts']['end_to_end_exact']==0


def test_duplicate_route_slots_fail_route_layer():
    r=row();r['response']['route']=copy.deepcopy(r['response']['route']);r['response']['route']['order_ids']*=2
    assert run([r])['summary']['shared']['counts']['route_exact']==0


def test_live_receipts_cannot_be_reused_across_cells():
    first=row();second=row('finance-no-cause')
    for r in (first,second):
        r['planner_mode']='live';r['response']['provider_calls']=[{'provider':'deepseek','response_id':'explicit-test-receipt','usage':{'total_tokens':0}}]
    with pytest.raises(ValueError):score(DATA,[first,second],['shared'],'live',provenance='historical_reuse')


def test_percentiles_are_explicit_empirical_nearest_rank():
    assert percentiles(list(range(1,101)))=={'n':100,'method':'empirical_nearest_rank','p50':50,'p95':95,'p99':99}


def test_response_cannot_be_relabelled_as_another_implementation():
    r=row();r['mode']='single'
    with pytest.raises(ValueError):run([r])


def test_real_provider_receipt_cannot_be_labelled_fixture_even_with_zero_tokens():
    r=row();r['response']['provider_calls']=[{'provider':'deepseek','response_id':'explicit-test'}]
    with pytest.raises(ValueError):run([r])

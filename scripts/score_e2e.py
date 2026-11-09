"""Score stored structured responses only; --p73-history reuses one accepted case, never calls a model."""
import argparse
from datetime import datetime,timezone
import hashlib,json
from pathlib import Path
from logimate.end_to_end_eval import score,query_hash
from logimate.retrieval import ROOT


def main():
    p=argparse.ArgumentParser(description=__doc__);g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--responses',type=Path);g.add_argument('--p73-history',action='store_true')
    p.add_argument('--planner-mode',choices=['fixture','live'],default='live');p.add_argument('--historical-reuse',action='store_true');a=p.parse_args()
    manifest=ROOT/'eval/e2e_v1.json';dataset=json.loads(manifest.read_text(encoding='utf-8'))
    if a.p73_history:
        source=ROOT/'docs/evidence/p7/analysis-docker-20261105T030815426996/summary.json'
        accepted=json.loads(source.read_text(encoding='utf-8'))
        if accepted['status']!='passed' or not accepted['cleanup_passed']:raise ValueError('accepted P7.3 gate required')
        # The actual query is locked by the accepted smoke helper's source hash.
        helper=ROOT/'scripts/smoke_analysis_review.py'
        assert hashlib.sha256(helper.read_bytes()).hexdigest()==accepted['input_sha256']['scripts/smoke_analysis_review.py']
        from scripts.smoke_analysis_review import QUERY
        c=next(c for c in dataset['cases'] if c['case_id']=='return-conflict');assert c['query']==QUERY
        r=accepted['results']['live-analysis-create']['view']['thread']['case']['analysis_receipt']['result']
        rows=[{'case_id':c['case_id'],'mode':'shared','planner_mode':'live','query_sha256':query_hash(QUERY),'response':r}]
        planner='live';provenance='historical_reuse'
    else:
        source=a.responses;rows=json.loads(source.read_text(encoding='utf-8'));planner=a.planner_mode
        provenance='historical_reuse' if a.historical_reuse else 'new_run'
    result=score(dataset,rows,['single','five','shared'],planner,provenance=provenance)
    result['input_sha256']={str(q.relative_to(ROOT)) if q.is_relative_to(ROOT) else 'external_responses':hashlib.sha256(q.read_bytes()).hexdigest()
        for q in [manifest,source,ROOT/'logimate/end_to_end_eval.py',ROOT/'scripts/score_e2e.py',ROOT/'data/synthetic_v1.json']}
    out=ROOT/'docs/evidence/p8'/datetime.now(timezone.utc).strftime('offline-score-%Y%m%dT%H%M%S%f');out.mkdir(parents=True)
    (out/'scores.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'complete':result['complete'],'summary':result['summary'],'evidence':str(out)},ensure_ascii=False))


if __name__=='__main__':main()

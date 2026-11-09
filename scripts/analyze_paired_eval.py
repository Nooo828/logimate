"""P8.3 offline analysis. Original accepted data and scores are never rewritten."""
import argparse,hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from logimate.retrieval import ROOT
from logimate.paired_analysis import analyze,markdown,verify_original_reproduction


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--evidence',type=Path,default=ROOT/'docs/evidence/p8/paired-docker-20261105T074259913219');a=p.parse_args()
    source=a.evidence.resolve();gate=json.loads((source/'summary.json').read_text(encoding='utf-8'))
    if gate['status']!='passed' or not gate['cleanup_passed'] or not gate['exported_artifact_hashes_verified'] or any(s['exit_code'] for s in gate['steps']):raise ValueError('actual accepted collection required')
    folder=source/'runtime/live';s=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    if s['planner_mode']!='live' or not s['billing_is_complete'] or not s['scores_complete'] or not s['input_unchanged']:raise ValueError('incomplete live source')
    for name,h in s['output_sha256'].items():
        if name not in ('responses.json','scores.json','ledger.sqlite3') or hashlib.sha256((folder/name).read_bytes()).hexdigest()!=h:raise ValueError('artifact source hash mismatch')
    old_path=ROOT/'eval/e2e_v1.json';new_path=ROOT/'eval/e2e_v1_1.json';prompt=ROOT/'prompts/route-v1.txt'
    old=json.loads(old_path.read_text(encoding='utf-8'));new=json.loads(new_path.read_text(encoding='utf-8'))
    if hashlib.sha256(old_path.read_bytes()).hexdigest()!=new['revision']['parent_sha256']:raise ValueError('wrong parent manifest')
    if hashlib.sha256(prompt.read_bytes()).hexdigest()!=new['revision']['contract_sha256'] or new['revision']['contract_sha256']!=gate['input_sha256']['prompts/route-v1.txt']:raise ValueError('frozen contract changed')
    for name in ('logimate/end_to_end_eval.py','data/synthetic_v1.json'):
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=gate['input_sha256'][name]:raise ValueError('scoring source/data changed')
    result=analyze(old,new,json.loads((folder/'responses.json').read_text(encoding='utf-8')))
    verify_original_reproduction(result['original_scores'],json.loads((folder/'scores.json').read_text(encoding='utf-8')))
    result['source_score_provenance']='new_run'
    paths=[source/'summary.json',folder/'responses.json',folder/'scores.json',folder/'ledger.sqlite3',old_path,new_path,prompt,ROOT/'logimate/paired_analysis.py',ROOT/'scripts/analyze_paired_eval.py']
    result['source_sha256']={str(q.relative_to(ROOT)) if q.is_relative_to(ROOT) else str(q):hashlib.sha256(q.read_bytes()).hexdigest() for q in paths}
    out=ROOT/'docs/evidence/p8'/datetime.now(timezone.utc).strftime('paired-analysis-%Y%m%dT%H%M%S%f');out.mkdir(parents=True)
    (out/'analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'report.md').write_text(markdown(result),encoding='utf-8')
    print('Status: analyzed_without_new_model_calls');print('Evidence: '+str(out))


if __name__=='__main__':main()

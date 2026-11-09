"""Synthetic corpus; real CPU embeddings. RRF selection uses development labels only."""
import argparse
import hashlib
import importlib.metadata
import json
import statistics
import time
from datetime import datetime, timezone
from logimate.retrieval import CORPUS, INDEX, MODEL, ROOT, KnowledgeSearch, cache_dir, chunks, eligible, load_encoder, normalized


def score(gold, ids):
    if not gold or len(ids) != len(set(ids)): raise ValueError('invalid gold/result')
    n = len(set(ids) & set(gold))
    return {'recall_at_5': n/len(gold), 'hit_at_5': float(n>0),
            'mrr': next((1/r for r,c in enumerate(ids,1) if c in gold),0), 'gold_precision_at_5': n/5}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-index', action='store_true')
    parser.add_argument('--download-model', action='store_true')
    args = parser.parse_args()
    gates = []
    for p in (ROOT/'docs/evidence/docker-stage').glob('*/summary.json'):
        s = json.loads(p.read_text(encoding='utf-8'))
        if s.get('acceptance_phase')=='P4.2-graph-mcp' and s.get('status')=='passed' and s.get('cleanup_passed'): gates.append(p)
    if not gates: raise RuntimeError('P4.2 container gate not passed')
    corpus = json.loads(CORPUS.read_text(encoding='utf-8'))
    qa_path = ROOT/'data/knowledge/qa-v1.json'; qa = json.loads(qa_path.read_text(encoding='utf-8'))
    digest = hashlib.sha256(CORPUS.read_bytes()).hexdigest()
    if qa['corpus_sha256'] != digest: raise ValueError('gold corpus mismatch')
    pieces = chunks(corpus)
    gold = {q['id']: [c['chunk_id'] for c in pieces if c['section_id']==q['gold_section'] and eligible(c,'demo-a',corpus['as_of'])] for q in qa['cases']}
    if any(not v for v in gold.values()): raise ValueError('empty gold')
    out = ROOT/'docs/evidence/p5'/datetime.now(timezone.utc).strftime('retrieval-%Y%m%dT%H%M%S%f'); out.mkdir(parents=True)
    (out/'frozen-gold.json').write_text(json.dumps({'corpus_sha256':digest,'gold':gold},indent=2),encoding='utf-8')
    start = time.perf_counter(); encoder = load_encoder(download=args.download_model); startup = (time.perf_counter()-start)*1000
    if args.build_index:
        title = list(encoder.passage_embed([c['title_path']+'\n'+c['body'] for c in pieces]))
        body = list(encoder.passage_embed([c['body'] for c in pieces]))
        weights = {p.relative_to(cache_dir()/'model').as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (cache_dir()/'model').rglob('*') if p.is_file() and 'snapshots' in p.parts and p.suffix in {'.json','.onnx'}}
        index = {'corpus_id':corpus['corpus_id'],'corpus_sha256':digest,'as_of':corpus['as_of'],'embedding_model':MODEL,'model_weight_sha256':weights,'chunks':pieces,
                 'vectors_title':{c['chunk_id']:normalized(v) for c,v in zip(pieces,title)},'vectors_body':{c['chunk_id']:normalized(v) for c,v in zip(pieces,body)}}
        INDEX.write_text(json.dumps(index,ensure_ascii=False),encoding='utf-8')
    service = KnowledgeSearch(encoder=encoder)
    def evaluate(cases, mode, k=60, title=True):
        rows=[]
        for q in cases:
            r=service.search(q['query'],'demo-a',mode=mode,k=k,title=title)
            assert all(eligible(c,'demo-a',corpus['as_of']) for c in r['hits'])
            ids=[c['chunk_id'] for c in r['hits']]
            rows.append({'id':q['id'],'ids':ids,**score(gold[q['id']],ids),'elapsed_ms':r['elapsed_ms']})
        return {'rows':rows,'metrics':{m:round(statistics.mean(r[m] for r in rows),6) for m in ['recall_at_5','hit_at_5','mrr','gold_precision_at_5']} | {'n':len(rows),'median_elapsed_ms':statistics.median(r['elapsed_ms'] for r in rows)}}
    dev=[q for q in qa['cases'] if q['split']=='dev']; holdout=[q for q in qa['cases'] if q['split']=='holdout']
    sweep={str(k):evaluate(dev,'hybrid',k) for k in [10,30,60]}
    selected=max([10,30,60],key=lambda k:(sweep[str(k)]['metrics']['recall_at_5'],sweep[str(k)]['metrics']['mrr'],k))
    results={m:evaluate(holdout,m,selected) for m in ['keyword','dense','hybrid']}
    results['hybrid_no_title']=evaluate(holdout,'hybrid',selected,title=False)
    result={'simulated':True,'real_embedding_inference':True,'real_llm_calls':False,'p4_2_gate':str(sorted(gates)[-1].relative_to(ROOT)),
        'corpus_sha256':digest,'qa_sha256':hashlib.sha256(qa_path.read_bytes()).hexdigest(),'index_sha256':hashlib.sha256(INDEX.read_bytes()).hexdigest(),
        'model':MODEL,'model_startup_ms':startup,'chunks':len(pieces),'eligible_chunks':sum(eligible(c,'demo-a',corpus['as_of']) for c in pieces),
        'versions':{p:importlib.metadata.version(p) for p in ['fastembed','onnxruntime','jieba','numpy']},'rrf_dev_sweep':sweep,'selected_k':selected,'holdout':results,
        'limitations':['12 author-owned paraphrase holdouts share topics with dev; not blind generalization.','Exact in-memory cosine, not Milvus/HNSW. No LLM answers.','Warm timings include query encoding/ranking; exclude model startup/index building.','No chunk-size ablation yet; title ablation changes lexical and vector inputs.']}
    (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    manifest={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for pattern in ['logimate/*.py','scripts/evaluate_retrieval.py','data/knowledge/*.json','requirements-retrieval.lock'] for p in ROOT.glob(pattern)}
    (out/'input-sha256.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps({'evidence':str(out),'selected_k':selected,'holdout':{m:r['metrics'] for m,r in results.items()}},ensure_ascii=False))


if __name__=='__main__': main()

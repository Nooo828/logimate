"""Offline real-CPU chunk ablation. Dev-only selection; no production index changes."""
import hashlib
import importlib.metadata
import json
import statistics
import time
from datetime import datetime, timezone

from logimate.chunking_experiment import (calibrate, chunk_documents, evidence_metrics,
                                         materialize, validate_questions, within_budget)
from logimate.retrieval import ROOT, bm25, eligible, normalized, rrf
from scripts.build_chunking_fixture import DIRECTORY
from scripts.smoke_milvus_api import exact_service

GATE = ROOT / 'docs/evidence/p5/milvus-api-20261103T111704748814/summary.json'
MODES = ('keyword', 'dense', 'hybrid')
BUDGET = 1000


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def summarize(rows, threshold):
    positive = [r for r in rows if r['answerable']]
    negative = [r for r in rows if not r['answerable']]
    accepted = lambda r: bool(r['has_hits'] and r['confidence'] >= threshold)
    return {'split': rows[0]['split'], 'answerable_n': len(positive), 'no_answer_n': len(negative),
            'evidence_recall_at_5': statistics.mean(r['at_5']['evidence_recall'] for r in positive),
            'all_evidence_at_5': statistics.mean(r['at_5']['all_evidence'] for r in positive),
            'whole_span_recall_at_5': statistics.mean(r['at_5']['whole_span_recall'] for r in positive),
            'mrr_whole_span': statistics.mean(r['at_5']['mrr_whole_span'] for r in positive),
            'evidence_recall_at_1000_chars': statistics.mean(r['at_budget']['evidence_recall'] for r in positive),
            'all_evidence_at_1000_chars': statistics.mean(r['at_budget']['all_evidence'] for r in positive),
            'mean_top5_context_chars': statistics.mean(r['top5_chars'] for r in positive),
            'mean_budget_context_chars': statistics.mean(r['budget_chars'] for r in positive),
            'always_return_no_answer_false_acceptance': statistics.mean(r['has_hits'] for r in negative),
            'calibrated_no_answer_false_acceptance': statistics.mean(accepted(r) for r in negative),
            'calibrated_answerable_acceptance': statistics.mean(accepted(r) for r in positive),
            'calibrated_all_evidence_acceptance': statistics.mean(accepted(r) and bool(r['at_5']['all_evidence']) for r in positive)}


def select_design(development):
    if not development or any(v['hybrid']['metrics']['split'] != 'dev' for v in development.values()):
        raise ValueError('chunk selection accepts development summaries only')
    def key(name):
        hybrid = development[name]['hybrid']
        m = hybrid['metrics']
        return (m['evidence_recall_at_1000_chars'], m['all_evidence_at_1000_chars'],
                hybrid['calibration']['balanced_accuracy'], m['mrr_whole_span'], -m['mean_top5_context_chars'])
    return max(sorted(development), key=key)


def main():
    gate = json.loads(GATE.read_text(encoding='utf-8'))
    if gate.get('status') != 'passed' or not gate.get('cleanup_passed'):
        raise RuntimeError('P5.2b joint container gate not passed')
    # Existing accepted runtime inputs stay frozen throughout this isolated experiment.
    if any(digest(ROOT / name) != value for name, value in gate['input_sha256'].items()):
        raise RuntimeError('Accepted joint baseline changed before experiment')
    corpus_path, qa_path = DIRECTORY / 'corpus.json', DIRECTORY / 'qa.json'
    corpus = json.loads(corpus_path.read_text(encoding='utf-8'))
    qa = json.loads(qa_path.read_text(encoding='utf-8'))
    if qa['corpus_sha256'] != digest(corpus_path): raise ValueError('experiment gold/corpus mismatch')
    documents, evidence = materialize(corpus)
    validate_questions(qa['cases'], documents, evidence, corpus['as_of'])
    out = ROOT / 'docs/evidence/p5' / datetime.now(timezone.utc).strftime('chunking-%Y%m%dT%H%M%S%f')
    out.mkdir(parents=True, exist_ok=False)
    design = {'status': 'frozen-before-inference', 'schemes': ['fixed', 'structure'],
              'sizes_chars': [100, 220, 400], 'extra_title_injection': [False, True],
              'modes': list(MODES), 'rrf_k': 60, 'top_k': 5, 'context_char_budget': BUDGET,
              'selection': 'Dev hybrid evidence recall@1000 chars, full evidence@1000 chars, dev answerability balanced accuracy, MRR, lower mean Top5 chars; stable lexical ID tie break.',
              'threshold': 'Native top score, dev balanced accuracy; tie prefer no-answer rejection then larger threshold. Not a probability.',
              'corpus_sha256': digest(corpus_path), 'qa_sha256': digest(qa_path)}
    def write(name, payload):
        (out / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    write('frozen-design.json', design)
    write('frozen-source-gold.json', {'evidence': evidence, 'cases': qa['cases']})
    write('canonical-documents.json', documents)
    variants = {}
    for scheme in design['schemes']:
        for size in design['sizes_chars']:
            pieces = chunk_documents(documents, scheme, size)
            for title in design['extra_title_injection']:
                key = f'{scheme}-{size}-title{int(title)}'
                variants[key] = {'scheme': scheme, 'size': size, 'title': title, 'chunks': pieces}
    write('chunk-manifests.json', variants)
    report = {'status': 'running', 'simulated': True, 'real_cpu_embeddings': True,
              'real_llm_calls': False, 'milvus_used_in_experiment': False,
              'joint_gate_sha256': digest(GATE), 'phase_times_ms': {},
              'source_sha256': {p.relative_to(ROOT).as_posix(): digest(p)
                  for pattern in ['scripts/evaluate_chunking.py', 'scripts/build_chunking_fixture.py',
                                  'logimate/chunking_experiment.py', 'data/knowledge/experiments/v2/*.json'] for p in ROOT.glob(pattern)},
              'versions': {p: importlib.metadata.version(p) for p in ('fastembed', 'onnxruntime', 'jieba', 'numpy')}}
    vectors, embeddings = {'passage': {}, 'query': {}}, {'passage': 0, 'query': 0}
    try:
        start = time.perf_counter()
        v1 = exact_service()
        v1.search('校验模型缓存', 'demo-a', mode='dense')  # Verifies frozen weight hashes, offline only.
        encoder = v1.encoder
        report['model_weight_sha256'] = v1.index['model_weight_sha256']
        report['phase_times_ms']['verified_model_warmup'] = (time.perf_counter() - start) * 1000

        def embed(texts, kind):
            missing = sorted(set(texts) - vectors[kind].keys())
            if not missing: return
            started = time.perf_counter()
            function = encoder.passage_embed if kind == 'passage' else encoder.query_embed
            values = list(function(missing))
            if len(values) != len(missing): raise ValueError('encoder result cardinality mismatch')
            for text, value in zip(missing, values): vectors[kind][text] = normalized(value)
            embeddings[kind] += len(values)
            label = kind + '_inference'
            report['phase_times_ms'][label] = report['phase_times_ms'].get(label, 0) + (time.perf_counter() - started) * 1000

        texts = [((c['title_path'] + '\n') if v['title'] else '') + c['body']
                 for v in variants.values() for c in v['chunks']]
        embed(texts, 'passage')

        def evaluate(cases, variant, mode):
            rows = []
            for case in cases:
                pool = [c for c in variant['chunks'] if eligible(c, 'demo-a', corpus['as_of'], case.get('domain'))]
                sparse = bm25(case['query'], pool, with_title=variant['title']) if mode != 'dense' else []
                dense = []
                if mode != 'keyword':
                    query = vectors['query'][case['query']]
                    dense = sorted([(c['chunk_id'], sum(a * b for a, b in zip(query, vectors['passage'][
                        ((c['title_path'] + '\n') if variant['title'] else '') + c['body']]))) for c in pool],
                        key=lambda pair: (-pair[1], pair[0]))[:10]
                ranking = rrf([sparse, dense], 60) if mode == 'hybrid' else sparse if mode == 'keyword' else dense
                by_id = {c['chunk_id']: c for c in pool}
                # First ten candidates for the budget metric; fixed Top5 is reported separately.
                ranked = [by_id[cid] for cid, _ in ranking[:10]]
                hits = ranked[:5]
                budget_hits, used = within_budget(ranked, BUDGET, variant['title'])
                _, top5_chars = within_budget(hits, 1000000, variant['title'])
                row = {'id': case['id'], 'split': case['split'], 'answerable': case['answerable'],
                       'has_hits': bool(hits), 'confidence': ranking[0][1] if ranking else 0.0,
                       'ids_at_5': [c['chunk_id'] for c in hits], 'ids_at_budget': [c['chunk_id'] for c in budget_hits],
                       'top5_chars': top5_chars, 'budget_chars': used}
                if case['answerable']:
                    row['at_5'] = evidence_metrics(case['gold_evidence'], hits, evidence)
                    row['at_budget'] = evidence_metrics(case['gold_evidence'], budget_hits, evidence)
                else: row['no_answer_reason'] = case['reason']
                rows.append(row)
            return rows

        dev = [q for q in qa['cases'] if q['split'] == 'dev']
        embed([q['query'] for q in dev], 'query')
        development = {}
        for name, variant in variants.items():
            development[name] = {}
            for mode in MODES:
                rows = evaluate(dev, variant, mode)
                calibration = calibrate(rows)
                development[name][mode] = {'calibration': calibration, 'rows': rows,
                                           'metrics': summarize(rows, calibration['threshold'])}
        selected = select_design(development)
        write('development.json', development)
        write('selection-dev.json', {'frozen_at_utc': datetime.now(timezone.utc).isoformat(),
                                     'selected_variant': selected,
                                     'thresholds': {n: {m: v[m]['calibration'] for m in MODES} for n, v in development.items()},
                                     'development_sha256': digest(out / 'development.json')})
        # Holdout inference/ranking starts only after all choices/thresholds have been persisted.
        holdout = [q for q in qa['cases'] if q['split'] == 'holdout']
        embed([q['query'] for q in holdout], 'query')
        results = {}
        for name, variant in variants.items():
            results[name] = {}
            for mode in MODES:
                rows = evaluate(holdout, variant, mode)
                threshold = development[name][mode]['calibration']['threshold']
                results[name][mode] = {'threshold_from_dev': threshold, 'rows': rows,
                                       'metrics': summarize(rows, threshold)}
        report.update({'status': 'passed', 'selected_on_dev': selected,
                      'dev_n': len(dev), 'holdout_n': len(holdout), 'variant_count': len(variants),
                      'variant_mode_count': len(variants) * len(MODES), 'embedding_counts': embeddings,
                      'chunk_counts': {name: {'all': len(v['chunks']), 'eligible': sum(eligible(c, 'demo-a', corpus['as_of']) for c in v['chunks'])} for name, v in variants.items()},
                      'holdout': results, 'selection_sha256': digest(out / 'selection-dev.json'),
                      'limitations': ['All policies/questions are author-owned synthetic stress data; no blind generalization.',
                                      'Fixed character counts, not model token budgets. Source-span union coverage is retrieval evidence, not answer accuracy.',
                                      'Native score thresholds are corpus-specific, not probabilities or production refusal policy.',
                                      'Exact CPU cosine isolates chunk design; no new Milvus/container/graph or HNSW performance claims.',
                                      'Cached query vectors are reused for ranking; phase times are not online request latency.',
                                      'All holdout variants are descriptive ablations; no winner is selected using holdout results.']})
        write('embeddings.json', {'model_weight_sha256': report['model_weight_sha256'], 'vectors_by_input_text': vectors})
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__)
        raise
    finally:
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        write('summary.json', report)
        print('Evidence: ' + str(out), flush=True)
    print(json.dumps({'status': report['status'], 'selected_on_dev': selected,
                      'selected_holdout': {m: results[selected][m]['metrics'] for m in MODES}}, ensure_ascii=False))


if __name__ == '__main__': main()

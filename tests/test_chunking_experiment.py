"""Source-span, table, filtering and development-only evaluation contracts."""
import copy
import pytest
pytest.importorskip('jieba')

from logimate.chunking_experiment import (calibrate, chunk_documents, evidence_metrics,
    materialize, span_covered, validate_questions, within_budget)
from scripts.build_chunking_fixture import build, doc, fact, section
from scripts.evaluate_chunking import select_design


def fixture():
    corpus, cases = build()
    docs, evidence = materialize(corpus)
    return corpus, cases, docs, evidence


def test_labels_are_valid_source_spans_and_not_chunk_ids():
    corpus, cases, docs, evidence = fixture()
    validate_questions(cases, docs, evidence, corpus['as_of'])
    for split in ('dev', 'holdout'):
        assert sum(c['split'] == split and c['answerable'] for c in cases) == 12
        assert sum(c['split'] == split and not c['answerable'] for c in cases) == 6
    for span in evidence.values():
        source = next(d['source'] for d in docs if d['doc_id'] == span['doc_id'])
        assert source[span['start']:span['end']] == span['quote']
    dev_gold = {eid for c in cases if c['split'] == 'dev' for eid in c['gold_evidence']}
    heldout_gold = {eid for c in cases if c['split'] == 'holdout' for eid in c['gold_evidence']}
    assert not (dev_gold & heldout_gold)


@pytest.mark.parametrize('size', [100, 220, 400])
def test_fixed_windows_preserve_exact_source_and_dynamic_tables_preserve_headers(size):
    _, _, docs, _ = fixture()
    fixed = chunk_documents(docs, 'fixed', size)
    structured = chunk_documents(docs, 'structure', size)
    assert all(len(c['body']) <= size for c in fixed + structured)
    assert len({c['chunk_id'] for c in structured}) == len(structured)
    for source in docs:
        assert ''.join(c['body'] for c in fixed if c['doc_id'] == source['doc_id']) == source['source']
    table_chunks = [c for c in structured if c['body'].startswith('| 材料 |')]
    assert table_chunks
    assert all(c['body'].splitlines()[1] == '| --- | --- | --- |' for c in table_chunks)
    for name in ('实际送达回执', '商品破损照片'):
        rows = [line for c in table_chunks for line in c['body'].splitlines() if line.startswith('| ' + name + ' |')]
        assert len(rows) == 1


def test_structural_hard_cut_preserves_full_ascii_identifier():
    import re
    corpus = {'documents': [doc('identifier', 'warehouse', 'SKU', section('one', '标识',
              fact('E', '仓' * 95 + 'SKU-AX70' + '核' * 20)))]}
    docs, _ = materialize(corpus)
    pieces = chunk_documents(docs, 'structure', 100)
    identifiers = [word for c in pieces for word in re.findall(r'[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*', c['body'])]
    assert identifiers == ['SKU-AX70']


def test_span_union_handles_boundary_split_but_not_gaps_or_other_docs():
    span = {'doc_id': 'a', 'start': 3, 'end': 8}
    split = [{'doc_id': 'a', 'source_spans': [[0, 5]]}, {'doc_id': 'a', 'source_spans': [[5, 10]]}]
    assert span_covered(span, split)
    metric = evidence_metrics(['E'], split, {'E': span})
    assert metric['evidence_recall'] == 1 and metric['whole_span_recall'] == 0
    assert metric['mrr_whole_span'] == 0
    assert not span_covered(span, [{'doc_id': 'a', 'source_spans': [[0, 4], [6, 10]]}])
    assert not span_covered(span, [{'doc_id': 'other', 'source_spans': [[0, 100]]}])
    with pytest.raises(ValueError): evidence_metrics([], [], {})


def test_context_budget_includes_extra_titles_and_keeps_rank_prefix():
    hits = [{'body': 'x' * 90, 'title_path': 'title'}, {'body': 'x' * 50, 'title_path': 'title'},
            {'body': 'x', 'title_path': 'title'}]
    accepted, used = within_budget(hits, 100, True)
    assert accepted == hits[:1] and used == 96
    assert len(within_budget(hits, 141, False)[0]) == 3


@pytest.mark.parametrize('change', ['foreign', 'unknown', 'duplicate', 'no-answer'])
def test_bad_gold_is_rejected_before_model_inference(change):
    corpus, cases, docs, evidence = fixture()
    case = copy.deepcopy(cases[0])
    if change == 'foreign': case['gold_evidence'] = ['X-foreign']
    if change == 'unknown': case['gold_evidence'] = ['missing']
    if change == 'duplicate': case['gold_evidence'] *= 2
    if change == 'no-answer': case['answerable'] = False
    with pytest.raises(ValueError): validate_questions([case], docs, evidence, corpus['as_of'])


def test_native_score_threshold_uses_only_development_labels():
    rows = [{'split': 'dev', 'answerable': True, 'has_hits': True, 'confidence': .9},
            {'split': 'dev', 'answerable': False, 'has_hits': True, 'confidence': .2}]
    result = calibrate(rows)
    assert result['balanced_accuracy'] == 1 and .2 < result['threshold'] < .9
    rows[0]['split'] = 'holdout'
    with pytest.raises(ValueError): calibrate(rows)
    with pytest.raises(ValueError): select_design({'fake': {'hybrid': {'metrics': {'split': 'holdout'}}}})


def test_frozen_fixture_cannot_be_overwritten(tmp_path, monkeypatch):
    from scripts import build_chunking_fixture as builder
    monkeypatch.setattr(builder, 'DIRECTORY', tmp_path)
    original = tmp_path / 'corpus.json'; original.write_text('frozen', encoding='utf-8')
    with pytest.raises(RuntimeError): builder.main()
    assert original.read_text(encoding='utf-8') == 'frozen'

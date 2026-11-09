"""Offline-only P5.2c experiment. Source-span gold is independent of chunk IDs."""
import hashlib
import math
import re
import statistics

from .retrieval import eligible


def materialize(corpus):
    documents, evidence = [], {}
    seen = set()
    for raw in corpus['documents']:
        if raw['doc_id'] in seen: raise ValueError('duplicate source document')
        seen.add(raw['doc_id'])
        doc = {k: v for k, v in raw.items() if k != 'sections'}
        text, units, sections = '', [], []
        for section in raw['sections']:
            section_start = len(text)
            heading = '## ' + section['heading']
            units.append({'kind': 'heading', 'start': len(text), 'end': len(text) + len(heading)})
            text += heading + '\n\n'
            for block in section['blocks']:
                if block['kind'] not in ('paragraph', 'table') or not block['text']:
                    raise ValueError('invalid source unit')
                start = len(text)
                text += block['text'] + '\n\n'
                units.append({'kind': block['kind'], 'start': start, 'end': start + len(block['text'])})
                for eid, quote in block.get('evidence', {}).items():
                    if eid in evidence or not quote or block['text'].count(quote) != 1:
                        raise ValueError('ambiguous/duplicate evidence annotation')
                    offset = start + block['text'].index(quote)
                    evidence[eid] = {'doc_id': doc['doc_id'], 'start': offset,
                                     'end': offset + len(quote), 'quote': quote}
            sections.append({'start': section_start, 'end': len(text), 'heading': section['heading']})
        documents.append(doc | {'source': text, 'units': units, 'sections': sections})
    return documents, evidence


def validate_questions(cases, documents, evidence, as_of):
    docs = {d['doc_id']: d for d in documents}
    seen = set()
    for case in cases:
        if case['id'] in seen or case['split'] not in ('dev', 'holdout') or not case['query'].strip():
            raise ValueError('invalid case identity/split/query')
        seen.add(case['id'])
        gold = case['gold_evidence']
        if len(gold) != len(set(gold)) or bool(gold) != case['answerable']:
            raise ValueError('answerability/gold contract mismatch')
        if not gold and not case.get('reason'): raise ValueError('unanswerable case needs reason')
        for eid in gold:
            if eid not in evidence: raise ValueError('unknown evidence')
            span = evidence[eid]
            doc = docs[span['doc_id']]
            if not eligible(doc, 'demo-a', as_of, case.get('domain')):
                raise ValueError('gold outside accessible policy pool')
            if doc['source'][span['start']:span['end']] != span['quote']:
                raise ValueError('gold source offset mismatch')


def _chunk(doc, spans, scheme, size, number, separator):
    body = separator.join(doc['source'][start:end] for start, end in spans)
    if not body or len(body) > size: raise ValueError('chunk exceeds character budget')
    headings = [s['heading'] for s in doc['sections']
                if any(start < s['end'] and end > s['start'] for start, end in spans)]
    metadata = {k: v for k, v in doc.items() if k not in ('source', 'units', 'sections')}
    return metadata | {'chunk_id': f"{doc['doc_id']}:{scheme}:{size}:{number}:" + hashlib.sha256(body.encode()).hexdigest()[:12],
                       'body': body, 'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
                       'title_path': doc['title'] + ' > ' + ' / '.join(headings),
                       'source_spans': [list(span) for span in spans]}


def _paragraph_parts(text, start, end, size):
    while end - start > size:
        cut = start + size
        punctuation = list(re.finditer(r'[。！？；\n]', text[start:cut]))
        if punctuation: cut = start + punctuation[-1].end()
        # Preserve complete SKU/lot/ASCII identifiers if a hard cut is needed.
        for identifier in re.finditer(r'[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*', text[start:end]):
            left, right = start + identifier.start(), start + identifier.end()
            if left < cut < right:
                cut = left if left > start else right
                break
        if cut <= start or cut - start > size: raise ValueError('source identifier too long')
        yield start, cut
        start = cut
    if end > start: yield start, end


def _table_parts(text, start, end, size):
    lines = text[start:end].splitlines(keepends=True)
    if len(lines) < 2 or not re.fullmatch(r'[\s|:\-]+', lines[1]):
        raise ValueError('Markdown table requires a header separator')
    header_end = start + len(lines[0]) + len(lines[1])
    while header_end > start and text[header_end - 1] in '\r\n': header_end -= 1
    header = (start, header_end)
    current, used = [header], header_end - start
    offset = start + len(lines[0]) + len(lines[1])
    for line in lines[2:]:
        row_end = offset + len(line.rstrip('\r\n'))
        width = row_end - offset
        if header_end - start + 1 + width > size:
            raise ValueError('table row and repeated header exceed budget; author must split cells')
        if used + 1 + width > size:
            yield current
            current, used = [header], header_end - start
        current.append((offset, row_end)); used += 1 + width
        offset += len(line)
    if len(current) > 1: yield current


def chunk_documents(documents, scheme, size):
    if scheme not in ('fixed', 'structure') or size < 80: raise ValueError('invalid chunk design')
    chunks = []
    for doc in documents:
        made = []
        if scheme == 'fixed':
            for start in range(0, len(doc['source']), size):
                made.append(_chunk(doc, [(start, min(start + size, len(doc['source'])))], scheme, size, len(made), ''))
        else:
            current, width = [], 0
            def flush():
                nonlocal current, width
                if current: made.append(_chunk(doc, current, scheme, size, len(made), '\n\n'))
                current, width = [], 0
            for unit in doc['units']:
                # Heading boundaries are explicit; rows always retain their original table header.
                if unit['kind'] == 'heading': flush()
                if unit['kind'] == 'table':
                    flush()
                    for spans in _table_parts(doc['source'], unit['start'], unit['end'], size):
                        made.append(_chunk(doc, spans, scheme, size, len(made), '\n'))
                    continue
                for start, end in _paragraph_parts(doc['source'], unit['start'], unit['end'], size):
                    cost = end - start + (2 if current else 0)
                    if width + cost > size:
                        flush(); cost = end - start
                    current.append((start, end)); width += cost
            flush()
        chunks.extend(made)
    return chunks


def merge_intervals(spans):
    merged = []
    for start, end in sorted(spans):
        if end <= start: raise ValueError('invalid source interval')
        if merged and start <= merged[-1][1]: merged[-1][1] = max(merged[-1][1], end)
        else: merged.append([start, end])
    return merged


def span_covered(span, hits):
    intervals = merge_intervals([pair for hit in hits if hit['doc_id'] == span['doc_id'] for pair in hit['source_spans']])
    return any(start <= span['start'] and end >= span['end'] for start, end in intervals)


def evidence_metrics(gold, hits, evidence):
    if not gold: raise ValueError('no-answer case has no evidence recall denominator')
    covered = [span_covered(evidence[eid], hits) for eid in gold]
    whole = [any(span_covered(evidence[eid], [hit]) for hit in hits) for eid in gold]
    first = next((1 / rank for rank, hit in enumerate(hits, 1)
                  if any(span_covered(evidence[eid], [hit]) for eid in gold)), 0.0)
    return {'evidence_recall': sum(covered) / len(gold), 'all_evidence': float(all(covered)),
            'whole_span_recall': sum(whole) / len(gold), 'mrr_whole_span': first}


def within_budget(hits, budget, with_title):
    accepted, used = [], 0
    for hit in hits:
        cost = len(hit['body']) + (len(hit['title_path']) + 1 if with_title else 0)
        if used + cost > budget: break  # Strict ranked prefix, no length-based reranking.
        accepted.append(hit); used += cost
    return accepted, used


def calibrate(dev_rows):
    if not dev_rows or any(r['split'] != 'dev' for r in dev_rows):
        raise ValueError('threshold calibration accepts development rows only')
    positives = [r for r in dev_rows if r['answerable']]
    negatives = [r for r in dev_rows if not r['answerable']]
    if not positives or not negatives: raise ValueError('calibration needs both classes')
    scores = sorted(set(r['confidence'] for r in dev_rows))
    if any(not math.isfinite(s) for s in scores): raise ValueError('nonfinite retrieval score')
    thresholds = [scores[0] - 1e-9, *[(a + b) / 2 for a, b in zip(scores, scores[1:])], scores[-1] + 1e-9]
    def rates(threshold):
        recall = statistics.mean(r['has_hits'] and r['confidence'] >= threshold for r in positives)
        rejection = statistics.mean(not r['has_hits'] or r['confidence'] < threshold for r in negatives)
        return {'answerable_acceptance': recall, 'no_answer_rejection': rejection,
                'balanced_accuracy': (recall + rejection) / 2}
    # Predeclared tie break: fewer false accepts, then larger threshold.
    selected = max(thresholds, key=lambda t: (rates(t)['balanced_accuracy'], rates(t)['no_answer_rejection'], t))
    return {'threshold': selected, 'calibration_split': 'dev', 'n': len(dev_rows), **rates(selected)}

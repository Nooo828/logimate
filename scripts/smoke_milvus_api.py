"""Actual HTTP vs actual CPU exact retrieval; no LLM or simulated DB responses."""
import argparse
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from logimate.retrieval import KnowledgeSearch


def exact_service():
    # Only this short-lived comparison process changes backend selection.
    previous = os.environ.get('LOGIMATE_DENSE_BACKEND')
    os.environ['LOGIMATE_DENSE_BACKEND'] = 'exact'
    try:
        return KnowledgeSearch()
    finally:
        if previous is None: os.environ.pop('LOGIMATE_DENSE_BACKEND', None)
        else: os.environ['LOGIMATE_DENSE_BACKEND'] = previous


def call(base, token, payload, expected=200, auth=True):
    headers = {'Content-Type': 'application/json'}
    if auth: headers['Authorization'] = 'Bearer ' + token
    request = Request(base + '/v1/knowledge/search', headers=headers,
                      data=json.dumps(payload).encode())
    try: response = urlopen(request, timeout=40)
    except HTTPError as exc: response = exc
    with response:
        status, body = response.code, json.load(response)
    if status != expected or body.get('simulated') is not True:
        raise AssertionError(f'Unexpected HTTP status/contract: {status}, expected {expected}')
    return body


def compare_response(body, expected, mode):
    backend = 'in-memory-exact' if mode == 'keyword' else 'milvus-hnsw'
    if body.get('backend') != backend:
        raise AssertionError('HTTP response did not use required backend')
    for name in ('corpus_id', 'corpus_sha256', 'as_of', 'mode', 'rrf_k', 'embedding_model'):
        if body[name] != expected[name]: raise AssertionError('Retrieval version/mode mismatch')
    expected_hits = {c['chunk_id']: c for c in expected['hits']}
    ids = [c['chunk_id'] for c in body['hits']]
    if len(set(ids)) != len(ids) or set(ids) != set(expected_hits):
        raise AssertionError('HTTP/exact Top5 set mismatch')
    for hit in body['hits']:
        reference = expected_hits[hit['chunk_id']]
        for field in ('tenant_id', 'published', 'domain', 'valid_from', 'valid_to', 'body_sha256', 'body', 'title_path'):
            if hit[field] != reference[field]: raise AssertionError('Untrusted HTTP evidence metadata')
    return {'backend': backend, 'same_set': True,
            'same_order': ids == [c['chunk_id'] for c in expected['hits']], 'ids': ids}


def check(base, token, outage=False):
    exact = exact_service()
    rows = []
    if outage:
        for mode in ('dense', 'hybrid'):
            body = call(base, token, {'query': '退货未退款', 'mode': mode}, expected=503)
            if 'hits' in body: raise AssertionError('Unavailable dense service returned successful evidence')
            rows.append({'mode': mode, 'http_status': 503, 'no_fallback': True})
        payload = {'query': '退货未退款', 'mode': 'keyword'}
        body = call(base, token, payload)
        result = compare_response(body, exact.search(payload['query'], 'demo-a', mode='keyword'), 'keyword')
        rows.append({'mode': 'keyword', 'http_status': 200, **result})
        return {'status': 'passed', 'phase': 'database-stopped', 'real_llm_calls': False, 'checks': rows}

    for name, query, domain in [('refund', '退货入库48小时未退款', None),
                                ('sku', '查询SKU-AX7库存差异', None),
                                ('warehouse-filter', '退货退款', 'warehouse')]:
        for mode in ('keyword', 'dense', 'hybrid'):
            payload = {'query': query, 'mode': mode, 'domain': domain}
            expected = exact.search(query, 'demo-a', mode=mode, domain=domain)
            body = call(base, token, payload)
            rows.append({'case': name, 'mode': mode, 'http_status': 200,
                         **compare_response(body, expected, mode)})
    guards = []
    for name, payload, auth, credential, status in [
        ('no-auth', {'query': '退款'}, False, token, 401),
        ('wrong-auth', {'query': '退款'}, True, 'wrong-demo-token', 401),
        ('client-tenant', {'query': '退款', 'tenant_id': 'demo-b'}, True, token, 422),
        ('client-backend', {'query': '退款', 'backend': 'exact'}, True, token, 422),
        ('empty-query', {'query': ' '}, True, token, 422),
        ('invalid-mode', {'query': '退款', 'mode': 'invented'}, True, token, 422),
        ('top-k', {'query': '退款', 'top_k': 11}, True, token, 422),
        ('invalid-domain', {'query': '退款', 'domain': 'private'}, True, token, 422),
    ]:
        call(base, credential, payload, expected=status, auth=auth)
        guards.append({'name': name, 'http_status': status})
    return {'status': 'passed', 'real_cpu_embeddings': True, 'real_llm_calls': False,
            'checks': rows, 'input_guards': guards}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True)
    parser.add_argument('--token', default='milvus-joint-verification-only')
    parser.add_argument('--outage', action='store_true')
    args = parser.parse_args()
    print(json.dumps(check(args.base, args.token, args.outage), ensure_ascii=False))

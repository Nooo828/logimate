"""Versioned, read-only policy context. Policies never represent order observations."""
import asyncio
from collections import deque
import hashlib
import json
import math
import os
import sys
import time
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, ValidationError, model_validator

from .deepseek import ModelUnavailable
from .models import Model
from .retrieval import INDEX, ROOT, KnowledgeSearch, eligible
from .tool_gateway import Principal

NAME = 'search_policy_context'
VERSION = 'policy-context-v1'
POLICY = 'p5-read-policy-context-v1'
KIND = 'synthetic_policy'
SERVER_VERSION = '0.5.0'
HITS_PER_DOMAIN = 2
WORKER_SECONDS = 8.0


class PolicyUnavailable(ModelUnavailable):
    def __init__(self, code, trace_id):
        super().__init__('Policy context unavailable; no fallback', category='tool_unavailable')
        self.policy_error_code = code
        self.policy_trace_id = trace_id


class PolicyArguments(Model):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=500, strict=True)
    domains: list[Literal['order', 'logistics', 'warehouse', 'aftersales']] = Field(min_length=1, max_length=4)

    @model_validator(mode='after')
    def unique_domains(self):
        if len(self.domains) != len(set(self.domains)): raise ValueError('duplicate domain')
        return self


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def evidence_id(hit, corpus_id):
    return f"{corpus_id}:{hit['chunk_id']}:{hit['body_sha256'][:12]}"


def retrieve(principal, args):
    if not set(args.domains) <= principal.domains: raise ValueError('forbidden policy domain')
    service = KnowledgeSearch()  # Explicit configured backend; no local fallback.
    results = {}
    for domain in args.domains:
        response = service.search(args.query, principal.tenant_id, domain=domain,
                                  mode='hybrid', top_k=HITS_PER_DOMAIN)
        hits = [{**hit, 'evidence_kind': KIND, 'evidence_id': evidence_id(hit, service.index['corpus_id'])}
                for hit in response['hits']]
        results[domain] = {'backend': response['backend'], 'hits': hits}
    return {'corpus_id': service.index['corpus_id'], 'corpus_sha256': service.index['corpus_sha256'],
            'index_sha256': hashlib.sha256(INDEX.read_bytes()).hexdigest(),
            'as_of': service.index['as_of'], 'results': results}


def validate_payload(payload, args, principal):
    if not set(args.domains) <= principal.domains: raise ValueError('forbidden policy scope')
    # Metadata validation only: this reference object never executes a dense query.
    index = KnowledgeSearch(dense_backend=object()).index
    keys = {'corpus_id', 'corpus_sha256', 'index_sha256', 'as_of', 'results'}
    if not isinstance(payload, dict) or set(payload) != keys: raise ValueError('invalid policy payload')
    expected = {'corpus_id': index['corpus_id'], 'corpus_sha256': index['corpus_sha256'],
                'index_sha256': hashlib.sha256(INDEX.read_bytes()).hexdigest(), 'as_of': index['as_of']}
    if any(payload.get(k) != v for k, v in expected.items()): raise ValueError('stale policy context')
    if not isinstance(payload['results'], dict) or set(payload['results']) != set(args.domains):
        raise ValueError('policy domains mismatch')
    selected = os.environ.get('LOGIMATE_DENSE_BACKEND', 'exact')
    if selected not in ('exact', 'milvus'): raise ValueError('invalid trusted policy backend')
    expected_backend = 'milvus-hnsw' if selected == 'milvus' else 'in-memory-exact'
    by_id = {c['chunk_id']: c for c in index['chunks']}
    for domain, result in payload['results'].items():
        if not isinstance(result, dict) or set(result) != {'backend', 'hits'} or result['backend'] != expected_backend:
            raise ValueError('unexpected policy backend/envelope')
        hits = result['hits']
        count = min(HITS_PER_DOMAIN, sum(eligible(c, principal.tenant_id, index['as_of'], domain) for c in index['chunks']))
        if not isinstance(hits, list) or len(hits) != count: raise ValueError('incomplete policy hit count')
        seen = set()
        for rank, hit in enumerate(hits, 1):
            if not isinstance(hit, dict): raise ValueError('invalid policy hit')
            cid = hit.get('chunk_id'); reference = by_id.get(cid)
            if reference is None or cid in seen: raise ValueError('unknown/duplicate policy hit')
            seen.add(cid)
            if not eligible(reference, principal.tenant_id, index['as_of'], domain):
                raise ValueError('policy tenant/publication/time/domain mismatch')
            if set(hit) != set(reference) | {'score', 'rank', 'evidence_kind', 'evidence_id'}:
                raise ValueError('unrecognized policy fields')
            if any(hit[k] != v for k, v in reference.items()): raise ValueError('policy content/hash mismatch')
            if hit['evidence_kind'] != KIND or hit['evidence_id'] != evidence_id(reference, index['corpus_id']):
                raise ValueError('policy evidence identity mismatch')
            score = hit['score']
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or hit['rank'] != rank:
                raise ValueError('invalid policy rank/score')
    return payload


def validate_result(data, args, principal):
    if not isinstance(data, dict) or data.get('simulated') is not True or any(data.get(k) != v for k, v in
        {'tool': NAME, 'tool_version': VERSION, 'policy_version': POLICY, 'evidence_kind': KIND}.items()):
        raise ValueError('invalid policy tool envelope')
    try: UUID(data.get('trace_id', ''))
    except (ValueError, TypeError, AttributeError): raise ValueError('invalid policy trace') from None
    if data.get('ok') is False:
        if data.get('error') in {'TIMEOUT', 'RETRIEVAL_UNAVAILABLE', 'RATE_LIMITED'}:
            raise PolicyUnavailable(data['error'], data['trace_id'])
        raise ValueError('policy request rejected')
    if data.get('ok') is not True or data.get('error') is not None or data.get('status') != 'context_only':
        raise ValueError('policy cannot claim order/answer status')
    keys = {'trace_id', 'simulated', 'tool', 'tool_version', 'policy_version', 'evidence_kind',
            'ok', 'error', 'elapsed_ms', 'status', 'payload', 'payload_sha256', 'query_sha256'}
    elapsed = data.get('elapsed_ms')
    if set(data) != keys or isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError('invalid policy envelope fields/timing')
    if data.get('query_sha256') != hashlib.sha256(args.query.encode()).hexdigest():
        raise ValueError('policy query mismatch')
    payload = validate_payload(data.get('payload'), args, principal)
    if data.get('payload_sha256') != checksum(payload): raise ValueError('policy envelope hash mismatch')
    return data


async def run_worker(principal, args):
    from .mcp_client import server_parameters
    env = server_parameters(principal, knowledge=True).env
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'logimate.policy_worker',
        cwd=ROOT, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        request = {'tenant_id': principal.tenant_id, 'domains': sorted(principal.domains),
                   'arguments': args.model_dump()}
        stdout, _ = await process.communicate(json.dumps(request).encode())
        if process.returncode or len(stdout) > 131072:
            raise RuntimeError('policy worker failed; private output withheld')
        return json.loads(stdout)
    finally:
        # On timeout/cancellation actually terminate the isolated CPU/HTTP work and reap it.
        if process.returncode is None:
            try: process.kill()
            except ProcessLookupError: pass
            await process.communicate()


class PolicyGateway:
    """Admission and time bounds are per session, not cross-request P6 limits."""
    def __init__(self, principal, *, runner=None, timeout=WORKER_SECONDS):
        if not math.isfinite(timeout) or not 0 < timeout <= WORKER_SECONDS: raise ValueError('invalid policy worker budget')
        self.principal, self.runner, self.timeout = principal, runner or run_worker, timeout
        self.active = False
        self.requests = deque()

    async def execute(self, arguments):
        start = time.monotonic()
        base = {'trace_id': str(uuid4()), 'simulated': True, 'tool': NAME,
                'tool_version': VERSION, 'policy_version': POLICY, 'evidence_kind': KIND}
        def finish(error=None, **values):
            return {**base, 'ok': error is None, 'error': error,
                    'elapsed_ms': round((time.monotonic() - start) * 1000, 3), **values}
        try: args = PolicyArguments.model_validate(arguments)
        except ValidationError: return finish('INVALID_ARGUMENTS')
        if not set(args.domains) <= self.principal.domains: return finish('FORBIDDEN')
        now = time.monotonic()
        while self.requests and self.requests[0] <= now - 60: self.requests.popleft()
        if self.active or len(self.requests) >= 4: return finish('RATE_LIMITED')
        self.requests.append(now); self.active = True
        try:
            payload = await asyncio.wait_for(self.runner(self.principal, args), timeout=self.timeout)
            payload = validate_payload(payload, args, self.principal)
            return finish(status='context_only', payload=payload, payload_sha256=checksum(payload),
                          query_sha256=hashlib.sha256(args.query.encode()).hexdigest())
        except TimeoutError: return finish('TIMEOUT')
        except Exception: return finish('RETRIEVAL_UNAVAILABLE')
        finally: self.active = False

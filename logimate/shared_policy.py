"""P6.2 MCP callback to one API-process retrieval service; no per-session fallback."""
import asyncio
import hashlib
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID, uuid4

from .policy_tools import NAME, KIND, HITS_PER_DOMAIN, PolicyArguments, PolicyUnavailable, checksum, evidence_id
from .retrieval import INDEX

VERSION = 'policy-context-shared-v2'
POLICY = 'p6-shared-retrieval-v1'
SERVER_VERSION = '0.6.0'


def validate_connection(connection):
    if not isinstance(connection, dict) or set(connection) != {'url', 'token'}:
        raise ValueError('invalid shared connection')
    parsed = urllib.parse.urlsplit(connection['url'])
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port or
            parsed.username or parsed.password or parsed.path != '/internal/p6/search' or parsed.query or parsed.fragment):
        raise ValueError('shared callback must be an explicit loopback endpoint')
    if not isinstance(connection['token'], str) or len(connection['token']) < 32:
        raise ValueError('invalid internal capability')
    return connection


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def fetch(connection, arguments):
    # Ignore user proxy settings; never forward the internal capability via redirects.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(connection['url'], data=json.dumps(arguments).encode(),
        headers={'Authorization': 'Bearer ' + connection['token'], 'Content-Type': 'application/json'})
    try:
        with opener.open(request, timeout=1.5) as response:
            raw = response.read(131073)
            if len(raw) > 131072: raise ValueError('oversized shared retrieval body')
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        # Private error bodies are not copied into MCP or provider prompts.
        if exc.code == 429: raise PolicyUnavailable('RATE_LIMITED', str(uuid4())) from None
        raise PolicyUnavailable('RETRIEVAL_UNAVAILABLE', str(uuid4())) from None


def validate_search(body, query, domain, principal):
    """Revalidate callback source/ACL plus explicit mode and budget semantics."""
    from .resilient_retrieval import ResilientRetrieval, IntegrityFailure
    from .resilient_worker import SearchArguments
    if not isinstance(body, dict) or body.get('status') not in {'ok', 'no_match'}:
        raise ValueError('unsuccessful shared result')
    UUID(body.get('trace_id', ''))
    if (body.get('requested_mode') != 'hybrid' or body.get('evidence_kind') != KIND or
            type(body.get('degraded')) is not bool or body.get('deadline_overrun') is not False or
            body.get('retrieval_budget_ms') != 800): raise ValueError('invalid shared budget/mode')
    elapsed = body.get('retrieval_elapsed_ms')
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or not 0 <= elapsed <= 800:
        raise ValueError('invalid shared timing')
    degraded = body['degraded']
    if body.get('served_mode') != ('keyword' if degraded else 'hybrid') or body.get('mode') != body['served_mode']:
        raise ValueError('hidden degradation')
    reason = body.get('degradation_reason')
    if (degraded and (not isinstance(reason, str) or not reason)) or (not degraded and reason is not None):
        raise ValueError('invalid degradation reason')
    fields = {'simulated', 'corpus_id', 'corpus_sha256', 'as_of', 'backend', 'mode', 'rrf_k',
              'embedding_model', 'hits', 'elapsed_ms', 'limitations'}
    service = ResilientRetrieval()  # Metadata validation only; no start, IPC, CPU encoder or DB call.
    try:
        service._validate({k: body[k] for k in fields}, principal.tenant_id,
            SearchArguments(query=query, domain=domain, top_k=HITS_PER_DOMAIN), 'keyword' if degraded else 'primary')
    except IntegrityFailure: raise ValueError('shared source contract rejected') from None
    if body['status'] != ('ok' if body['hits'] else 'no_match'): raise ValueError('invalid no-match semantics')
    return body


def validate_result(data, args, principal):
    if not isinstance(data, dict) or any(data.get(k) != v for k, v in {
        'tool': NAME, 'tool_version': VERSION, 'policy_version': POLICY, 'evidence_kind': KIND, 'simulated': True}.items()):
        raise ValueError('invalid shared policy identity')
    UUID(data.get('trace_id', ''))
    if data.get('ok') is False:
        if data.get('error') in {'RETRIEVAL_UNAVAILABLE', 'RATE_LIMITED', 'TIMEOUT'}:
            raise PolicyUnavailable(data['error'], data['trace_id'])
        raise ValueError('shared policy rejected')
    if data.get('ok') is not True or data.get('status') != 'context_only' or data.get('error') is not None:
        raise ValueError('invalid shared policy status')
    if set(data) != {'trace_id', 'simulated', 'tool', 'tool_version', 'policy_version', 'evidence_kind',
                     'ok', 'error', 'elapsed_ms', 'status', 'payload', 'payload_sha256', 'query_sha256', 'degraded'}:
        raise ValueError('unrecognized shared envelope fields')
    if data.get('query_sha256') != hashlib.sha256(args.query.encode()).hexdigest(): raise ValueError('query mismatch')
    payload = data.get('payload')
    if not isinstance(payload, dict) or set(payload) != {'index_sha256', 'results'} or payload.get('index_sha256') != hashlib.sha256(INDEX.read_bytes()).hexdigest():
        raise ValueError('index mismatch')
    if not set(args.domains) <= principal.domains or set(payload.get('results', {})) != set(args.domains):
        raise ValueError('domain mismatch')
    for domain, result in payload['results'].items():
        body = result['retrieval']
        validate_search(body, args.query, domain, principal)
        hits = [{**hit, 'evidence_kind': KIND, 'evidence_id': evidence_id(hit, body['corpus_id'])} for hit in body['hits']]
        if result != {'backend': body['backend'], 'hits': hits, 'retrieval': body}: raise ValueError('context/source mismatch')
    if data.get('payload_sha256') != checksum(payload): raise ValueError('shared payload hash mismatch')
    if type(data.get('degraded')) is not bool or data['degraded'] != any(r['retrieval']['degraded'] for r in payload['results'].values()):
        raise ValueError('hidden aggregate degradation')
    return data


class SharedPolicyGateway:
    server_version = SERVER_VERSION

    def __init__(self, principal, connection):
        self.principal, self.connection = principal, validate_connection(connection)

    async def execute(self, arguments):
        started = time.perf_counter()
        base = dict(trace_id=str(uuid4()), simulated=True, tool=NAME, tool_version=VERSION,
                    policy_version=POLICY, evidence_kind=KIND)
        def finish(error=None, **values):
            return {**base, 'ok': error is None, 'error': error,
                    'elapsed_ms': round((time.perf_counter()-started)*1000, 3), **values}
        try:
            args = PolicyArguments.model_validate(arguments)
            if not set(args.domains) <= self.principal.domains: return finish('FORBIDDEN')
            results = {}
            # Each domain is one <=800ms retrieval. MCP/HTTP/startup/model time is separate.
            # Sequential requests share API admission and breaker, including other graph sessions.
            for domain in args.domains:
                body = await asyncio.to_thread(fetch, self.connection,
                    {'query': args.query, 'domain': domain, 'top_k': HITS_PER_DOMAIN})
                validate_search(body, args.query, domain, self.principal)
                hits = [{**hit, 'evidence_kind': KIND, 'evidence_id': evidence_id(hit, body['corpus_id'])} for hit in body['hits']]
                results[domain] = {'backend': body['backend'], 'hits': hits, 'retrieval': body}
            payload = {'index_sha256': hashlib.sha256(INDEX.read_bytes()).hexdigest(), 'results': results}
            data = finish(status='context_only', payload=payload, payload_sha256=checksum(payload),
                query_sha256=hashlib.sha256(args.query.encode()).hexdigest(),
                degraded=any(r['retrieval']['degraded'] for r in results.values()))
            return validate_result(data, args, self.principal)
        except PolicyUnavailable as exc: return finish(exc.policy_error_code)
        except Exception: return finish('RETRIEVAL_UNAVAILABLE')

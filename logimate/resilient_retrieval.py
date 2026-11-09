"""P6.1 process-shared retrieval budget, explicit degradation and dependency breaker."""
import asyncio
from collections import deque
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from .circuit import CircuitBreaker
from .retrieval import ROOT, MODEL, KnowledgeSearch, cache_dir, eligible
from .resilient_worker import SearchArguments


class DependencyFailure(RuntimeError): pass
class IntegrityFailure(RuntimeError): pass
class SlotBusy(RuntimeError): pass


@dataclass(frozen=True)
class Budget:
    total: float = .8
    primary: float = .6
    fallback: float = .15
    reserve: float = .05

    def __post_init__(self):
        values = (self.total, self.primary, self.fallback, self.reserve)
        if any(not math.isfinite(v) or v <= 0 for v in values) or sum(values[1:]) > self.total + 1e-9:
            raise ValueError('invalid retrieval budget')


def worker_environment(role):
    allowed = {'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'TEMP', 'TMP', 'COMSPEC', 'HOME', 'USERPROFILE',
               'LOGIMATE_RETRIEVAL_CACHE', 'LOGIMATE_LEXICAL_CACHE'}
    if role == 'primary': allowed |= {'LOGIMATE_DENSE_BACKEND', 'LOGIMATE_MILVUS_URI', 'LOGIMATE_MILVUS_TOKEN'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env['LOGIMATE_LEXICAL_CACHE'] = str(Path(os.environ.get('LOGIMATE_LEXICAL_CACHE', str(cache_dir() / 'lexical'))) / role)
    env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    return env


class WorkerSlot:
    def __init__(self, role, *, command=None):
        self.role = role
        self.command = command or [sys.executable, '-m', 'logimate.resilient_worker', '--role', role]
        self.env = worker_environment(role)
        self.process = self.boot_task = None
        self.restart_handle = None
        self.ready = self.busy = self.closed = False
        self.error_kind = None
        self.generations = self.cancelled_jobs = 0

    def schedule_warm(self):
        if self.closed or self.ready or self.error_kind == 'contract': return
        if self.boot_task is None or self.boot_task.done():
            self.boot_task = asyncio.create_task(self._warm())

    def defer_warm(self):
        # One owned restart timer; let the isolated keyword budget finish first.
        if self.closed or self.restart_handle is not None: return
        def restart():
            self.restart_handle = None
            self.schedule_warm()
        self.restart_handle = asyncio.get_running_loop().call_later(.3, restart)

    async def _reset(self):
        process, self.process = self.process, None
        self.ready = False
        if process is not None:
            if process.returncode is None:
                try: process.kill()
                except ProcessLookupError: pass
            await process.wait()

    async def _warm(self):
        try:
            self.error_kind = None
            self.process = await asyncio.create_subprocess_exec(*self.command, cwd=ROOT,
                env=self.env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=131072,
                **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
            self.generations += 1
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout=15)
            if not line: raise DependencyFailure('worker exited during startup')
            try: data = json.loads(line)
            except ValueError: raise IntegrityFailure('invalid readiness JSON') from None
            if not isinstance(data, dict): raise IntegrityFailure('invalid readiness envelope')
            if data.get('type') == 'startup_error':
                if data.get('error_kind') == 'contract': raise IntegrityFailure('source/model integrity')
                raise DependencyFailure('worker initialization unavailable')
            if data != {'type': 'ready', 'role': self.role}: raise IntegrityFailure('invalid readiness')
            self.ready = True
        except IntegrityFailure: self.error_kind = 'contract'
        except asyncio.CancelledError: raise
        except Exception: self.error_kind = 'dependency'
        finally:
            if not self.ready: await self._reset()

    async def request(self, tenant, args):
        if not self.ready or self.busy or self.process is None: raise SlotBusy('no ready worker')
        self.busy = True
        process = self.process
        rid = str(uuid4())
        try:
            process.stdin.write((json.dumps({'request_id': rid, 'tenant_id': tenant,
                'arguments': args.model_dump()}) + '\n').encode())
            await process.stdin.drain()
            line = await process.stdout.readline()
            if not line: raise EOFError('worker exited')
            try: data = json.loads(line)
            except ValueError: raise IntegrityFailure('invalid worker JSON') from None
            if not isinstance(data, dict) or data.get('request_id') != rid: raise IntegrityFailure('stale worker response')
            if data.get('ok') is not True:
                if data.get('error_kind') == 'contract': raise IntegrityFailure('retrieval contract rejected')
                if data.get('error_kind') == 'dependency': raise DependencyFailure('retrieval dependency unavailable')
                raise IntegrityFailure('invalid worker status')
            return data['body']
        except asyncio.CancelledError:
            self.cancelled_jobs += 1
            await self._reset()  # Reap before returning cancellation to the deadline controller.
            self.defer_warm()
            raise
        except (OSError, EOFError, ConnectionError):
            await self._reset(); self.defer_warm()
            raise DependencyFailure('worker transport unavailable') from None
        except (KeyError, TypeError, ValueError):
            raise IntegrityFailure('invalid worker response') from None
        finally: self.busy = False

    async def close(self):
        self.closed = True
        if self.restart_handle is not None:
            self.restart_handle.cancel(); self.restart_handle = None
        if self.boot_task is not None and not self.boot_task.done():
            self.boot_task.cancel()
            await asyncio.gather(self.boot_task, return_exceptions=True)
        await self._reset()

    def snapshot(self):
        return {'ready': self.ready, 'busy': self.busy, 'warming': bool(self.restart_handle or (self.boot_task and not self.boot_task.done())),
                'error_kind': self.error_kind, 'generations': self.generations, 'cancelled_jobs': self.cancelled_jobs}


class Admission:
    def __init__(self, *, active_limit=4, per_tenant=30, global_rate=60, clock=time.monotonic):
        self.active_limit, self.per_tenant, self.global_rate, self.clock = active_limit, per_tenant, global_rate, clock
        self.active = 0
        self.global_times = deque()
        self.tenant_times = {name: deque() for name in ('demo-a', 'demo-b')}

    def enter(self, tenant):
        if tenant not in self.tenant_times: return 'forbidden'
        now = self.clock()
        for queue in [self.global_times, *self.tenant_times.values()]:
            while queue and queue[0] <= now - 60: queue.popleft()
        if self.active >= self.active_limit: return 'capacity'
        if len(self.global_times) >= self.global_rate or len(self.tenant_times[tenant]) >= self.per_tenant: return 'rate'
        self.global_times.append(now); self.tenant_times[tenant].append(now); self.active += 1
        return None

    def leave(self): self.active -= 1


@dataclass
class Outcome:
    status_code: int
    body: dict


class ResilientRetrieval:
    def __init__(self, *, primary=None, keyword=None, budget=None, breaker=None, admission=None):
        self.index = KnowledgeSearch(dense_backend=False).index
        self.backend = os.environ.get('LOGIMATE_DENSE_BACKEND', 'exact')
        if self.backend not in ('exact', 'milvus'): raise ValueError('unknown trusted backend')
        self.primary, self.keyword = primary or WorkerSlot('primary'), keyword or WorkerSlot('keyword')
        self.budget, self.breaker, self.admission = budget or Budget(), breaker or CircuitBreaker(), admission or Admission()
        self.tasks = set()
        index_id = hashlib.sha256(json.dumps(self.index, sort_keys=True).encode()).hexdigest()
        self.dependency_id = hashlib.sha256((self.backend + ':' + os.environ.get('LOGIMATE_MILVUS_URI', '') + ':' + index_id).encode()).hexdigest()
        self.integrity_failed = self.closed = False
        self.metrics = {key: 0 for key in ('requests', 'primary_success', 'primary_failure', 'primary_timeout',
            'fallback_success', 'fallback_failure', 'admission_rejected', 'caller_cancelled', 'integrity_failure', 'deadline_overrun',
            'responses_ok', 'responses_degraded', 'responses_unavailable')}

    async def start(self):
        self.primary.schedule_warm(); self.keyword.schedule_warm()

    async def close(self):
        self.closed = True
        self.primary.closed = self.keyword.closed = True
        for task in list(self.tasks): task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        await asyncio.gather(self.primary.close(), self.keyword.close())

    def _fatal(self):
        return self.integrity_failed or any(s.error_kind == 'contract' for s in (self.primary, self.keyword))

    def snapshot(self):
        return {'enabled': True, 'dependency_id': self.dependency_id, 'integrity_failed': self._fatal(), 'active_requests': self.admission.active,
                'budget_ms': round(self.budget.total * 1000), 'primary': self.primary.snapshot(),
                'keyword': self.keyword.snapshot(), 'circuit': self.breaker.snapshot(), 'metrics': dict(self.metrics)}

    def _validate(self, body, tenant, args, role):
        if not isinstance(body, dict) or body.get('simulated') is not True: raise IntegrityFailure('invalid evidence envelope')
        mode = 'hybrid' if role == 'primary' else 'keyword'
        expected = {'mode': mode, 'corpus_id': self.index['corpus_id'], 'corpus_sha256': self.index['corpus_sha256'],
                    'as_of': self.index['as_of'], 'rrf_k': 60,
                    'embedding_model': MODEL if role == 'primary' else None,
                    'backend': 'milvus-hnsw' if role == 'primary' and self.backend == 'milvus' else 'in-memory-exact'}
        if set(body) != {'simulated', 'corpus_id', 'corpus_sha256', 'as_of', 'backend', 'mode', 'rrf_k',
                         'embedding_model', 'hits', 'elapsed_ms', 'limitations'}:
            raise IntegrityFailure('unrecognized retrieval envelope fields')
        if any(body.get(k) != v for k, v in expected.items()): raise IntegrityFailure('version/backend mismatch')
        elapsed = body.get('elapsed_ms')
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
            raise IntegrityFailure('invalid worker timing')
        if not isinstance(body['limitations'], list) or any(not isinstance(v, str) for v in body['limitations']):
            raise IntegrityFailure('invalid scope limitations')
        pool = {c['chunk_id']: c for c in self.index['chunks'] if eligible(c, tenant, self.index['as_of'], args.domain)}
        hits = body.get('hits')
        if not isinstance(hits, list) or len(hits) > args.top_k: raise IntegrityFailure('invalid evidence count')
        if role == 'primary' and len(hits) != min(len(pool), args.top_k): raise IntegrityFailure('incomplete primary evidence')
        seen = set()
        for rank, hit in enumerate(hits, 1):
            if not isinstance(hit, dict) or not isinstance(hit.get('chunk_id'), str): raise IntegrityFailure('invalid hit')
            cid = hit['chunk_id']; ref = pool.get(cid)
            if ref is None or cid in seen or set(hit) != set(ref) | {'rank', 'score'}: raise IntegrityFailure('unauthorized/duplicate hit')
            if any(hit[k] != v or type(hit[k]) is not type(v) for k, v in ref.items()): raise IntegrityFailure('source fields mismatch')
            if type(hit['rank']) is not int or hit['rank'] != rank: raise IntegrityFailure('invalid rank')
            if isinstance(hit['score'], bool) or not isinstance(hit['score'], (int, float)) or not math.isfinite(hit['score']):
                raise IntegrityFailure('invalid score')
            seen.add(cid)
        return body

    async def search(self, arguments, tenant):
        args = arguments if isinstance(arguments, SearchArguments) else SearchArguments.model_validate(arguments)
        started = time.perf_counter(); deadline = started + self.budget.total
        trace = str(uuid4()); attempts = []; permit = None
        self.metrics['requests'] += 1
        def finish(code, *, body=None, reason=None):
            elapsed = (time.perf_counter() - started) * 1000
            overrun = elapsed > self.budget.total * 1000
            if overrun:
                self.metrics['deadline_overrun'] += 1
                code, body, reason = 503, None, 'deadline_exceeded'
            if self._fatal(): code, body, reason = 503, None, 'integrity_failure'
            degraded = body is not None and body['mode'] == 'keyword'
            if code == 200:
                self.metrics['responses_ok'] += 1
                if degraded: self.metrics['responses_degraded'] += 1
            elif code == 503: self.metrics['responses_unavailable'] += 1
            payload = dict(body or {})
            payload.update(simulated=True, trace_id=trace, status=('no_match' if not payload.get('hits') else 'ok') if code == 200 else
                'forbidden' if code == 403 else 'rate_limited' if code == 429 else 'unavailable',
                evidence_kind='synthetic_policy', requested_mode='hybrid', degraded=degraded,
                degradation_reason=reason, served_mode=payload.get('mode'), retrieval_elapsed_ms=round(elapsed, 3),
                retrieval_budget_ms=round(self.budget.total * 1000), deadline_overrun=overrun,
                attempts=attempts, circuit=self.breaker.snapshot())
            return Outcome(code, payload)

        rejection = self.admission.enter(tenant)
        if rejection:
            self.metrics['admission_rejected'] += 1
            return finish(403 if rejection == 'forbidden' else 429, reason='admission_' + rejection)
        task = asyncio.current_task(); self.tasks.add(task)
        try:
            if self.closed or self._fatal(): return finish(503, reason='integrity_failure' if self._fatal() else 'service_closed')
            reason = 'primary_not_ready'
            if self.primary.ready and not self.primary.busy:
                permit = self.breaker.acquire()
                if permit is None: reason = 'circuit_open'
                else:
                    began = time.perf_counter()
                    attempt = {'stage': 'primary', 'status': 'started'}; attempts.append(attempt)
                    try:
                        left = min(self.budget.primary, deadline - time.perf_counter() - self.budget.fallback - self.budget.reserve)
                        if left <= 0: raise TimeoutError
                        body = await asyncio.wait_for(self.primary.request(tenant, args), timeout=left)
                        body = self._validate(body, tenant, args, 'primary')
                        self.breaker.record(permit, success=True); permit = None
                        self.metrics['primary_success'] += 1; attempt['status'] = 'ok'
                        return finish(200, body=body)
                    except TimeoutError:
                        self.breaker.record(permit, success=False); permit = None
                        self.metrics['primary_timeout'] += 1; reason = 'primary_timeout'; attempt['status'] = 'timeout_cancelled'
                    except DependencyFailure:
                        self.breaker.record(permit, success=False); permit = None
                        self.metrics['primary_failure'] += 1; reason = 'primary_unavailable'; attempt['status'] = 'unavailable'
                    except SlotBusy:
                        self.breaker.abandon(permit); permit = None
                        reason = 'primary_busy'; attempt['status'] = 'busy'
                    finally: attempt['elapsed_ms'] = round((time.perf_counter() - began) * 1000, 3)
            elif self.primary.busy: reason = 'primary_busy'
            if not self.keyword.ready or self.keyword.busy: return finish(503, reason=reason + ':keyword_unavailable')
            left = min(self.budget.fallback, deadline - time.perf_counter() - self.budget.reserve)
            if left <= 0: return finish(503, reason='fallback_budget_exhausted')
            attempt = {'stage': 'keyword', 'status': 'started'}; attempts.append(attempt)
            began = time.perf_counter()
            try:
                body = await asyncio.wait_for(self.keyword.request(tenant, args), timeout=left)
                body = self._validate(body, tenant, args, 'keyword')
                self.metrics['fallback_success'] += 1; attempt['status'] = 'ok'
                return finish(200, body=body, reason=reason)
            except (TimeoutError, DependencyFailure, SlotBusy):
                self.metrics['fallback_failure'] += 1; attempt['status'] = 'unavailable'
                return finish(503, reason='both_paths_unavailable')
            finally: attempt['elapsed_ms'] = round((time.perf_counter() - began) * 1000, 3)
        except IntegrityFailure:
            self.integrity_failed = True; self.metrics['integrity_failure'] += 1
            if attempts and attempts[-1]['status'] == 'started': attempts[-1]['status'] = 'contract_rejected'
            if permit is not None: self.breaker.abandon(permit); permit = None
            return finish(503, reason='integrity_failure')
        except asyncio.CancelledError:
            self.metrics['caller_cancelled'] += 1
            if permit is not None: self.breaker.abandon(permit)
            raise
        finally:
            self.tasks.discard(task); self.admission.leave()

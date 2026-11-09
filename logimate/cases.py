"""P7.1 durable synthetic review cases. No business writes or graph-node recovery."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Literal
from uuid import uuid4

from pydantic import Field
from .models import Model
from .repository import Repository
from .rules import diagnose


class CreateCase(Model):
    order_id: str = Field(pattern=r'^SIM-[0-9]{3}$')
    trigger: Literal['auto', 'user_requested', 'high_risk'] = 'auto'


class ReviewCase(Model):
    expected_version: int = Field(ge=1, strict=True)
    action: Literal['confirm_review', 'reject_review', 'request_evidence']
    note: str = Field(default='', max_length=1000)


@dataclass(frozen=True)
class Actor:
    tenant: str
    subject: str
    reviewer: bool = False


class CaseError(RuntimeError):
    def __init__(self, status, code): self.status, self.code = status, code


def encoded(value): return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
def digest(value): return hashlib.sha256(encoded(value).encode()).hexdigest()
def now(): return datetime.now(timezone.utc).isoformat()


def request_key(key):
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', key):
        raise CaseError(422, 'INVALID_IDEMPOTENCY_KEY')
    return key


class CaseStore:
    def __init__(self, path):
        if str(path) == ':memory:': raise ValueError('durable case store requires a file')
        self.path = Path(path).resolve(); self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] not in (0, 1):
                raise CaseError(503, 'UNSUPPORTED_CASE_SCHEMA')
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    case_id TEXT NOT NULL REFERENCES cases(id), seq INTEGER NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(case_id,seq));
                CREATE TABLE IF NOT EXISTS requests (
                    tenant TEXT NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    response TEXT NOT NULL, response_digest TEXT NOT NULL, PRIMARY KEY(tenant,request_key));
                CREATE TABLE IF NOT EXISTS case_review_threads (
                    case_id TEXT PRIMARY KEY REFERENCES cases(id), thread_id TEXT UNIQUE NOT NULL);
                PRAGMA user_version=1;
            ''')

    @contextmanager
    def _connection(self, *, write=None):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=3, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON'); db.execute('PRAGMA synchronous=FULL')
            if write is not None: db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            if db.in_transaction: db.commit()
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            if db is not None and db.in_transaction: db.rollback()
            raise CaseError(503, 'CASE_STORAGE_UNAVAILABLE_OR_INVALID') from None
        except BaseException:
            if db is not None and db.in_transaction: db.rollback()
            raise
        finally:
            if db is not None: db.close()

    def _read(self, db, case_id, tenant):
        row = db.execute('SELECT * FROM cases WHERE id=? AND tenant=?', (case_id, tenant)).fetchone()
        if row is None: raise CaseError(404, 'CASE_NOT_FOUND')
        body = json.loads(row['body']); value_hash = digest(body)
        if value_hash != row['digest'] or body['case_id'] != case_id or body['tenant_id'] != tenant:
            raise CaseError(503, 'CASE_INTEGRITY_FAILURE')
        events = [json.loads(v['body']) for v in db.execute('SELECT body FROM events WHERE case_id=? ORDER BY seq', (case_id,))]
        previous = None
        for seq, event in enumerate(events, 1):
            fields = {k:v for k,v in event.items() if k != 'event_hash'}
            if (event['case_id'] != case_id or event['tenant_id'] != tenant or event['sequence'] != seq or event['previous_event_hash'] != previous or
                    digest(fields) != event['event_hash']): raise CaseError(503, 'CASE_AUDIT_INTEGRITY_FAILURE')
            previous = event['event_hash']
        if len(events) != body['version'] or not events or events[-1]['state_sha256'] != value_hash:
            raise CaseError(503, 'CASE_AUDIT_INTEGRITY_FAILURE')
        return body, events

    def _cached(self, db, actor, key, fingerprint):
        row = db.execute('SELECT * FROM requests WHERE tenant=? AND request_key=?', (actor.tenant, key)).fetchone()
        if row is None: return None
        if row['fingerprint'] != fingerprint: raise CaseError(409, 'IDEMPOTENCY_CONFLICT')
        response = json.loads(row['response'])
        if digest(response) != row['response_digest'] or response['case']['tenant_id'] != actor.tenant:
            raise CaseError(503, 'CASE_REQUEST_INTEGRITY_FAILURE')
        return {**response, 'replayed': True}

    def _remember(self, db, actor, key, fingerprint, response):
        db.execute('INSERT INTO requests VALUES (?,?,?,?,?)',
            (actor.tenant, key, fingerprint, encoded(response), digest(response)))

    def _event(self, db, body, actor, action, previous):
        event = {'case_id': body['case_id'], 'tenant_id':body['tenant_id'], 'sequence': body['version'], 'actor': actor.subject,
            'action': action, 'at': body['updated_at'], 'state': body['state'],
            'state_sha256': digest(body), 'previous_event_hash': previous, 'review':body.get('review')}
        event['event_hash'] = digest(event)
        db.execute('INSERT INTO events VALUES (?,?,?)', (body['case_id'], body['version'], encoded(event)))
        return event

    def get(self, case_id, tenant):
        with self._connection(write=False) as db:
            body, events = self._read(db, case_id, tenant)
            return {'simulated': True, 'case': body, 'events': events, 'replayed': False}

    def create(self, actor, arguments, key, build):
        request_key(key)
        fingerprint = digest({'operation':'create', 'actor':actor.subject, 'arguments':arguments})
        with self._connection(write=True) as db:
            cached = self._cached(db, actor, key, fingerprint)
            if cached is not None:
                self._read(db, cached['case']['case_id'], actor.tenant)
                return cached
            body = build()
            timestamp = now()
            body.update(case_id=str(uuid4()), tenant_id=actor.tenant, version=1,
                created_at=timestamp, updated_at=timestamp, review=None)
            db.execute('INSERT INTO cases VALUES (?,?,?,?)', (body['case_id'],actor.tenant,encoded(body),digest(body)))
            event = self._event(db, body, actor, 'created', None)
            response = {'simulated':True, 'case':body, 'events':[event], 'replayed':False}
            self._remember(db, actor, key, fingerprint, response)
            return response

    def decide(self, actor, case_id, arguments, key, *, workflow_id=None):
        if not actor.reviewer: raise CaseError(403, 'REVIEW_ROLE_REQUIRED')
        args = ReviewCase.model_validate(arguments); request_key(key)
        fingerprint = digest({'operation':'review', 'actor':actor.subject, 'case_id':case_id, 'arguments':args.model_dump()})
        with self._connection(write=True) as db:
            body, events = self._read(db, case_id, actor.tenant)  # Tenant authorization precedes idempotency lookup.
            bound = db.execute('SELECT thread_id FROM case_review_threads WHERE case_id=?',(case_id,)).fetchone()
            if bound is not None and bound['thread_id'] != workflow_id:
                raise CaseError(409, 'CASE_REQUIRES_NATIVE_REVIEW_THREAD')
            cached = self._cached(db, actor, key, fingerprint)
            if cached is not None: return cached
            if body['state'] != 'waiting_human': raise CaseError(409, 'CASE_NOT_WAITING')
            if body['version'] != args.expected_version: raise CaseError(409, 'STALE_CASE_VERSION')
            body['version'] += 1; body['updated_at'] = now()
            body['state'] = {'confirm_review':'reviewed','reject_review':'rejected','request_evidence':'waiting_human'}[args.action]
            body['review'] = {'actor':actor.subject, 'action':args.action, 'note':args.note}
            db.execute('UPDATE cases SET body=?,digest=? WHERE id=? AND tenant=?', (encoded(body),digest(body),case_id,actor.tenant))
            event = self._event(db, body, actor, args.action, events[-1]['event_hash'])
            response = {'simulated':True, 'case':body, 'events':events+[event], 'replayed':False}
            self._remember(db, actor, key, fingerprint, response)
            return response


class CaseService:
    def __init__(self, store, repository=None): self.store, self.repository = store, repository or Repository()

    def create(self, actor, arguments, key):
        args = CreateCase.model_validate(arguments)
        def build():
            repo = self.repository; snapshot = repo.get(args.order_id, actor.tenant)
            if snapshot is None: raise CaseError(404, 'ORDER_NOT_FOUND')
            diagnosis = diagnose(snapshot, as_of=repo.dataset.as_of, dataset_id=repo.dataset.dataset_id, intent='overall')
            reasons = []
            if args.trigger != 'auto': reasons.append(args.trigger)
            if diagnosis.needs_human: reasons.append('diagnosis_requires_human')
            if diagnosis.missing_domains: reasons.append('missing_evidence')
            if any(f.code=='RETURN_CONFLICT' for f in diagnosis.findings): reasons.append('state_conflict')
            return {'implementation':'durable-review-case-v1', 'simulated':True, 'order_id':args.order_id,
                'state':'waiting_human' if reasons else 'completed', 'reasons':reasons,
                'source':{'dataset_id':repo.dataset.dataset_id,'dataset_sha256':repo.sha256,'as_of':repo.dataset.as_of.isoformat()},
                'snapshot':snapshot.model_dump(mode='json'), 'diagnosis':diagnosis.model_dump(mode='json'),
                'limitations':['Synthetic read-only review workflow; review does not execute refunds or modify order facts.',
                    'SQLite case-state persistence; no native LangGraph interrupt/checkpointer or Redis/MySQL deployment yet.']}
        return self.store.create(actor, args.model_dump(), key, build)

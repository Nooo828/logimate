"""Persistent, isolated P6 retrieval role. Readiness is emitted only after warm-up."""
import argparse
import json
import sys
from typing import Literal

from pydantic import Field, field_validator
from .models import Model
from .milvus_backend import MilvusIntegrityError, MilvusUnavailable
from .retrieval import KnowledgeSearch, load_verified_encoder, normalized


class SearchArguments(Model):
    query: str = Field(min_length=1, max_length=500, strict=True)
    domain: Literal['order', 'logistics', 'warehouse', 'aftersales'] | None = None
    top_k: int = Field(default=5, ge=1, le=10, strict=True)

    @field_validator('query')
    @classmethod
    def valid_query(cls, value):
        value = value.strip()
        if not value: raise ValueError('empty query')
        value.encode('utf-8')
        return value


def emit(payload): print(json.dumps(payload, ensure_ascii=False, allow_nan=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', choices=['primary', 'keyword'], required=True)
    role = parser.parse_args().role
    try:
        service = KnowledgeSearch(dense_backend=False) if role == 'keyword' else KnowledgeSearch()
        # No remote database access during warm-up. Source/model integrity is checked here.
        service.search('预热规则', 'demo-a', mode='keyword')
        if role == 'primary':
            service.encoder = load_verified_encoder(service.index)
            normalized(list(service.encoder.query_embed(['预热检索']))[0])
        emit({'type': 'ready', 'role': role})
    except (ValueError, MilvusIntegrityError):
        emit({'type': 'startup_error', 'error_kind': 'contract'}); return 1
    except Exception:
        emit({'type': 'startup_error', 'error_kind': 'dependency'}); return 1
    for line in sys.stdin.buffer:
        if len(line) > 8192: return 2
        rid = None
        try:
            request = json.loads(line)
            rid = request['request_id']
            if request['tenant_id'] not in {'demo-a', 'demo-b'}: raise ValueError('invalid trusted tenant')
            args = SearchArguments.model_validate(request['arguments'])
            body = service.search(args.query, request['tenant_id'], domain=args.domain,
                                  top_k=args.top_k, mode='keyword' if role == 'keyword' else 'hybrid')
            emit({'request_id': rid, 'ok': True, 'body': body})
        except (ValueError, KeyError, TypeError, MilvusIntegrityError):
            emit({'request_id': rid, 'ok': False, 'error_kind': 'contract'})
        except (MilvusUnavailable, OSError, RuntimeError):
            emit({'request_id': rid, 'ok': False, 'error_kind': 'dependency'})
    return 0


if __name__ == '__main__': raise SystemExit(main())

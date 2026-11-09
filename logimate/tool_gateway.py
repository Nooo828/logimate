"""Read-only P4 boundary shared by local and MCP adapters; all data are synthetic."""
import asyncio
import hashlib
import json
import math
import random
import time
from dataclasses import dataclass
from collections import deque
from uuid import uuid4

from pydantic import Field, ValidationError

from .models import Model
from .repository import Repository
from .rules import DOMAINS

TOOL_VERSION = "read-v1"
POLICY_VERSION = "p4-read-policy-v1"
TOOLS = {f"read_{domain}": domain for domain in DOMAINS}


class ReadArguments(Model):
    order_id: str = Field(pattern=r"^SIM-[0-9]{3}$", strict=True)


@dataclass(frozen=True)
class Principal:
    """Trusted deployment context, never populated from model/tool arguments."""
    tenant_id: str
    domains: frozenset[str]

    def __post_init__(self):
        if self.tenant_id not in {"demo-a", "demo-b"} or not self.domains <= set(DOMAINS):
            raise ValueError("invalid synthetic deployment principal")


@dataclass(frozen=True)
class ReadPolicy:
    attempt_seconds: float = 0.5
    total_seconds: float = 2.0
    max_retries: int = 2
    jitter_seconds: float = 0.05

    def __post_init__(self):
        if (not isinstance(self.max_retries, int) or isinstance(self.max_retries, bool)
                or not 0 <= self.max_retries <= 2
                or any(not math.isfinite(v) or v <= 0 for v in (self.attempt_seconds, self.total_seconds))
                or not math.isfinite(self.jitter_seconds) or self.jitter_seconds < 0):
            raise ValueError("invalid bounded read policy")


class TransientReadError(Exception):
    """Adapter-classified transient transport/5xx failure; never schema or ACL errors."""


class ToolGateway:
    MAX_ACTIVE = 3
    MAX_REQUESTS_PER_MINUTE = 60

    def __init__(self, principal: Principal, repository=None, *, policy=None, backend=None):
        self.principal = principal
        self.repository = repository or Repository()
        self.policy = policy or ReadPolicy()
        self.backend = backend or self._fixture_read
        self._active = 0
        self._requests = deque()

    async def _fixture_read(self, order_id, tenant_id):
        # Bounded in-memory fixture only. Future I/O adapters must be async and cancellable.
        return self.repository.get(order_id, tenant_id)

    async def execute(self, name: str, arguments: dict) -> dict:
        # One event loop per gateway; reject excess work instead of building an unbounded queue.
        now = time.monotonic()
        while self._requests and self._requests[0] <= now - 60:
            self._requests.popleft()
        if self._active >= self.MAX_ACTIVE or len(self._requests) >= self.MAX_REQUESTS_PER_MINUTE:
            return await self._execute(name, arguments, admission_error="RATE_LIMITED")
        self._requests.append(now)
        self._active += 1
        try:
            return await self._execute(name, arguments)
        finally:
            self._active -= 1

    async def _execute(self, name: str, arguments: dict, admission_error=None) -> dict:
        started = time.monotonic()
        result = {"trace_id": str(uuid4()), "simulated": True,
                  "dataset_id": self.repository.dataset.dataset_id,
                  "dataset_sha256": self.repository.sha256,
                  "tool": name if name in TOOLS else "unknown",
                  "tool_version": TOOL_VERSION, "policy_version": POLICY_VERSION, "attempts": []}

        def finish(*, error=None, **values):
            return dict(result, ok=error is None, error=error,
                        elapsed_ms=round((time.monotonic() - started) * 1000, 3), **values)

        if admission_error:
            return finish(error=admission_error)
        if name not in TOOLS:
            return finish(error="UNKNOWN_TOOL")
        domain = TOOLS[name]
        if domain not in self.principal.domains:
            return finish(error="FORBIDDEN")
        try:
            args = ReadArguments.model_validate(arguments)
        except ValidationError:
            return finish(error="INVALID_ARGUMENTS")
        result["order_id"] = args.order_id

        async def read_with_retries():
            for attempt in range(1, self.policy.max_retries + 2):
                audit = {"number": attempt, "status": "running"}
                result["attempts"].append(audit)
                try:
                    async with asyncio.timeout(self.policy.attempt_seconds):
                        snapshot = await self.backend(args.order_id, self.principal.tenant_id)
                    audit["status"] = "ok"
                    return snapshot
                except (TimeoutError, TransientReadError) as exc:
                    audit["status"] = "timeout" if isinstance(exc, TimeoutError) else "transient"
                    if attempt > self.policy.max_retries:
                        raise
                    delay = random.uniform(0, self.policy.jitter_seconds)
                    audit["retry_delay_ms"] = round(delay * 1000, 3)
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    audit["status"] = "cancelled"
                    raise
                except Exception:
                    audit["status"] = "permanent_error"
                    raise

        try:
            async with asyncio.timeout(self.policy.total_seconds):
                snapshot = await read_with_retries()
        except TimeoutError:
            return finish(error="TIMEOUT")
        except TransientReadError:
            return finish(error="TEMPORARILY_UNAVAILABLE")
        except Exception:
            # Do not expose adapter exception messages, credentials or backend internals.
            return finish(error="BACKEND_ERROR")
        # Defense in depth: a faulty external adapter cannot bypass tenant/order scoping.
        if snapshot is not None and (snapshot.order.tenant_id != self.principal.tenant_id
                                     or snapshot.order.order_id != args.order_id):
            return finish(error="IDENTITY_MISMATCH")
        if snapshot is None:
            return finish(status="not_found", record=None, evidence=None)
        record = getattr(snapshot, domain)
        if record is None:
            return finish(status="missing", record=None, evidence=None)
        if record.tenant_id != self.principal.tenant_id or record.order_id != args.order_id:
            return finish(error="IDENTITY_MISMATCH")
        payload = record.model_dump(mode="json")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        evidence = {"evidence_id": f"{result['dataset_id']}:{domain}:{record.record_id}:{digest[:12]}",
                    "payload_sha256": digest, "record_id": record.record_id, "domain": domain}
        return finish(status="found", record=payload, evidence=evidence)

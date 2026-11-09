"""P4.2 graph tool adapters. One MCP session/process per graph request, no fallback."""
import asyncio
import concurrent.futures
import hashlib
import json
import threading
import time

from mcp.shared.exceptions import MCPError

from .deepseek import ModelUnavailable
from .mcp_client import connect
from .models import Order, Logistics, Warehouse, Aftersales
from .rules import DOMAINS
from .tool_gateway import POLICY_VERSION, TOOL_VERSION, Principal, ReadArguments, ToolGateway, TOOLS

RECORD_TYPES = dict(zip(DOMAINS, (Order, Logistics, Warehouse, Aftersales)))


def validate_result(data, *, domain, order_id, principal, repository):
    """Treat protocol data as untrusted, including claimed identities and evidence hashes."""
    if not isinstance(data, dict) or data.get("simulated") is not True:
        raise ValueError("invalid MCP tool envelope")
    expected = {"tool": "read_" + domain,
                "dataset_id": repository.dataset.dataset_id, "dataset_sha256": repository.sha256,
                "tool_version": TOOL_VERSION, "policy_version": POLICY_VERSION}
    if any(data.get(k) != v for k, v in expected.items()):
        raise ValueError("tool response version or identity mismatch")
    if data.get("ok") is not True:
        if data.get("error") in {"TIMEOUT", "TEMPORARILY_UNAVAILABLE", "BACKEND_ERROR", "RATE_LIMITED"}:
            raise ModelUnavailable("Read tool unavailable; no local fallback", category="tool_unavailable")
        raise ValueError("read tool rejected request")
    if data.get("order_id") != order_id:
        raise ValueError("tool response order mismatch")
    if data.get("error") is not None or data.get("status") not in {"found", "missing", "not_found"}:
        raise ValueError("invalid tool status")
    if data["status"] != "found":
        if data.get("record") is not None or data.get("evidence") is not None or (
                domain == "order" and data["status"] == "missing"):
            raise ValueError("invalid absent record")
        return data
    record = RECORD_TYPES[domain].model_validate(data.get("record"))
    if record.tenant_id != principal.tenant_id or record.order_id != order_id:
        raise ValueError("record identity mismatch")
    payload = record.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    evidence = {"evidence_id": f"{repository.dataset.dataset_id}:{domain}:{record.record_id}:{digest[:12]}",
                "payload_sha256": digest, "record_id": record.record_id, "domain": domain}
    if data.get("evidence") != evidence:
        raise ValueError("tool evidence hash mismatch")
    return {**data, "record": payload}


class LocalToolReader:
    transport = "local-gateway"

    def __init__(self, tenant_id, repository, *, knowledge=False):
        self.principal = Principal(tenant_id, frozenset(DOMAINS))
        self.repository = repository
        self.gateway = ToolGateway(self.principal, repository)
        self.lock = threading.Lock()
        self.startup_ms = 0
        self.policy_gateway = None
        if knowledge:
            from .policy_tools import PolicyGateway
            self.policy_gateway = PolicyGateway(self.principal)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, domain, order_id, timeout):
        # Local fixture reads are synchronous/bounded; lock protects per-gateway rate counters.
        with self.lock:
            data = asyncio.run(self.gateway.execute("read_" + domain, {"order_id": order_id}))
        return validate_result(data, domain=domain, order_id=order_id,
                               principal=self.principal, repository=self.repository)

    def search_policies(self, query, domains, timeout):
        from .policy_tools import PolicyArguments, validate_result as validate_policy
        if self.policy_gateway is None: raise ValueError('policy capability not enabled')
        args = PolicyArguments(query=query, domains=domains)
        with self.lock:
            async def bounded():
                return await asyncio.wait_for(self.policy_gateway.execute(args.model_dump()), timeout=timeout)
            try: data = asyncio.run(bounded())
            except TimeoutError:
                raise ModelUnavailable('Policy context time budget exhausted', category='tool_unavailable') from None
        return validate_policy(data, args, self.principal)


class MCPToolReader:
    transport = "mcp-stdio"

    def __init__(self, tenant_id, repository, *, startup_timeout=8, knowledge=False, shared_retrieval=None):
        self.principal = Principal(tenant_id, frozenset(DOMAINS))
        self.repository = repository
        self.startup_timeout = startup_timeout
        self.knowledge = knowledge
        self.shared_retrieval = shared_retrieval
        self.ready = threading.Event()
        self.error = None
        self.loop = self.session = self.stop = None
        self.thread = threading.Thread(target=self._worker, daemon=True, name="logimate-mcp-session")

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        self.stop = asyncio.Event()
        try:
            async with connect(self.principal, knowledge=self.knowledge, shared_retrieval=self.shared_retrieval) as (session, initialized):
                if initialized.server_info.version != ('0.6.0' if self.shared_retrieval is not None else "0.5.0" if self.knowledge else "0.4.0"):
                    raise ValueError("unsupported server version")
                listed = await session.list_tools()
                expected = {name: ReadArguments.model_json_schema() for name in TOOLS}
                if self.knowledge:
                    from .policy_tools import NAME, PolicyArguments
                    expected[NAME] = PolicyArguments.model_json_schema()
                if {t.name for t in listed.tools} != set(expected) or any(
                        t.input_schema != expected[t.name] for t in listed.tools):
                    raise ValueError("unexpected MCP tool schema")
                self.session = session
                self.protocol_version = initialized.protocol_version
                self.ready.set()
                await self.stop.wait()
        except Exception:
            # SDK errors can include remote data; expose no raw exception text.
            self.error = "MCP session unavailable or incompatible"
        finally:
            self.ready.set()

    def _worker(self):
        asyncio.run(self._serve())

    def __enter__(self):
        start = time.monotonic()
        self.thread.start()
        if not self.ready.wait(self.startup_timeout) or self.error or self.session is None:
            self.close()
            raise ModelUnavailable("MCP startup failed; no local fallback", category="mcp_startup")
        self.startup_ms = round((time.monotonic() - start) * 1000, 3)
        return self

    def close(self):
        if self.loop and self.stop and self.loop.is_running():
            try:
                self.loop.call_soon_threadsafe(self.stop.set)
            except RuntimeError:
                pass  # The worker may already have completed/closed the loop.
        if self.thread.is_alive():
            self.thread.join(timeout=self.startup_timeout + 2)
        if self.thread.is_alive():
            raise ModelUnavailable("MCP shutdown did not complete", category="mcp_shutdown")

    def __exit__(self, *args):
        self.close()

    def read(self, domain, order_id, timeout):
        if domain not in DOMAINS or self.error or not self.thread.is_alive():
            raise ModelUnavailable("MCP session unavailable; no local fallback", category="mcp_transport")
        data = self._request('read_' + domain, {'order_id': order_id}, timeout, 5)
        return validate_result(data, domain=domain, order_id=order_id,
                               principal=self.principal, repository=self.repository)

    def search_policies(self, query, domains, timeout):
        from .policy_tools import NAME, PolicyArguments, validate_result as validate_policy
        if not self.knowledge: raise ValueError('policy capability not enabled')
        if self.shared_retrieval is not None:
            from .shared_policy import validate_result as validate_policy
        args = PolicyArguments(query=query, domains=domains)
        data = self._request(NAME, args.model_dump(), timeout, 12)
        return validate_policy(data, args, self.principal)

    def _request(self, name, arguments, timeout, limit):
        if self.error or not self.thread.is_alive():
            raise ModelUnavailable('MCP session unavailable; no fallback', category='mcp_transport')
        future = asyncio.run_coroutine_threadsafe(self.session.call_tool(
            name, arguments, read_timeout_seconds=min(limit, timeout)), self.loop)
        try:
            remote = future.result(timeout=min(limit, timeout))
        except MCPError as exc:
            if exc.code == -32602:
                raise ValueError("MCP rejected tool contract") from None
            raise ModelUnavailable("MCP request failed; no local fallback", category="mcp_transport") from None
        except Exception:
            future.cancel()
            raise ModelUnavailable("MCP transport failed or timed out; no local fallback", category="mcp_transport") from None
        data = remote.structured_content
        if remote.is_error != (isinstance(data, dict) and data.get("ok") is False):
            raise ValueError("inconsistent MCP error envelope")
        return data

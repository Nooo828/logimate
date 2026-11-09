"""Real stdio MCP acceptance, no model or credentials required; all records synthetic."""
import asyncio
import hashlib
import importlib.metadata
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from mcp.shared.exceptions import MCPError
from mcp.client.session import HANDSHAKE_PROTOCOL_VERSIONS

from logimate.mcp_client import connect, server_parameters
from logimate.repository import Repository
from logimate.rules import DOMAINS, diagnose
from logimate.tool_gateway import Principal, ToolGateway, TOOLS

ROOT = Path(__file__).resolve().parent.parent


async def version_negotiation_check(principal):
    params = server_parameters(principal)
    process = await asyncio.create_subprocess_exec(params.command, *params.args, cwd=params.cwd, env=params.env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "unsupported-test-version", "capabilities": {},
        "clientInfo": {"name": "logimate-version-negative-test", "version": "0.4.0"}}}
    try:
        process.stdin.write((json.dumps(request) + "\n").encode())
        await process.stdin.drain()
        reply = json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=5))
        negotiated = reply["result"]["protocolVersion"]
        assert negotiated in HANDSHAKE_PROTOCOL_VERSIONS
        assert negotiated != request["params"]["protocolVersion"]
        return {"operation": "initialize:unsupported-version", "request": request, "reply": reply}
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


async def protocol_checks():
    checks, events = [], []
    principal = Principal("demo-a", frozenset(DOMAINS))
    gateway = ToolGateway(principal)
    events.append(await version_negotiation_check(principal))
    checks.append("unsupported handshake version negotiates a supported version")
    async with connect(principal) as (session, initialized):
        events.append({"operation": "initialize+notifications/initialized", "result":
                       initialized.model_dump(mode="json", by_alias=True, exclude_none=True)})
        listed = await session.list_tools()
        assert {t.name for t in listed.tools} == set(TOOLS)
        assert all(t.input_schema["additionalProperties"] is False for t in listed.tools)
        assert all("tenant_id" not in t.input_schema["properties"] for t in listed.tools)
        events.append({"operation": "tools/list", "result": listed.model_dump(mode="json", by_alias=True, exclude_none=True)})
        checks.append("four read-only tools and closed schema")
        ref = diagnose(Repository().get("SIM-002", "demo-a"), as_of=gateway.repository.dataset.as_of,
                       dataset_id="synthetic-v1", intent="refund_delay")
        expected = {e.domain: e for e in ref.evidence}
        for name, domain in TOOLS.items():
            remote = await session.call_tool(name, {"order_id": "SIM-002"})
            data = remote.structured_content
            local = await gateway.execute(name, {"order_id": "SIM-002"})
            assert not remote.is_error and data["ok"] and data["status"] == "found"
            for key in ("record", "evidence", "dataset_sha256", "tool_version", "policy_version"):
                assert data[key] == local[key], (name, key)
            assert data["evidence"]["evidence_id"] == expected[domain].evidence_id
            assert data["evidence"]["payload_sha256"] == expected[domain].payload_sha256
            events.append({"operation": "tools/call", "result": data})
            checks.append(f"{name}: MCP/local/P1 evidence equality")
        for name, args, status, error in [
            ("read_order", {"order_id": "SIM-009"}, "not_found", None),
            ("read_order", {"order_id": "SIM-999"}, "not_found", None),
            ("read_aftersales", {"order_id": "SIM-005"}, "missing", None),
            ("read_order", {"order_id": "SIM-002", "tenant_id": "demo-b"}, None, "INVALID_ARGUMENTS"),
            ("read_order", {"order_id": "SIM-002", "version": "read-v0"}, None, "INVALID_ARGUMENTS"),
            ("read_order", {"order_id": "../../data"}, None, "INVALID_ARGUMENTS"),
            ("read_order", {}, None, "INVALID_ARGUMENTS")]:
            remote = await session.call_tool(name, args)
            data = remote.structured_content
            assert data["error"] == error and data.get("status") == status
            assert remote.is_error == (error is not None)
            assert not data.get("record")
            events.append({"operation": "tools/call", "result": data})
            checks.append(f"{name}:{status or error}:{len(checks)}")
        try:
            await session.call_tool("execute_refund", {"order_id": "SIM-002"})
        except MCPError as exc:
            assert exc.code == -32602
            events.append({"operation": "tools/call:unknown", "protocol_error_code": exc.code})
            checks.append("unknown tool rejected with JSON-RPC -32602")
        else:
            raise AssertionError("unknown tool accepted")
    async with connect(Principal("demo-a", frozenset({"order"}))) as (session, _):
        listed = await session.list_tools()
        assert [t.name for t in listed.tools] == ["read_order"]
        denied = await session.call_tool("read_warehouse", {"order_id": "SIM-002"})
        assert denied.is_error and denied.structured_content["error"] == "FORBIDDEN"
        assert denied.structured_content["attempts"] == []
        events.append({"operation": "tools/call:restricted-principal", "result": denied.structured_content})
        checks.append("domain ACL denies hidden tool before backend")
    async with connect(Principal("demo-b", frozenset({"order"}))) as (session, _):
        own = await session.call_tool("read_order", {"order_id": "SIM-009"})
        other = await session.call_tool("read_order", {"order_id": "SIM-002"})
        assert own.structured_content["record"]["tenant_id"] == "demo-b"
        assert other.structured_content["status"] == "not_found"
        events.extend([{"operation": "tools/call:demo-b", "result": r.structured_content} for r in (own, other)])
        checks.append("independent deployment identity filters data")
    return {"passed": True, "checks": checks, "protocol_events": events,
            "mcp_version": importlib.metadata.version("mcp"), "simulated": True}


def main():
    # This switch enables container exec without writing to the non-root image filesystem.
    if "--stdout" in sys.argv:
        print(json.dumps(asyncio.run(protocol_checks()), ensure_ascii=True))
        return 0
    out = ROOT / "docs/evidence/p4" / datetime.now(timezone.utc).strftime("mcp-%Y%m%dT%H%M%S%f")
    out.mkdir(parents=True)
    try:
        result = asyncio.run(protocol_checks())
    except Exception as exc:
        result = {"passed": False, "error_type": type(exc).__name__}
        raise
    finally:
        result["created_at_utc"] = datetime.now(timezone.utc).isoformat()
        paths = [*ROOT.glob("logimate/*.py"), *ROOT.glob("scripts/*mcp*.py"),
                 ROOT / "requirements.lock", ROOT / "data/synthetic_v1.json"]
        result["input_sha256"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        (out / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Evidence: {out}")
    print(f"MCP stdio: {len(result['checks'])} checks passed; SDK {result['mcp_version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import asyncio
import time

import pytest

from logimate.mcp_client import server_parameters
from logimate.repository import Repository
from logimate.rules import DOMAINS
from logimate.tool_gateway import Principal, ReadPolicy, ToolGateway, TransientReadError
from scripts.verify_mcp import protocol_checks

PRINCIPAL = Principal("demo-a", frozenset(DOMAINS))


def run(coro):
    return asyncio.run(coro)


def test_real_stdio_roundtrip_and_isolation():
    result = run(protocol_checks())
    assert result["passed"] and len(result["checks"]) == 16


@pytest.mark.parametrize("name,args,error", [
    ("write_order", {"order_id": "SIM-002"}, "UNKNOWN_TOOL"),
    ("read_order", {"order_id": "SIM-002", "tenant_id": "demo-b"}, "INVALID_ARGUMENTS"),
    ("read_order", {"order_id": 2}, "INVALID_ARGUMENTS"),
    ("read_order", {"order_id": "SIM-002", "version": "read-v0"}, "INVALID_ARGUMENTS")])
def test_invalid_request_never_reaches_backend(name, args, error):
    async def forbidden_backend(*args):
        pytest.fail("backend reached")
    r = run(ToolGateway(PRINCIPAL, backend=forbidden_backend).execute(name, args))
    assert r["error"] == error and r["attempts"] == []


def test_acl_checked_before_data_read():
    r = run(ToolGateway(Principal("demo-a", frozenset({"order"}))).execute(
        "read_warehouse", {"order_id": "SIM-002"}))
    assert r["error"] == "FORBIDDEN" and not r["attempts"]


def test_transient_recovers_at_third_attempt():
    calls = []
    async def backend(order_id, tenant_id):
        calls.append((order_id, tenant_id))
        if len(calls) < 3:
            raise TransientReadError("explicit injected 503")
        return Repository().get(order_id, tenant_id)
    r = run(ToolGateway(PRINCIPAL, backend=backend).execute("read_order", {"order_id": "SIM-002"}))
    assert r["ok"] and len(calls) == 3
    assert [a["status"] for a in r["attempts"]] == ["transient", "transient", "ok"]
    assert all(0 <= a["retry_delay_ms"] <= 50 for a in r["attempts"][:-1])


def test_transient_exhaustion_never_more_than_two_retries():
    calls = []
    async def backend(*args):
        calls.append(1)
        raise TransientReadError("private provider details")
    r = run(ToolGateway(PRINCIPAL, backend=backend).execute("read_order", {"order_id": "SIM-002"}))
    assert len(calls) == 3 and r["error"] == "TEMPORARILY_UNAVAILABLE"
    assert "private" not in str(r)


def test_timeout_cancels_each_attempt_and_no_background_work():
    started, stopped = [], []
    async def slow(*args):
        started.append(1)
        try:
            await asyncio.sleep(5)
        finally:
            stopped.append(1)
    policy = ReadPolicy(attempt_seconds=.01, total_seconds=.5, jitter_seconds=.001)
    r = run(ToolGateway(PRINCIPAL, policy=policy, backend=slow).execute("read_order", {"order_id": "SIM-002"}))
    assert r["error"] == "TIMEOUT" and len(started) == len(stopped) == 3


def test_total_budget_includes_retry_sleep_and_cancels_active_call():
    cancelled = []
    async def slow(*args):
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.append(True)
    policy = ReadPolicy(attempt_seconds=.3, total_seconds=.03, jitter_seconds=.01)
    start = time.monotonic()
    r = run(ToolGateway(PRINCIPAL, policy=policy, backend=slow).execute("read_order", {"order_id": "SIM-002"}))
    assert r["error"] == "TIMEOUT" and len(cancelled) == 1
    assert time.monotonic() - start < .5


def test_total_budget_cuts_off_backoff_before_second_attempt(monkeypatch):
    calls = []
    async def transient(*args):
        calls.append(1)
        raise TransientReadError("explicit injected transient failure")
    monkeypatch.setattr("logimate.tool_gateway.random.uniform", lambda *args: 3.0)
    policy = ReadPolicy(attempt_seconds=.1, total_seconds=.03, jitter_seconds=5)
    start = time.monotonic()
    r = run(ToolGateway(PRINCIPAL, policy=policy, backend=transient).execute(
        "read_order", {"order_id": "SIM-002"}))
    assert r["error"] == "TIMEOUT" and len(calls) == 1
    assert time.monotonic() - start < .5


def test_permanent_error_never_retried_or_echoed():
    calls = []
    async def broken(*args):
        calls.append(1)
        raise ValueError("sensitive upstream details")
    r = run(ToolGateway(PRINCIPAL, backend=broken).execute("read_order", {"order_id": "SIM-002"}))
    assert r["error"] == "BACKEND_ERROR" and len(calls) == 1
    assert "sensitive" not in str(r)


def test_faulty_adapter_cannot_return_other_identity():
    async def wrong(*args):
        return Repository().get("SIM-009", "demo-b")
    r = run(ToolGateway(PRINCIPAL, backend=wrong).execute("read_order", {"order_id": "SIM-002"}))
    assert r["error"] == "IDENTITY_MISMATCH" and not r.get("record")


def test_child_process_does_not_inherit_model_or_telemetry_secret(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "explicit-test-secret")
    monkeypatch.setenv("LANGSMITH_API_KEY", "explicit-test-secret")
    params = server_parameters(PRINCIPAL)
    assert "DEEPSEEK_API_KEY" not in params.env and "LANGSMITH_API_KEY" not in params.env
    assert params.env["LOGIMATE_MCP_TENANT"] == "demo-a"


def test_no_implicit_or_unrecognized_tenant_identity():
    with pytest.raises(ValueError):
        Principal("", frozenset({"order"}))
    with pytest.raises(ValueError):
        Principal("demo-a", frozenset({"finance"}))


def test_admission_limit_rejects_without_queueing_or_backend_read():
    async def scenario():
        ready, release = asyncio.Event(), asyncio.Event()
        async def backend(*args):
            ready.set()
            await release.wait()
            return None
        gateway = ToolGateway(PRINCIPAL, backend=backend)
        gateway.MAX_ACTIVE = 1
        task = asyncio.create_task(gateway.execute("read_order", {"order_id": "SIM-002"}))
        await ready.wait()
        denied = await gateway.execute("read_order", {"order_id": "SIM-002"})
        assert denied["error"] == "RATE_LIMITED" and denied["attempts"] == []
        release.set()
        await task
        assert gateway._active == 0
        gateway.MAX_REQUESTS_PER_MINUTE = 1
        assert (await gateway.execute("read_order", {"order_id": "SIM-002"}))["error"] == "RATE_LIMITED"
    run(scenario())


@pytest.mark.parametrize("kwargs", [{"max_retries": 3}, {"total_seconds": -1},
                                   {"attempt_seconds": float("nan")}])
def test_unbounded_policy_rejected(kwargs):
    with pytest.raises(ValueError):
        ReadPolicy(**kwargs)

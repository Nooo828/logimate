import json

import pytest

from logimate.deepseek import DeepSeek, ModelUnavailable
from logimate.single_agent import route_query, run_agent


class Fake:
    """Explicit test double; never counted as a live model observation."""
    def __init__(self, *responses):
        self.responses = iter(responses)
    def complete(self, *args, **kwargs):
        return next(self.responses), {"usage": {"total_tokens": 10}, "model": "test-double"}


def route(action="read", ids=None, domains=None):
    return {"content": json.dumps({"action": action, "order_ids": ids if ids is not None else ["SIM-002"],
                                   "domains": domains if domains is not None else ["order", "logistics", "warehouse", "aftersales"]})}


def tools(names, oid="SIM-002", extra=None):
    return {"content": None, "tool_calls": [{"id": str(i), "type": "function", "function": {
        "name": n, "arguments": json.dumps({"order_id": oid, **(extra or {})})}} for i,n in enumerate(names)]}


def test_real_credentials_required(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ModelUnavailable, match="not configured"):
        DeepSeek()


def test_cross_domain_loop_and_evidence():
    r = run_agent(Fake(route(), tools(["read_order", "read_logistics", "read_warehouse", "read_aftersales"])), "核对SIM-002", "demo-a")
    assert r["status"] == "answered"
    assert len(r["tool_calls"]) == 4
    assert {f["code"] for f in r["diagnosis"]["findings"]} == {"REFUND_OVERDUE", "FINANCE_FAILURE_RECORDED"}
    assert r["usage"]["total_tokens"] == 20


@pytest.mark.parametrize("bad", [tools(["execute_refund"]), tools(["read_order"], extra={"tenant_id": "demo-b"}),
                                tools(["read_order"], oid="SIM-009")])
def test_unauthorized_plan_stops_before_read(bad):
    r = run_agent(Fake(route(), bad), "核对SIM-002", "demo-a")
    assert r["status"] == "needs_human"
    assert r["tool_calls"] == []


def test_repeat_call_terminates():
    r = run_agent(Fake(route(), tools(["read_order"]), tools(["read_order"])), "核对SIM-002", "demo-a")
    assert r["status"] == "needs_human"
    assert len(r["tool_calls"]) == 1


def test_other_tenant_no_records():
    r = run_agent(Fake(route(ids=["SIM-009"], domains=["order"]), tools(["read_order"], oid="SIM-009")), "查SIM-009", "demo-a")
    assert r["status"] == "not_found"
    assert r["records"] == {}


@pytest.mark.parametrize("action", ["clarify", "human", "refuse"])
def test_non_read_does_not_call_tools(action):
    r = run_agent(Fake(route(action=action, domains=[])), "SIM-002", "demo-a")
    assert r["status"] == action
    assert not r["tool_calls"]


def test_slot_hallucination_rejected():
    with pytest.raises(ValueError, match="explicit query"):
        route_query(Fake(route(ids=["SIM-008"])), "查SIM-002")


def test_round_limit_with_partial_data():
    r = run_agent(Fake(route(), tools(["read_order"])), "SIM-002", "demo-a", max_rounds=1)
    assert r["status"] == "needs_human"
    assert len(r["tool_calls"]) == 1


def test_unavailable_never_returns_mock_answer():
    class Down:
        def complete(self, *a, **kw):
            raise ModelUnavailable("DeepSeek HTTP 401")
    r = run_agent(Down(), "SIM-002", "demo-a")
    assert r["status"] == "unavailable"
    assert r["diagnosis"] is None
    assert not r["tool_calls"]


def test_agent_http_requires_key_and_rejects_extra_tenant(monkeypatch):
    from fastapi.testclient import TestClient
    from logimate.api import create_app
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with TestClient(create_app(token="test-agent")) as client:
        assert client.post("/v1/agent/queries", json={"query": "SIM-002"}).status_code == 401
        client.headers["Authorization"] = "Bearer test-agent"
        missing = client.post("/v1/agent/queries", json={"query": "SIM-002"})
        assert missing.status_code == 503
        assert client.post("/v1/agent/queries", json={"query": "SIM-002", "tenant_id": "demo-b"}).status_code == 422

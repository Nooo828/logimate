import json
import threading

import pytest

from logimate.deepseek import ModelUnavailable
from logimate.five_agent import merge_results, run_five_agent


class FakeProvider:
    def __init__(self, fail=None, malicious=None, action="read"):
        self.fail, self.malicious, self.action = fail, malicious, action
    def complete(self, messages, tools=None, **kwargs):
        if tools is None:
            ids = ["SIM-009"] if "SIM-009" in messages[-1]["content"] else ["SIM-002"]
            msg = {"content": json.dumps({"action": self.action, "order_ids": ids,
                    "domains": ["order", "logistics", "warehouse", "aftersales"] if self.action=="read" else []})}
        else:
            name = tools[0]["function"]["name"]
            if name == self.fail:
                raise ModelUnavailable("test failure")
            oid = json.loads(messages[-1]["content"])["order_id"]
            msg = {"tool_calls": [{"function": {"name": self.malicious or name,
                                                  "arguments": json.dumps({"order_id": oid})}}]}
        return msg, {"model": "explicit-test-double", "usage": {"total_tokens": 1}}


def test_fanout_merges_all_domain_summaries():
    r = run_five_agent(FakeProvider(), "核对SIM-002", "demo-a")
    assert r["status"] == "answered"
    assert set(r["domain_results"]) == {"order", "logistics", "warehouse", "aftersales"}
    assert len(r["tool_calls"]) == 4 and len(r["provider_calls"]) == 5
    assert r["events"].count("aggregate") == 1
    assert r["events"].index("order") < r["events"].index("logistics")
    assert len(r["diagnosis"]["evidence"]) == 4


def test_failure_does_not_erase_successful_domains():
    r = run_five_agent(FakeProvider(fail="read_logistics"), "SIM-002", "demo-a")
    assert r["status"] == "unavailable"
    assert set(r["records"]) == {"order", "warehouse", "aftersales"}
    assert r["diagnosis"] is None


def test_expert_cannot_call_another_domains_tool():
    r = run_five_agent(FakeProvider(malicious="execute_refund"), "SIM-002", "demo-a")
    assert r["status"] == "needs_human" and not r["records"]


def test_cross_tenant_stops_after_order_dependency():
    r = run_five_agent(FakeProvider(), "SIM-009", "demo-a")
    assert r["status"] == "not_found" and not r["records"]
    assert len(r["provider_calls"]) == 2


def test_non_read_route_has_no_expert():
    r = run_five_agent(FakeProvider(action="human"), "SIM-002", "demo-a")
    assert r["status"] == "human" and not r["tool_calls"]


def test_recursion_budget_stops_safely():
    r = run_five_agent(FakeProvider(), "SIM-002", "demo-a", recursion_limit=1)
    assert r["status"] == "needs_human"


def test_duplicate_domain_reducer_rejects_overwrite():
    with pytest.raises(ValueError, match="duplicate"):
        merge_results({"order": {}}, {"order": {"status": "wrong"}})


def test_three_independent_experts_actually_run_concurrently():
    barrier = threading.Barrier(3)
    class Parallel(FakeProvider):
        def complete(self, messages, tools=None, **kwargs):
            if tools and tools[0]["function"]["name"] != "read_order":
                barrier.wait(timeout=2)
            return super().complete(messages, tools=tools, **kwargs)
    r = run_five_agent(Parallel(), "SIM-002", "demo-a")
    assert r["status"] == "answered"
    assert len(r["records"]) == 4


def test_graph_endpoint_fails_closed_without_credentials(monkeypatch):
    from fastapi.testclient import TestClient
    from logimate.api import create_app
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with TestClient(create_app()) as c:
        assert c.post('/v1/graph/queries',json={'query':'SIM-002'}).status_code==401
        r = c.post('/v1/graph/queries',json={'query':'SIM-002'},headers={'Authorization':'Bearer local-demo-token'})
        assert r.status_code==503

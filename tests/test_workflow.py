import hashlib
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from baselines.v0 import aggregate
from logimate.api import create_app
from logimate.repository import DATA_FILE, Repository
from logimate.rules import diagnose


@pytest.fixture
def repo():
    return Repository()


@pytest.fixture
def client(repo):
    with TestClient(create_app(repo, token="test-token")) as c:
        c.headers["Authorization"] = "Bearer test-token"
        yield c


def query(client, oid):
    return client.post("/v1/diagnoses", json={"order_id": oid})


@pytest.mark.parametrize("oid,codes,status", [
    ("SIM-001", set(), "clear"),
    ("SIM-002", {"REFUND_OVERDUE", "FINANCE_FAILURE_RECORDED"}, "needs_human"),
    ("SIM-003", {"LOGISTICS_STALLED"}, "anomaly"),
    ("SIM-004", {"STOCK_MISMATCH"}, "anomaly"),
    ("SIM-005", {"MISSING_DATA"}, "needs_human"),
    ("SIM-006", {"RETURN_CONFLICT"}, "needs_human"),
    ("SIM-007", {"FUTURE_TIMESTAMP"}, "needs_human"),
    ("SIM-008", {"REFUND_OVERDUE"}, "needs_human"),
])
def test_scenarios(client, oid, codes, status):
    response = query(client, oid)
    assert response.status_code == 200
    body = response.json()
    assert body["simulated"] is True
    assert body["status"] == status
    assert {f["code"] for f in body["findings"]} == codes
    evidence = {e["evidence_id"]: e for e in body["evidence"]}
    assert {e["domain"] for e in evidence.values()} == ({"order", "logistics", "warehouse", "aftersales"} - set(body["missing_domains"]))
    for finding in body["findings"]:
        assert finding["evidence_ids"]
        assert all(e in evidence for e in finding["evidence_ids"])
    for e in evidence.values():
        assert hashlib.sha256(json.dumps(e["payload"], sort_keys=True).encode()).hexdigest() == e["payload_sha256"]


@pytest.mark.parametrize("oid", ["SIM-999", "SIM-009"])
def test_unknown_and_other_tenant_indistinguishable(client, oid):
    r = query(client, oid)
    assert r.status_code == 404
    assert r.json()["error"]["message"] == "未找到可访问的模拟订单"


@pytest.mark.parametrize("auth", [None, "Bearer wrong"])
def test_auth_required(client, auth):
    client.headers.pop("Authorization")
    if auth:
        client.headers["Authorization"] = auth
    assert query(client, "SIM-002").status_code == 401
    assert client.get("/v1/orders/SIM-002").status_code == 401


@pytest.mark.parametrize("body", [{}, {"order_id": "DROP TABLE orders"},
    {"order_id": "SIM-002", "tenant_id": "demo-b"},
    {"order_id": "SIM-002", "intent": "refund_now"}, {"order_id": 2}])
def test_input_contract(client, body):
    assert client.post("/v1/diagnoses", json=body).status_code == 422


@pytest.mark.parametrize("hours,overdue", [(47.999, False), (48, True), (48.001, True)])
def test_refund_sla_boundary(repo, hours, overdue):
    snapshot = repo.get("SIM-008", "demo-a")
    w = snapshot.warehouse.model_copy(update={"return_received_at": repo.dataset.as_of-timedelta(hours=hours)})
    result = diagnose(snapshot.model_copy(update={"warehouse": w}), as_of=repo.dataset.as_of,
                      dataset_id="synthetic-v1", intent="refund_delay")
    assert ("REFUND_OVERDUE" in {f.code for f in result.findings}) == overdue


@pytest.mark.parametrize("status", ["processing", "paid"])
def test_started_refund_not_reported_as_not_started(repo, status):
    s = repo.get("SIM-008", "demo-a")
    a = s.aftersales.model_copy(update={"refund_status": status})
    r = diagnose(s.model_copy(update={"aftersales": a}), as_of=repo.dataset.as_of, dataset_id="synthetic-v1", intent="overall")
    assert not r.findings


def test_unknown_refund_needs_human(repo):
    s = repo.get("SIM-008", "demo-a")
    a = s.aftersales.model_copy(update={"refund_status": "unknown"})
    r = diagnose(s.model_copy(update={"aftersales": a}), as_of=repo.dataset.as_of, dataset_id="synthetic-v1", intent="overall")
    assert r.needs_human
    assert {f.code for f in r.findings} == {"REFUND_STATUS_UNCERTAIN"}


def test_baseline_does_not_diagnose(repo):
    assert aggregate(repo.get("SIM-002", "demo-a"))["diagnosis"] is None


def test_read_only_and_stable_evidence(client):
    before = DATA_FILE.read_bytes()
    first = query(client, "SIM-002").json()
    second = query(client, "SIM-002").json()
    assert first["trace_id"] != second["trace_id"]
    assert first["evidence"] == second["evidence"]
    assert DATA_FILE.read_bytes() == before
    unsupported = client.post("/v1/refunds", json={})
    assert unsupported.status_code == 404
    assert unsupported.json()["simulated"] is True


def test_raw_records_and_health(client):
    assert client.get("/health").json()["simulated"]
    raw = client.get("/v1/orders/SIM-002").json()["records"]
    assert raw["warehouse"]["return_received_at"]
    assert client.get("/v1/orders/invalid").status_code == 422


def test_cross_domain_tenant_validation(tmp_path):
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    data["snapshots"][0]["warehouse"]["tenant_id"] = "demo-b"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="identity mismatch"):
        Repository(path)

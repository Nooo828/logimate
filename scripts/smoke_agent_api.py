"""One real DeepSeek-backed FastAPI request via in-process HTTP transport."""
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from logimate.api import create_app

ROOT = Path(__file__).resolve().parents[1]


def main():
    with TestClient(create_app(token="agent-api-smoke")) as client:
        response = client.post("/v1/agent/queries", json={"query": "SIM-002退货入库后未退款，请排查四域证据。"},
                               headers={"Authorization": "Bearer agent-api-smoke"})
    body = response.json()
    out = ROOT / "docs/evidence/p2" / datetime.now(timezone.utc).strftime("agent-api-%Y%m%dT%H%M%S%f")
    out.mkdir(parents=True, exist_ok=False)
    (out/"result.json").write_text(json.dumps({"transport": "FastAPI TestClient in-process HTTP; real DeepSeek external request",
        "http_status": response.status_code, "body": body}, ensure_ascii=False, indent=2), encoding="utf-8")
    assert response.status_code == 200, response.status_code
    assert body["status"] == "answered"
    assert len(body["tool_calls"]) == 4
    assert {f["code"] for f in body["diagnosis"]["findings"]} == {"REFUND_OVERDUE", "FINANCE_FAILURE_RECORDED"}
    print(f"Agent API smoke passed. Evidence: {out}")


if __name__ == "__main__":
    main()

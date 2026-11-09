"""Generate only synthetic records; never connect to a business system."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def generate():
    snapshots = []
    for n in range(1, 10):
        oid = f"SIM-{n:03}"
        tenant = "demo-b" if n == 9 else "demo-a"
        common = {"order_id": oid, "tenant_id": tenant, "observed_at": "2026-10-28T08:00:00Z"}
        def record(domain, **fields):
            return dict(common, record_id=f"{domain}-{n:03}", **fields)
        snapshots.append({
            "order": record("order", status="delivered"),
            "logistics": record("logistics", status="delivered", event_at="2026-10-27T08:00:00Z"),
            "warehouse": record("warehouse", return_received_at=None, expected_stock=10, actual_stock=10),
            "aftersales": record("aftersales", return_status="none", refund_status="not_applicable", finance_error=None),
        })
    for n in (2, 6, 8):
        snapshots[n-1]["warehouse"]["return_received_at"] = "2026-10-25T08:00:00Z"
        snapshots[n-1]["logistics"]["status"] = "return_delivered"
        snapshots[n-1]["aftersales"].update(return_status="received", refund_status="not_started")
    snapshots[1]["aftersales"]["finance_error"] = "FINANCE_API_TIMEOUT"
    snapshots[2]["order"]["status"] = "shipped"
    snapshots[2]["logistics"].update(status="stalled", event_at="2026-10-26T08:00:00Z")
    snapshots[3]["warehouse"]["actual_stock"] = 8
    snapshots[4]["aftersales"] = None
    snapshots[5]["aftersales"]["return_status"] = "in_transit"
    snapshots[6]["warehouse"]["return_received_at"] = "2026-10-29T08:00:00Z"
    dataset = {"dataset_id": "synthetic-v1", "simulated": True,
               "as_of": "2026-10-28T08:00:00Z", "snapshots": snapshots}
    path = ROOT / "data" / "synthetic_v1.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(dataset, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    generate()

import hashlib
from pathlib import Path

from .models import Dataset, Snapshot

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "synthetic_v1.json"


class Repository:
    """Read-only local fixture adapter. Tenant comes from verified server context."""

    def __init__(self, path: Path = DATA_FILE):
        self.dataset = Dataset.model_validate_json(path.read_text(encoding="utf-8"))
        self.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        keys = set()
        for snapshot in self.dataset.snapshots:
            key = (snapshot.order.tenant_id, snapshot.order.order_id)
            if key in keys:
                raise ValueError("duplicate tenant/order")
            keys.add(key)
            for domain in ("logistics", "warehouse", "aftersales"):
                record = getattr(snapshot, domain)
                if record and (record.tenant_id, record.order_id) != key:
                    raise ValueError("cross-domain identity mismatch")

    def get(self, order_id: str, tenant_id: str) -> Snapshot | None:
        return next((s for s in self.dataset.snapshots
                     if s.order.order_id == order_id and s.order.tenant_id == tenant_id), None)

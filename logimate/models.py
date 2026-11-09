from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Record(Model):
    record_id: str
    order_id: str
    tenant_id: str
    observed_at: AwareDatetime


class Order(Record):
    status: Literal["delivered", "shipped", "return_requested"]


class Logistics(Record):
    status: Literal["delivered", "stalled", "return_delivered", "not_collected"]
    event_at: AwareDatetime


class Warehouse(Record):
    return_received_at: AwareDatetime | None
    expected_stock: int = Field(ge=0)
    actual_stock: int = Field(ge=0)


class Aftersales(Record):
    return_status: Literal["none", "received", "in_transit"]
    refund_status: Literal["not_applicable", "not_started", "processing", "paid", "unknown"]
    finance_error: Literal["FINANCE_API_TIMEOUT"] | None = None


class Snapshot(Model):
    order: Order
    logistics: Logistics | None
    warehouse: Warehouse | None
    aftersales: Aftersales | None


class Dataset(Model):
    dataset_id: Literal["synthetic-v1"]
    simulated: Literal[True]
    as_of: AwareDatetime
    snapshots: tuple[Snapshot, ...]


class DiagnoseRequest(Model):
    order_id: str = Field(pattern=r"^SIM-[0-9]{3}$")
    intent: Literal["overall", "refund_delay"] = "overall"


class Evidence(Model):
    evidence_id: str
    domain: Literal["order", "logistics", "warehouse", "aftersales"]
    record_id: str
    observed_at: datetime
    payload_sha256: str
    payload: dict


class Finding(Model):
    code: str
    statement: str
    evidence_ids: list[str]
    rule_id: str


class Diagnosis(Model):
    trace_id: str
    implementation: Literal["rules-v1"] = "rules-v1"
    dataset_id: str
    simulated: Literal[True] = True
    as_of: datetime
    order_id: str
    status: Literal["clear", "anomaly", "needs_human"]
    findings: list[Finding]
    evidence: list[Evidence]
    missing_domains: list[str]
    needs_human: bool
    next_steps: list[str]
    limitations: list[str]

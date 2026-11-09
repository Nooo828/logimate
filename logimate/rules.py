import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from .models import Diagnosis, Evidence, Finding, Snapshot

DOMAINS = ("order", "logistics", "warehouse", "aftersales")


def diagnose(snapshot: Snapshot, *, as_of, dataset_id: str, intent: str) -> Diagnosis:
    evidence = []
    refs = {}
    missing = []
    for domain in DOMAINS:
        record = getattr(snapshot, domain)
        if record is None:
            missing.append(domain)
            continue
        payload = record.model_dump(mode="json")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        eid = f"{dataset_id}:{domain}:{record.record_id}:{digest[:12]}"
        refs[domain] = eid
        evidence.append(Evidence(evidence_id=eid, domain=domain, record_id=record.record_id,
                                 observed_at=record.observed_at, payload_sha256=digest, payload=payload))
    findings = []
    human = bool(missing)

    def add(code, statement, domains, rule_id):
        findings.append(Finding(code=code, statement=statement,
                                evidence_ids=[refs[d] for d in domains], rule_id=rule_id))

    if missing:
        add("MISSING_DATA", "部分业务域数据缺失，不能按正常状态处理。", ["order"], "DATA-001")
    w, a, l = snapshot.warehouse, snapshot.aftersales, snapshot.logistics
    future = any(e.observed_at > as_of for e in evidence)
    future = future or bool(w and w.return_received_at and w.return_received_at > as_of)
    future = future or bool(l and l.event_at > as_of)
    if future:
        human = True
        add("FUTURE_TIMESTAMP", "存在晚于模拟查询时间的记录，停止时间规则判定。", list(refs), "DATA-002")
    if not future:
        if w and a:
            received = w.return_received_at is not None
            if received != (a.return_status == "received"):
                human = True
                add("RETURN_CONFLICT", "仓储入库记录与售后退货状态不一致，需核对同步时间线。",
                    ["warehouse", "aftersales"], "CONSISTENCY-001")
            elif received and a.refund_status == "not_started":
                if as_of - w.return_received_at >= timedelta(hours=48):
                    add("REFUND_OVERDUE", "退货已入库至少48小时，退款仍未发起。",
                        ["warehouse", "aftersales"], "REFUND-048H-v1")
                    if a.finance_error:
                        add("FINANCE_FAILURE_RECORDED", "售后模拟记录包含财务接口超时；这是排查线索，尚不能证明唯一根因。",
                            ["aftersales"], "CAUSE-001")
                    human = True
            if received and a.refund_status in ("unknown", "not_applicable"):
                human = True
                add("REFUND_STATUS_UNCERTAIN", "已入库但退款状态不可用于判断，需人工核实。",
                    ["warehouse", "aftersales"], "DATA-003")
        if l and snapshot.order.status == "delivered" and l.status == "not_collected":
            human = True
            add("FULFILLMENT_CONFLICT", "订单已送达与物流未揽收状态不一致。", ["order", "logistics"], "CONSISTENCY-002")
        if intent == "overall":
            if l and l.status == "stalled" and as_of - l.event_at >= timedelta(hours=24):
                add("LOGISTICS_STALLED", "物流模拟状态为停滞且至少24小时未更新。", ["logistics"], "LOGISTICS-024H-v1")
            if w and w.expected_stock != w.actual_stock:
                add("STOCK_MISMATCH", "仓储预期库存与实盘库存不一致。", ["warehouse"], "STOCK-001")
    return Diagnosis(
        trace_id=str(uuid4()), dataset_id=dataset_id, as_of=as_of, order_id=snapshot.order.order_id,
        status="needs_human" if human else ("anomaly" if findings else "clear"),
        findings=findings, evidence=evidence, missing_domains=missing, needs_human=human,
        next_steps=["将证据交给运营核实；本系统未创建外部工单，也未执行退款。"] if human
        else (["请运营核对异常对应记录。"] if findings else ["当前已实现规则未发现异常。"]),
        limitations=["全部记录为模拟数据；48/24小时阈值为本复现的演示规则。",
                     "第一阶段仅支持显式单号和枚举意图，无LLM、LangGraph、MCP或实时系统连接。",
                     "人工介入仅为响应标记，尚未实现持久工单和中断恢复。"],
    )

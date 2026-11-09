"""Paired local/MCP graph acceptance. Default planner is explicitly a test double."""
import argparse
import getpass
import hashlib
import json
import os
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path

from logimate.deepseek import DeepSeek
from logimate.five_agent import run_tool_graph
from logimate.rules import DOMAINS

ROOT = Path(__file__).resolve().parent.parent
CASES = [
    {"id": "finance", "query": "请从四个业务域整体排查SIM-002退货后未退款。", "codes": ["FINANCE_FAILURE_RECORDED", "REFUND_OVERDUE"], "status": "answered", "domains": list(DOMAINS)},
    {"id": "no-cause", "query": "请从四个业务域整体排查SIM-008退货后未退款。", "codes": ["REFUND_OVERDUE"], "status": "answered", "domains": list(DOMAINS)},
    {"id": "missing", "query": "请从四个业务域整体排查SIM-005。", "codes": ["MISSING_DATA"], "status": "answered", "domains": list(DOMAINS)},
    {"id": "conflict", "query": "请从四个业务域整体排查SIM-006。", "codes": ["RETURN_CONFLICT"], "status": "answered", "domains": list(DOMAINS)},
    {"id": "order", "query": "仅查询SIM-001的订单状态。", "codes": [], "status": "answered", "domains": ["order"]},
    {"id": "stock", "query": "仅核对SIM-004的库存预期和实盘数量。", "codes": [], "status": "answered", "domains": ["order", "warehouse"]},
    {"id": "tenant", "query": "请从四个业务域整体排查SIM-009。", "codes": [], "status": "not_found", "domains": []},
]


class FixturePlanner:
    """No LLM: deterministic planning only, used to isolate graph/transport behavior."""
    def complete(self, messages, tools=None, **kwargs):
        if tools is None:
            query = messages[-1]["content"]
            oid = re.search(r"SIM-[0-9]{3}", query).group()
            domains = ["order"] if "仅查询" in query else ["order", "warehouse"] if "仅核对" in query else list(DOMAINS)
            message = {"content": json.dumps({"action": "read", "order_ids": [oid], "domains": domains})}
        else:
            oid = json.loads(messages[-1]["content"])["order_id"]
            message = {"tool_calls": [{"function": {"name": tools[0]["function"]["name"],
                         "arguments": json.dumps({"order_id": oid})}}]}
        return message, {"model": "explicit-fixture-planner-no-LLM", "usage": {"total_tokens": 0}}


def canonical(response):
    diagnosis = dict(response["diagnosis"]) if response.get("diagnosis") else None
    if diagnosis:
        diagnosis.pop("trace_id", None)
    return {"status": response["status"], "records": response["records"], "diagnosis": diagnosis,
            "tool_calls": sorted(response["tool_calls"], key=lambda x: x["domain"])}


def compare(provider, *, live=False):
    rows = []
    for i, case in enumerate(CASES):
        pair = {}
        modes = ["local-gateway", "mcp-stdio"]
        if i % 2:
            modes.reverse()
        for mode in modes:
            r = run_tool_graph(provider, case["query"], "demo-a", transport=mode)
            if not live:
                r.update(model_called=False, planner_test_double=True)
            codes = sorted(f["code"] for f in (r.get("diagnosis") or {}).get("findings", []))
            match = (r["status"] == case["status"] and codes == sorted(case["codes"])
                     and {c["domain"] for c in r["tool_calls"]} == set(case["domains"]))
            pair[mode] = r
            rows.append({"case_id": case["id"], "mode": mode, "expected_match": match, "response": r})
        equal = canonical(pair["local-gateway"]) == canonical(pair["mcp-stdio"])
        for row in rows[-2:]:
            row["pair_equal"] = equal
    summary = {mode: {"n": len(CASES), "expected_match": sum(r["expected_match"] for r in rows if r["mode"] == mode),
        "median_elapsed_ms": statistics.median(r["response"]["elapsed_ms"] for r in rows if r["mode"] == mode),
        "planner_responses": sum(len(r["response"]["provider_calls"]) for r in rows if r["mode"] == mode),
        "real_provider_responses": sum(len(r["response"]["provider_calls"]) for r in rows if r["mode"] == mode) if live else 0,
        "total_tokens": sum(r["response"]["usage"]["total_tokens"] for r in rows if r["mode"] == mode)} for mode in modes}
    return {"passed": all(r["expected_match"] and r["pair_equal"] for r in rows), "simulated": True,
            "real_model_calls": live, "planner": "DeepSeek" if live else "explicit deterministic test double",
            "summary": summary, "rows": rows,
            "limitations": ["Seven synthetic scenarios, not production accuracy or latency.",
                            "Test-double mode has no real model responses or model tokens.",
                            "MCP starts one subprocess/session per graph request; startup and cleanup included."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Hidden key input; real model calls incur API usage")
    parser.add_argument("--stdout", action="store_true", help="No filesystem writes; used by non-root Docker checks")
    args = parser.parse_args()
    # The integration stage must follow the actually accepted MCP container.
    if not args.stdout:
        gates = []
        for path in (ROOT / "docs/evidence/docker-stage").glob("*/summary.json"):
            gate = json.loads(path.read_text(encoding="utf-8"))
            if gate.get("acceptance_phase") == "P4-mcp" and gate.get("status") == "passed" and gate.get("cleanup_passed"):
                gates.append(path)
        if not gates:
            raise RuntimeError("P4 MCP container acceptance must pass first")
    if args.live:
        if not os.environ.get("DEEPSEEK_API_KEY"):
            os.environ["DEEPSEEK_API_KEY"] = getpass.getpass("DeepSeek API key (hidden, this run only): ")
        provider = DeepSeek()
    else:
        provider = FixturePlanner()
    result = compare(provider, live=args.live)
    if args.stdout:
        print(json.dumps(result, ensure_ascii=True))
    else:
        out = ROOT / "docs/evidence/p4" / datetime.now(timezone.utc).strftime("graph-%Y%m%dT%H%M%S%f")
        out.mkdir(parents=True)
        result["accepted_p4_gate"] = str(sorted(gates)[-1].relative_to(ROOT))
        result["created_at_utc"] = datetime.now(timezone.utc).isoformat()
        result["input_sha256"] = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for pattern in ("logimate/*.py", "scripts/verify_mcp_graph.py", "requirements.lock", "data/synthetic_v1.json", "prompts/*.txt") for p in ROOT.glob(pattern)}
        (out / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": result["passed"], "real_model_calls": args.live, "summary": result["summary"], "evidence": str(out)}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

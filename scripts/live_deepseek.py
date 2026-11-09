"""Bounded live experiment: synthetic routing24 + single-agent3, official DeepSeek only."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from logimate.deepseek import DeepSeek, ModelUnavailable
from logimate.routing_eval import score
from logimate.single_agent import route_query, run_agent

ROOT = Path(__file__).resolve().parents[1]


def main():
    provider = DeepSeek()  # Fail clearly before creating a run if no key; no mock fallback.
    out = ROOT / "docs/evidence/p2" / datetime.now(timezone.utc).strftime("deepseek-%Y%m%dT%H%M%S%f")
    out.mkdir(parents=True, exist_ok=False)
    dataset_path = ROOT / "eval/routing_v1.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    result = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "real_model_calls": True,
              "business_data": "synthetic-v1", "requested_model": provider.model,
              "predictions": {}, "routing_calls": [], "agents": [], "failures": [],
              "files_sha256": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for pattern in ("prompts/*.txt", "logimate/*.py", "eval/routing_v1.json", "data/synthetic_v1.json")
                for p in ROOT.glob(pattern)}}

    def save():
        (out/"experiment.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    for case in dataset["examples"]:
        calls = []
        try:
            route, _ = route_query(provider, case["query"], call_log=calls)
            result["predictions"][case["case_id"]] = route.model_dump()
        except ModelUnavailable as exc:
            result["failures"].append({"case_id": case["case_id"], "reason": str(exc)})
            # Invalid credentials/balance/rate limit: stop rather than repeat billable failures.
            if any(code in str(exc) for code in ("401", "402", "403", "429")):
                result["stop_reason"] = str(exc)
                save()
                print(f"Stopped: {exc}. Evidence: {out}", flush=True)
                return 1
        except ValueError:
            result["predictions"][case["case_id"]] = {}
            result["failures"].append({"case_id": case["case_id"], "reason": "route_contract_error"})
        result["routing_calls"].append({"case_id": case["case_id"], "calls": calls})
        save()
        print(f"routing {case['case_id']}: saved", flush=True)
    result["routing_scores"] = {split: score(dataset, {c["case_id"]: result["predictions"][c["case_id"]]
        for c in dataset["examples"] if c["split"] == split and c["case_id"] in result["predictions"]}, split)
        for split in ("dev", "holdout")}
    for query in ["SIM-002退货入库了还没退款，请核对四个域定位异常。",
                  "SIM-008退货后没退款，请跨域核实原因。", "查询SIM-009的订单状态。"]:
        agent = run_agent(provider, query, "demo-a")
        agent["query"] = query
        result["agents"].append(agent)
        save()
        print(f"agent: {agent['status']}, tools={len(agent['tool_calls'])}", flush=True)
    calls = [call for row in result["routing_calls"] for call in row["calls"]]
    calls += [call for agent in result["agents"] for call in agent["provider_calls"]]
    result["successful_provider_responses"] = len(calls)
    result["usage"] = {k: sum(c.get("usage", {}).get(k, 0) or 0 for c in calls)
                       for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    result["usage_note"] = "Provider-returned usage only; missing usage on failed calls is not estimated; no currency cost claim."
    save()
    print(json.dumps({"evidence": str(out), "responses": len(calls), "usage": result["usage"],
                      "routing": {s:r["metrics"] for s,r in result["routing_scores"].items()}}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

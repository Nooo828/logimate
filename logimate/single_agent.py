import hashlib
import json
import re
import time
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .deepseek import ModelUnavailable
from .repository import Repository
from .routing_eval import Route
from .rules import diagnose

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
ROUTE_PROMPT = (PROMPTS / "route-v1.txt").read_text(encoding="utf-8")
TOOL_PROMPT = (PROMPTS / "tools-v1.txt").read_text(encoding="utf-8")
DOMAINS = ("order", "logistics", "warehouse", "aftersales")


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(pattern=r"^SIM-[0-9]{3}$")


def route_query(provider, query, timeout=20, call_log=None):
    message, meta = provider.complete([{"role": "system", "content": ROUTE_PROMPT},
                                       {"role": "user", "content": query}], json_output=True, timeout=timeout)
    if call_log is not None:
        call_log.append(meta)
    route = Route.model_validate_json(message.get("content") or "")
    if len(route.order_ids) != len(set(route.order_ids)) or len(route.domains) != len(set(route.domains)):
        raise ValueError("duplicate route fields")
    present = set(re.findall(r"(?<![A-Za-z0-9-])SIM-[0-9]{3}(?![A-Za-z0-9-])", query))
    if set(route.order_ids) != present:
        raise ValueError("model slots differ from explicit query ids")
    if route.action != "read" and route.domains:
        raise ValueError("non-read action requested tool domains")
    if route.action == "read" and (len(route.order_ids) != 1 or not route.domains):
        raise ValueError("read route needs one explicit id and nonempty domains")
    return route, meta


def run_agent(provider, query, tenant_id, repository=None, *, max_rounds=4, budget_seconds=60):
    repo = repository or Repository()
    start = time.monotonic()
    result = {"trace_id": str(uuid4()), "implementation": "single-agent-v1", "simulated": True,
              "status": "needs_human", "route": None, "provider_calls": [], "tool_calls": [],
              "records": {}, "diagnosis": None, "reason": None, "model_called": False,
              "dataset_id": repo.dataset.dataset_id, "dataset_sha256": repo.sha256,
              "prompt_sha256": {"route": hashlib.sha256(ROUTE_PROMPT.encode()).hexdigest(),
                                 "tools": hashlib.sha256(TOOL_PROMPT.encode()).hexdigest()}}

    def remaining():
        left = budget_seconds - (time.monotonic()-start)
        if left <= 0:
            raise ModelUnavailable("Agent total time budget exhausted")
        return min(20, left)

    try:
        result["model_called"] = True
        route, meta = route_query(provider, query, timeout=remaining(), call_log=result["provider_calls"])
        result["route"] = route.model_dump()
        if route.action != "read":
            result["status"] = route.action
            result["reason"] = "模型请求澄清/转人工/拒绝；未执行工具。"
            return result
        oid = route.order_ids[0]
        allowed = set(route.domains) | {"order"}
        tools = [{"type": "function", "function": {"name": "read_"+domain,
                 "description": "Read synthetic " + domain + " records. Tenant enforced by server.",
                 "parameters": ToolArgs.model_json_schema()}} for domain in DOMAINS]
        messages = [{"role": "system", "content": TOOL_PROMPT},
                    {"role": "user", "content": json.dumps({"query": query, "route": route.model_dump(),
                                                             "required_domains": sorted(allowed)}, ensure_ascii=False)}]
        seen, fetched = set(), set()
        for _ in range(max_rounds):
            message, meta = provider.complete(messages, tools=tools, timeout=remaining())
            result["provider_calls"].append(meta)
            calls = message.get("tool_calls", [])
            if not calls:
                raise ValueError("model ended before required evidence was read")
            if len(calls) > 4:
                raise ValueError("tool call budget exceeded")
            messages.append(message)
            for call in calls:
                fn = call["function"]
                name = fn["name"]
                if name not in {"read_"+d for d in allowed}:
                    raise ValueError("tool outside authorized route/whitelist")
                args = ToolArgs.model_validate_json(fn["arguments"])
                if args.order_id != oid:
                    raise ValueError("tool changed explicit order id")
                fingerprint = (name, args.order_id)
                if fingerprint in seen:
                    raise ValueError("duplicate tool call/no progress")
                seen.add(fingerprint)
                domain = name.removeprefix("read_")
                snapshot = repo.get(args.order_id, tenant_id)
                if snapshot is None:
                    result["status"] = "not_found"
                    result["reason"] = "未找到可访问的模拟订单；与其他租户单号返回同一结果。"
                    return result
                record = getattr(snapshot, domain)
                payload = record.model_dump(mode="json") if record else None
                result["records"][domain] = payload
                result["tool_calls"].append({"name": name, "order_id": oid, "domain": domain,
                                              "state": "found" if record else "missing", "record_id": record.record_id if record else None})
                fetched.add(domain)
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps({"simulated": True, "domain": domain, "record": payload}, ensure_ascii=False)})
            if fetched >= allowed:
                result["status"] = "answered"
                result["reason"] = "仅返回真实工具记录；业务诊断由代码规则输出。"
                if fetched == set(DOMAINS):
                    result["diagnosis"] = diagnose(snapshot, as_of=repo.dataset.as_of,
                        dataset_id=repo.dataset.dataset_id, intent="overall").model_dump(mode="json")
                return result
        raise ValueError("maximum agent rounds exceeded")
    except ModelUnavailable as exc:
        result["status"] = "unavailable"
        result["reason"] = str(exc)
    except (ValueError, KeyError, TypeError):
        result["status"] = "needs_human"
        result["reason"] = "模型输出或工具计划不满足契约/预算，已停止，需人工核实。"
    finally:
        result["elapsed_ms"] = round((time.monotonic()-start)*1000, 3)
        result["usage"] = {key: sum(m.get("usage", {}).get(key, 0) or 0 for m in result["provider_calls"])
                           for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        result["limitations"] = ["真实DeepSeek调用+合成业务数据；非历史原工程。",
            "单Agent负责路由和工具选择；最终业务结论为确定性规则，不是自由文本生成。",
            "无LangGraph/MCP；不执行退款或真实人工工单。"]
    return result

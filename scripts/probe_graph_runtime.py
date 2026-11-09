"""Probe one local/MCP graph pair in the exact existing P4.2 image; no rebuild first."""
import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent


def select_image(root):
    candidates = []
    for path in (root / "docs/evidence/docker-stage").glob("*/summary.json"):
        info = json.loads(path.read_text(encoding="utf-8"))
        if info.get("acceptance_phase") != "P4.2-graph-mcp":
            continue
        for step in info.get("steps", []):
            if step["name"] == "image-health-ports" and step["exit_code"] == 0:
                candidates.append((path, info, step["log"]))
    if not candidates:
        raise RuntimeError("No recorded P4.2 image; do not guess an image or rebuild")
    source, info, log = sorted(candidates, key=lambda x: str(x[0]))[-1]
    image = json.loads(next(line for line in (source.parent / log).read_text(encoding="utf-8").splitlines()
                            if line.startswith('{"Image":')))["Image"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Invalid recorded image digest")
    # Only app modules are mounted. Refuse dependency/data/prompt drift in the reused image.
    for name in ("requirements.lock", "data/synthetic_v1.json", "prompts/route-v1.txt", "prompts/tools-v1.txt"):
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != info["input_sha256"].get(name):
            raise RuntimeError("Existing image dependency/data/prompt mismatch; fresh build required")
    return image, source


def graph_passed(row):
    body = row.get("body", {})
    return (row.get("http_status") == 200 and body.get("simulated") is True
            and body.get("status") == "answered"
            and sorted(f.get("code") for f in (body.get("diagnosis") or {}).get("findings", [])) ==
            ["FINANCE_FAILURE_RECORDED", "REFUND_OVERDUE"]
            and len(body.get("tool_calls", [])) == 4
            and len(body.get("provider_calls", [])) == 5
            and body.get("usage", {}).get("total_tokens", 0) > 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--then-verify", action="store_true", help="Only after both probes pass, rebuild and run full P4.2 acceptance")
    args = parser.parse_args()
    image, source = select_image(ROOT)
    port = int(os.environ.get("LOGIMATE_PROBE_PORT", "18081"))
    if not 1024 <= port <= 65535:
        raise ValueError("Invalid probe port")
    # Preflight before requesting a credential; no reset or host runtime repair.
    preflight = subprocess.run(["docker", "version", "--format", "{{json .Server}}"], capture_output=True, timeout=15)
    if preflight.returncode:
        print("Docker engine unavailable to this terminal; no model calls or rebuild performed.")
        return 1
    key = (os.environ.get("DEEPSEEK_API_KEY") or getpass.getpass("DeepSeek API key (hidden, probe + optional verification only): ")).strip()
    if not key:
        raise RuntimeError("Empty credential")
    env = dict(os.environ, DEEPSEEK_API_KEY=key, PYTHONIOENCODING="utf-8", LOGIMATE_DEMO_TOKEN="runtime-probe-only")
    out = ROOT / "docs/evidence/docker-stage" / datetime.now(timezone.utc).strftime("graph-probe-%Y%m%dT%H%M%S%f")
    out.mkdir(parents=True)
    name = "logimate-graph-probe-" + uuid.uuid4().hex[:12]
    result = {"status": "running", "image": image, "source_summary": source.relative_to(ROOT).as_posix(),
              "existing_image_reused": True, "workspace_app_mount_readonly": True, "live_model_mode": True,
              "model_attempts_reported": 0, "successful_provider_responses_reported": 0,
              "input_sha256": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in ROOT.glob("logimate/*.py")}, "requests": []}

    def safe_text(value):
        return value.replace(key, "[REDACTED]")

    def command(label, argv, timeout=30):
        proc = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        (out / (label + ".txt")).write_text(safe_text(proc.stdout + proc.stderr), encoding="utf-8")
        if proc.returncode:
            raise RuntimeError(label + " failed; inspect saved local record")
        return proc.stdout

    def request(path, payload=None, timeout=5):
        req = Request(f"http://127.0.0.1:{port}" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json", "Authorization": "Bearer runtime-probe-only"})
        try:
            response = urlopen(req, timeout=timeout)
        except HTTPError as exc:
            response = exc  # This is our local API, never the vendor error body.
        with response:
            return {"http_status": response.code, "body": json.loads(response.read(524288))}

    started = False
    try:
        mount = f"type=bind,source={ROOT / 'logimate'},target=/app/logimate,readonly"
        if "," in str(ROOT):
            raise ValueError("Unsupported comma in bind source path")
        started = True
        command("container-start", ["docker", "run", "--rm", "-d", "--name", name,
            "--publish", f"127.0.0.1:{port}:8000", "--mount", mount,
            "--env", "DEEPSEEK_API_KEY", "--env", "LOGIMATE_MODEL", "--env", "LOGIMATE_DEMO_TOKEN",
            "--env", "PYTHONDONTWRITEBYTECODE=1", image])
        print("Existing image started with updated app modules mounted read-only; no rebuild.", flush=True)
        deadline = time.monotonic() + 30
        while True:
            try:
                health = request("/health")
                if health["http_status"] == 200:
                    break
            except (URLError, OSError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Probe API readiness timeout")
            time.sleep(1)
        for mode, path in [("local-graph", "/v1/graph/queries"), ("mcp-graph", "/v1/graph/mcp/queries")]:
            began = time.monotonic()
            row = request(path, {"query": "SIM-002退货入库后未退款，请核对四个业务域。"}, timeout=90)
            row.update(mode=mode, elapsed_ms=round((time.monotonic() - began) * 1000, 3))
            row["passed"] = graph_passed(row)
            if mode == "mcp-graph":
                row["passed"] = row["passed"] and row["body"].get("tool_transport") == "mcp-stdio" and len(row["body"].get("tool_transport_audit", [])) == 4
            result["requests"].append(row)
            body = row["body"]
            result["model_attempts_reported"] += len(body.get("model_attempts", []))
            result["successful_provider_responses_reported"] += len(body.get("provider_calls", []))
            compact = {k: body[k] for k in ("status", "failure_details", "model_attempts", "events", "usage", "reason") if k in body}
            print(safe_text(json.dumps({"mode": mode, "http_status": row["http_status"], "passed": row["passed"], **compact}, ensure_ascii=True)), flush=True)
        result["status"] = "passed" if all(r["passed"] for r in result["requests"]) else "failed"
    except Exception as exc:
        result.update(status="failed", probe_error_type=type(exc).__name__)
        print("Probe execution failed; inspect local evidence. No full acceptance started.", flush=True)
    finally:
        if started:
            try:
                command("container-logs", ["docker", "logs", name], timeout=10)
            except Exception:
                pass
            try:
                cleanup = subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
                result["cleanup_passed"] = cleanup.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                result["cleanup_passed"] = False
            if not result["cleanup_passed"]:
                result["status"] = "failed"
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        (out / "summary.json").write_text(safe_text(json.dumps(result, ensure_ascii=False, indent=2)), encoding="utf-8")
        print("Evidence: " + str(out), flush=True)
    if result["status"] != "passed":
        return 1
    if args.then_verify:
        return subprocess.call([sys.executable, "-m", "scripts.verify_docker", "--p4-graph", "--wait-engine", "30"], cwd=ROOT, env=env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

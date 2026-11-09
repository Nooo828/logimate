"""Start a real local Uvicorn process, exercise HTTP, then stop only that process."""
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "evidence"


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = "smoke-test-only"
    env = dict(os.environ, LOGIMATE_DEMO_TOKEN=token)
    command = [sys.executable, "-m", "uvicorn", "logimate.api:app", "--host", "127.0.0.1", "--port", str(port)]
    checks = []

    def request(path, body=None, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + token
        req = Request(f"http://127.0.0.1:{port}"+path, headers=headers,
                      data=json.dumps(body).encode() if body is not None else None)
        start = time.perf_counter()
        try:
            response = urlopen(req, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            payload = json.loads(response.read())
            code = response.code
        return code, payload, round((time.perf_counter()-start)*1000, 3)

    with (OUT / "uvicorn-smoke.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            for _ in range(50):
                if process.poll() is not None:
                    raise RuntimeError("Uvicorn exited before readiness")
                try:
                    status, body, ms = request("/health")
                    if status == 200:
                        break
                except (URLError, TimeoutError):
                    time.sleep(0.1)
            else:
                raise RuntimeError("Uvicorn readiness timed out")
            checks.append({"name": "health", "status": status, "elapsed_ms": ms, "body": body})
            for name, path, body, auth, expected in [
                ("raw-four-domains", "/v1/orders/SIM-002", None, True, 200),
                ("refund-delay", "/v1/diagnoses", {"order_id": "SIM-002", "intent": "refund_delay"}, True, 200),
                ("missing", "/v1/diagnoses", {"order_id": "SIM-999"}, True, 404),
                ("tenant-isolation", "/v1/diagnoses", {"order_id": "SIM-009"}, True, 404),
                ("auth", "/v1/orders/SIM-002", None, False, 401),
                ("invalid", "/v1/diagnoses", {"order_id": "not-an-order"}, True, 422),
            ]:
                status, result, ms = request(path, body, auth)
                assert status == expected, (name, status, result)
                if name == "refund-delay":
                    assert {f["code"] for f in result["findings"]} == {"REFUND_OVERDUE", "FINANCE_FAILURE_RECORDED"}
                    assert len(result["evidence"]) == 4
                checks.append({"name": name, "status": status, "elapsed_ms": ms, "body": result})
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    result = {"captured_at_utc": datetime.now(timezone.utc).isoformat(), "transport": "real local HTTP",
              "bind": "127.0.0.1", "port": port, "simulated": True, "server_stopped": process.poll() is not None,
              "checks": checks, "note": "One local smoke sample per request; not a latency benchmark or business evaluation."}
    (OUT / "http-smoke.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(checks)} real HTTP checks passed; test server stopped.")


if __name__ == "__main__":
    run()

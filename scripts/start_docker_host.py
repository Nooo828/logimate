"""Normal Desktop startup with evidence. Never resets WSL or Docker data."""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP_PIPE = "npipe:////./pipe/dockerDesktopLinuxEngine"


def decode(data):
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    # Redirected Windows wsl.exe output can be UTF-16LE without a BOM.
    return data.decode("utf-16-le" if b"\x00" in data else "utf-8", errors="replace")


def linux_server(code, output):
    if code:
        return None
    try:
        server = json.loads(output)
    except ValueError:
        return None
    if not isinstance(server, dict) or not server.get("Version") or server.get("Os") != "linux":
        return None
    return server


def wait_for_engine(probe, wait_seconds, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + wait_seconds
    while True:
        code, output = probe()
        server = linux_server(code, output)
        if server:
            return server
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        sleep(min(5, remaining))


def run_acceptance(server, enabled, env, runner=subprocess.run):
    if not server or not enabled:
        return None
    # Only change the child's environment; leave user Docker configuration alone.
    child_env = dict(env, DOCKER_HOST=DESKTOP_PIPE)
    child_env.pop("DOCKER_CONTEXT", None)
    return runner([sys.executable, "-m", "scripts.verify_milvus"], cwd=ROOT,
                  env=child_env, check=False).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-desktop", action="store_true")
    parser.add_argument("--wait-engine", type=int, default=120, choices=range(0, 601), metavar="0..600")
    parser.add_argument("--then-verify-milvus", action="store_true")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    out = ROOT / "docs/evidence/docker-stage" / ("startup-" + stamp)
    out.mkdir(parents=True, exist_ok=False)
    report = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
              "status": "running", "steps": [], "engine_ready": False,
              "data_reset": False, "wsl_terminated": False,
              "milvus_acceptance_requested": args.then_verify_milvus}

    def save():
        (out / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def command(label, argv, timeout=20):
        try:
            p = subprocess.run(argv, cwd=ROOT, capture_output=True, timeout=timeout)
            code, stdout, stderr = p.returncode, decode(p.stdout), decode(p.stderr)
        except subprocess.TimeoutExpired as exc:
            code, stdout, stderr = 124, decode(exc.stdout), decode(exc.stderr) + "\nCommand timed out"
        except OSError as exc:
            code, stdout, stderr = 127, "", str(exc)
        log = f"{len(report['steps']) + 1:02d}-{label}.txt"
        # No credentials are requested and Docker credential config is never copied.
        (out / log).write_text(stdout + stderr, encoding="utf-8")
        report["steps"].append({"name": label, "exit_code": code, "log": log})
        save()
        print(f"{label}: exit {code}", flush=True)
        return code, stdout

    save()
    try:
        command("wsl-list", ["wsl.exe", "--list", "--verbose"])
        command("wsl-status", ["wsl.exe", "--status"])
        command("wsl-version", ["wsl.exe", "--version"])
        command("host-evidence-before", [sys.executable, "-m", "scripts.collect_docker_host"], timeout=30)
        command("default-engine", ["docker", "version", "--format", "{{json .Server}}"])
        probe_count = 0

        def probe():
            nonlocal probe_count
            probe_count += 1
            return command(f"desktop-engine-{probe_count}",
                           ["docker", "--host", DESKTOP_PIPE, "version", "--format", "{{json .Server}}"], timeout=10)

        server = wait_for_engine(probe, 0)
        if not server and args.start_desktop:
            command("desktop-start", ["docker", "desktop", "start", "--timeout", "60"], timeout=65)
            server = wait_for_engine(probe, args.wait_engine)
        report["engine_ready"] = bool(server)
        if server:
            report["server"] = server
            report["status"] = "engine_ready"
            save()
            code = run_acceptance(server, args.then_verify_milvus, os.environ)
            report["milvus_exit_code"] = code
            if code is not None:
                report["status"] = "passed" if code == 0 else "milvus_failed"
        else:
            report["status"] = "engine_unavailable"
        command("host-evidence-after", [sys.executable, "-m", "scripts.collect_docker_host"], timeout=30)
    except (OSError, ValueError) as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
    finally:
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save()
        print("Status: " + report["status"], flush=True)
        print("Evidence: " + str(out), flush=True)
    return 0 if report["status"] in ("engine_ready", "passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

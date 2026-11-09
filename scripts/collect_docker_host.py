"""Read-only Docker startup evidence collector, intended for the user's terminal.

Does not stop processes, rename/delete files, change settings, or upload diagnostics.
Does not require the Docker engine to start. Run with the project's Python.
"""
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def collect():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = ROOT / "docs/evidence/docker-stage" / f"host-{stamp}-{os.getpid()}"
    out.mkdir(parents=True, exist_ok=False)
    local = Path(os.environ["LOCALAPPDATA"])
    report = {"captured_at_utc": datetime.now(timezone.utc).isoformat(), "localappdata": str(local),
              "read_only": True, "errors": [], "directories": {}}
    if "\\packages\\sandbox." in str(local).lower():
        report["errors"].append("LOCALAPPDATA is redirected; run from your own Windows terminal for host evidence.")
    for folder in (local / "Docker", local / "Docker/run", local / "docker-secrets-engine"):
        try:
            rows = []
            for path in folder.iterdir():
                if folder == local / "Docker" and not path.name.lower().startswith("run"):
                    continue
                st = path.lstat()  # Do not follow AF_UNIX reparse points.
                rows.append({"name": path.name, "path": str(path), "size": st.st_size,
                             "attributes": getattr(st, "st_file_attributes", None),
                             "modified_at_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()})
            report["directories"][str(folder)] = rows
        except OSError as exc:
            report["errors"].append(f"{folder}: {exc}")
    error_file = local / "Docker/backend.error.json"
    try:
        content = error_file.read_bytes()
        (out / "backend-error.json").write_bytes(content)
        report["backend_error_modified_at_utc"] = datetime.fromtimestamp(error_file.stat().st_mtime, timezone.utc).isoformat()
        report["backend_error_sha256"] = hashlib.sha256(content).hexdigest()
    except OSError as exc:
        report["errors"].append(str(exc))
    log = local / "Docker/log/host/com.docker.backend.exe.log"
    try:
        # Keep a bounded local excerpt, without uploading a diagnostic bundle.
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 64000))
            tail = stream.read().decode("utf-8", errors="replace")
        (out / "backend-tail.txt").write_text(tail, encoding="utf-8")
    except OSError as exc:
        report["errors"].append(str(exc))
    args = ["powershell.exe", "-NoProfile", "-Command",
            "Get-Process -Name 'Docker Desktop','com.docker.backend','docker-desktop' -ErrorAction SilentlyContinue | "
            "Select-Object Id,ProcessName,Path | ConvertTo-Json"]
    try:
        p = subprocess.run(args, capture_output=True, timeout=15)
        (out / "docker-processes.txt").write_text(p.stdout.decode("utf-8", errors="replace") +
                                                  p.stderr.decode("utf-8", errors="replace"), encoding="utf-8")
        report["process_probe_exit_code"] = p.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        report["errors"].append(f"Process inventory unavailable: {exc}")
    (out / "host-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "record.md").write_text(
        f"# Docker宿主只读收集 {stamp}\n\n未修改系统，未上传日志。\n\n"
        f"LOCALAPPDATA: `{local}`\n\n见host-report.json、backend-error.json（如可读）、backend-tail.txt与docker-processes.txt。\n",
        encoding="utf-8")
    print(f"Saved local read-only evidence: {out}", flush=True)


if __name__ == "__main__":
    collect()

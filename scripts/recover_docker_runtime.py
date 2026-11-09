"""Scoped Windows Docker runtime recovery for the user's own terminal.

Default: preview only. --apply permits graceful stop, verified Docker process stop,
and backup-by-rename of runtime folders. No deletion, WSL reset, or settings changes.
--include-secrets also quarantines the observed old secrets engine socket folder.
--verify runs the existing real Docker acceptance suite after engine readiness.
"""
import argparse
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKER_HOME = Path(r"C:\Program Files\Docker\Docker")
ALLOWED_PROCESSES = [DOCKER_HOME / "Docker Desktop.exe", DOCKER_HOME / "frontend/Docker Desktop.exe",
                     DOCKER_HOME / "resources/com.docker.backend.exe"]
RUN_ENTRIES = {"dockerInference", "userAnalyticsOtlpHttp.sock", "sailor-ingest.sock", "dockerEthernetVfkit"}


def quarantine(source, expected_parent, allowed_entries, suffix, apply=False):
    source, parent = Path(source).absolute(), Path(expected_parent).resolve(strict=True)
    if source.parent.resolve(strict=True) != parent:
        raise ValueError("Runtime directory parent mismatch")
    info = source.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or source.is_symlink():
        raise ValueError("Runtime directory must be a plain directory")
    names = {p.name for p in source.iterdir()}
    if names - allowed_entries:
        raise ValueError("Unexpected runtime entries; stop for inspection: " + repr(sorted(names - allowed_entries)))
    backup = source.with_name(source.name + ".backup-" + suffix)
    if backup.parent.resolve(strict=True) != parent or backup.exists():
        raise ValueError("Unsafe or existing backup destination")
    plan = {"source": str(source), "backup": str(backup), "entries": sorted(names), "applied": False}
    if apply:
        source.rename(backup)  # Same-parent rename only. Never follows or deletes socket entries.
        try:
            source.mkdir()
        except OSError:
            backup.rename(source)
            raise
        plan["applied"] = True
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--include-secrets", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    out = ROOT / "docs/evidence/docker-stage" / ("recovery-" + stamp)
    out.mkdir(parents=True, exist_ok=False)
    result = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "apply": args.apply,
              "status": "running", "steps": [], "backups": []}

    def save():
        (out / "recovery.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def command(name, argv, timeout=30, required=True):
        print(name, flush=True)
        try:
            proc = subprocess.run(argv, cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
            code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            code, stdout, stderr = 124, "", f"Timed out after {timeout}s"
        log = f"{len(result['steps'])+1:02d}-{name}.txt"
        (out / log).write_text("COMMAND: " + subprocess.list2cmdline(argv) + "\n" + stdout + stderr + f"\nEXIT_CODE: {code}\n", encoding="utf-8")
        result["steps"].append({"name": name, "exit_code": code, "log": log})
        save()
        if code and required:
            raise RuntimeError(f"{name} failed; see {out/log}")
        return code, stdout

    def powershell(name, code):
        return command(name, ["powershell.exe", "-NoProfile", "-Command",
                       "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); " + code])

    def processes(label):
        _, text = powershell(label, "$rows=@(Get-Process | Where-Object { $_.ProcessName -eq 'Docker Desktop' -or $_.ProcessName -eq 'com.docker.backend' } | Select-Object Id,ProcessName,Path); ConvertTo-Json -InputObject $rows -Compress")
        rows = json.loads(text.strip())
        for row in rows:
            if not row.get("Path") or os.path.normcase(row["Path"]) not in {os.path.normcase(str(p)) for p in ALLOWED_PROCESSES}:
                raise RuntimeError("Docker process path could not be verified; no forced stop permitted")
        return rows

    try:
        local = Path(os.environ["LOCALAPPDATA"])
        if "\\packages\\sandbox." in str(local).lower():
            raise RuntimeError("Run this script from your own Windows terminal; current LOCALAPPDATA is redirected")
        specs = [(local / "Docker/run", local / "Docker", RUN_ENTRIES)]
        if args.include_secrets and (local / "docker-secrets-engine").exists():
            specs.append((local / "docker-secrets-engine", local, {"engine.sock"}))
        for source, parent, entries in specs:
            result["backups"].append(quarantine(source, parent, entries, stamp))
        current_error = local / "Docker/backend.error.json"
        if current_error.exists():
            (out / "backend-error-before.json").write_bytes(current_error.read_bytes())
        before = processes("processes-before")
        if not args.apply:
            result["status"] = "preview_only"
            print(json.dumps(result["backups"], ensure_ascii=False, indent=2), flush=True)
            return 0
        # Engine startup is already failing; try the supported graceful stop first.
        command("desktop-graceful-stop", ["docker", "desktop", "stop", "--timeout", "25"], timeout=35, required=False)
        for row in processes("processes-after-graceful-stop"):
            pid = int(row["Id"])
            expected = row["Path"].replace("'", "''")
            powershell(f"stop-verified-process-{pid}",
                f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
                f"if($null -ne $p) {{ if($p.Path -ne '{expected}') {{ throw 'Process identity changed' }}; "
                "Stop-Process -InputObject $p -Force -ErrorAction Stop }")
        time.sleep(2)
        if processes("processes-before-rename"):
            raise RuntimeError("Docker processes remain or respawned; no directory was renamed")
        for i, (source, parent, entries) in enumerate(specs):
            result["backups"][i] = quarantine(source, parent, entries, stamp, apply=True)
            save()
        print("Runtime folders backed up. Starting installed Docker Desktop.", flush=True)
        with (out / "desktop-launch.txt").open("w", encoding="utf-8") as log:
            subprocess.Popen([str(DOCKER_HOME / "Docker Desktop.exe")], stdout=log, stderr=log,
                             startupinfo=None, creationflags=subprocess.CREATE_NO_WINDOW)
        ready = False
        for n in range(12):
            code, stdout = command(f"engine-probe-{n+1}", ["docker", "version", "--format", "{{json .Server}}"], timeout=8, required=False)
            if code == 0:
                server = json.loads(stdout)
                if isinstance(server, dict) and server.get("Version"):
                    result["server_version"] = server["Version"]
                    ready = True
                    break
            time.sleep(2)
        if not ready:
            raise RuntimeError("Docker engine is still unavailable after recovery; preserve new error for inspection")
        result["status"] = "engine_ready"
        if args.verify:
            # Acceptance output remains streamed; that script saves every individual command and result.
            code = subprocess.call([sys.executable, "-m", "scripts.verify_docker"], cwd=ROOT)
            result["verification_exit_code"] = code
            if code:
                raise RuntimeError("Engine started, but Docker acceptance failed; see latest verification evidence")
            result["status"] = "acceptance_passed"
        return 0
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    finally:
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save()
        (out / "record.md").write_text("# Docker运行时修复\n\n状态：" + result["status"] + "\n\n"
            + (result.get("error", "") + "\n\n")
            + "目录备份与命令退出码见recovery.json；未删除镜像、卷或WSL发行版，未上传日志。\n", encoding="utf-8")
        print(f"Recovery evidence: {out}", flush=True)


if __name__ == "__main__":
    sys.exit(main())

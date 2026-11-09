"""Host-terminal Docker UI recovery, with bounded local evidence and no data reset.

Default collects evidence only. --restart permits graceful stop, then stopping only
processes whose executable paths match the installed Docker components. No WSL
shutdown, directory rename, settings edits, volume deletion or diagnostic upload.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
INSTALL=Path(r'C:\Program Files\Docker\Docker')
ALLOWED={os.path.normcase(str(INSTALL/p)) for p in ('Docker Desktop.exe','frontend/Docker Desktop.exe','resources/com.docker.backend.exe')}
PIPE='npipe:////./pipe/dockerDesktopLinuxEngine'


def verified_processes(rows):
    for row in rows:
        if not row.get('Path') or os.path.normcase(row['Path']) not in ALLOWED or type(row.get('Id')) is not int:
            raise ValueError('Cannot verify installed Docker process; no forced stop allowed')
    return rows


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--restart',action='store_true');a=p.parse_args()
    out=ROOT/'docs/evidence/docker-stage'/datetime.now(timezone.utc).strftime('ui-recovery-%Y%m%dT%H%M%S%f')
    out.mkdir(parents=True);local=Path(os.environ['LOCALAPPDATA'])
    report={'status':'collecting','restart_requested':a.restart,'steps':[], 'local_appdata':str(local),
        'wsl_reset':False,'runtime_renamed':False,'volumes_deleted':False,'settings_changed':False}
    def save():(out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def command(label,argv,timeout=20):
        try:
            r=subprocess.run(argv,cwd=ROOT,capture_output=True,timeout=timeout)
            def decode(raw):return raw.decode('utf-16-le' if b'\x00' in raw else 'utf-8',errors='replace')
            code,stdout,stderr=r.returncode,decode(r.stdout),decode(r.stderr)
        except subprocess.TimeoutExpired:code,stdout,stderr=124,'','Timed out; partial output withheld'
        except OSError as exc:code,stdout,stderr=127,'',type(exc).__name__
        (out/(label+'.txt')).write_text(stdout+stderr,encoding='utf-8')
        report['steps'].append({'name':label,'exit_code':code});save();print(f'{label}: exit {code}',flush=True)
        return code,stdout
    def powershell(label,script):
        return command(label,['powershell.exe','-NoProfile','-Command',
            '[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); '+script])
    def inventory(label):
        code,text=powershell(label,"$rows=@(Get-Process | Where-Object { $_.ProcessName -eq 'Docker Desktop' -or $_.ProcessName -eq 'com.docker.backend' } | Select-Object Id,ProcessName,Path); ConvertTo-Json -InputObject $rows -Compress")
        if code:raise RuntimeError('Process inventory failed')
        rows=json.loads(text)
        if not isinstance(rows,list):raise ValueError('Invalid process inventory')
        return verified_processes(rows)
    def capture(label):
        target=out/label;target.mkdir()
        metadata=[]
        for rel in ('backend.error.json','log/host/Docker Desktop.exe.log','log/host/com.docker.backend.exe.log','log/host/monitor.log',
                    'log/host/electron-'+datetime.now().strftime('%Y-%m-%d')+'.log'):
            source=local/'Docker'/rel
            try:
                st=source.stat()
                with source.open('rb') as f:f.seek(max(0,st.st_size-64000));data=f.read()
                (target/source.name).write_bytes(data)
                metadata.append({'file':rel,'modified_utc':datetime.fromtimestamp(st.st_mtime,timezone.utc).isoformat(),'size':st.st_size})
            except OSError as exc:metadata.append({'file':rel,'error_type':type(exc).__name__})
        (target/'files.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
    save()
    try:
        if '#!sandbox.' in str(local).lower() or '\\packages\\sandbox.' in str(local).lower():
            report['status']='host_terminal_required'
            print('Run from your own Windows PowerShell; this process has redirected app data.',flush=True)
            return 1
        capture('before');inventory('processes-before')
        command('wsl-list',['wsl.exe','--list','--verbose'])
        powershell('services',"Get-Service -Name 'com.docker.service','WslService','LxssManager','vmcompute' -ErrorAction SilentlyContinue | Select-Object Name,Status,StartType | ConvertTo-Json")
        powershell('application-events',"Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=(Get-Date).AddHours(-2); Id=1000,1001,1026} -MaxEvents 30 -ErrorAction SilentlyContinue | Where-Object { $_.Message -match 'Docker|com.docker' } | Select-Object TimeCreated,Id,ProviderName,Message | ConvertTo-Json")
        code,text=command('engine-before',['docker','--host',PIPE,'version','--format','{{json .Server}}'])
        if not a.restart:report['status']='collected_only';return 0
        if code==0:
            # A functioning engine may own active workloads; don't stop it just to open its window.
            report['status']='engine_already_running_ui_unverified'
        else:
            command('desktop-graceful-stop',['docker','desktop','stop','--timeout','20'],timeout=25)
            for row in inventory('processes-after-graceful-stop'):
                pid=row['Id'];path=row['Path'].replace("'","''")
                result,_=powershell('stop-verified-'+str(pid),f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; if($null -ne $p) {{ if($p.Path -ne '{path}') {{ throw 'Process identity changed' }}; Stop-Process -InputObject $p -Force -ErrorAction Stop }}")
                if result:raise RuntimeError('Verified process stop failed')
            time.sleep(1)
            if inventory('processes-before-launch'):raise RuntimeError('Docker processes remain; stop for inspection')
            report['status']='starting'
        # The user explicitly needs a visible Desktop window.
        code,_=powershell('desktop-window-launch',"Start-Process -FilePath 'C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe' -WindowStyle Normal -ErrorAction Stop")
        if code:raise RuntimeError('Desktop launch failed')
        deadline=time.monotonic()+90;count=0
        while True:
            count+=1;code,text=command('engine-probe-'+str(count),['docker','--host',PIPE,'version','--format','{{json .Server}}'],timeout=8)
            if code==0 and isinstance(json.loads(text),dict) and json.loads(text).get('Version'):
                report['status']='engine_ready_ui_needs_visual_confirmation';report['engine']=json.loads(text);return 0
            if time.monotonic()>deadline:break
            time.sleep(3)
        report['status']='engine_unavailable';return 1
    except Exception as exc:report.update(status='failed',error_type=type(exc).__name__);return 1
    finally:
        if report['status']!='host_terminal_required':capture('after')
        report['finished_at_utc']=datetime.now(timezone.utc).isoformat();save()
        print('Status: '+report['status']);print('Evidence: '+str(out),flush=True)


if __name__=='__main__':raise SystemExit(main())

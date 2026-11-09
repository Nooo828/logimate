"""Small host/container DeepSeek probe using existing P3 image; no rebuild or model fallback."""
import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
# Runs unchanged on Windows host and inside the already-built Linux image.
PROBE_CODE='''import json,os,socket,ssl,time
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
r={'endpoint':'https://api.deepseek.com/chat/completions','model':os.environ.get('LOGIMATE_MODEL','deepseek-flash')}
start=time.monotonic()
try:
    body={'model':r['model'],'messages':[{'role':'user','content':'Reply only OK.'}],
          'thinking':{'type':'disabled'},'max_tokens':16,'stream':False}
    req=Request(r['endpoint'],data=json.dumps(body).encode(),headers={
        'Authorization':'Bearer '+os.environ['DEEPSEEK_API_KEY'],'Content-Type':'application/json'})
    with urlopen(req,timeout=25) as response:
        data=json.load(response); r.update(ok=True,http_status=response.status,
            response_id=data.get('id'),usage=data.get('usage',{}),actual_model=data.get('model'))
except HTTPError as e:
    # Provider error bodies can echo credentials. Never read or save them.
    r.update(ok=False,http_status=e.code,error_kind='provider_http')
except (URLError,OSError,TimeoutError) as e:
    reason=getattr(e,'reason',e)
    kind='tls' if isinstance(reason,ssl.SSLError) else 'dns' if isinstance(reason,socket.gaierror) else 'timeout' if isinstance(reason,TimeoutError) else 'network'
    r.update(ok=False,error_kind=kind,exception_type=type(reason).__name__,errno=getattr(reason,'errno',None))
except (ValueError,KeyError,TypeError):
    r.update(ok=False,error_kind='invalid_response_or_configuration')
r['elapsed_ms']=round((time.monotonic()-start)*1000,3)
print(json.dumps(r))
'''


def classify(host,container):
    for row in (host,container):
        if row.get('http_status')==401:return 'credential_rejected'
        if row.get('http_status')==402:return 'balance_or_billing_rejected'
        if row.get('http_status')==429:return 'provider_rate_limit'
        if row.get('http_status')==400:return 'provider_request_or_model_configuration'
        if row.get('http_status',0)>=500:return 'provider_http_failure'
    if host.get('ok') and container.get('ok'):return 'both_probes_passed_not_yet_full_acceptance'
    if host.get('ok') and not container.get('ok'):return 'container_path_failure_or_runtime_environment'
    if container.get('ok') and not host.get('ok'):return 'host_path_failure_or_runtime_environment'
    return 'both_paths_failed_inspect_error_kinds'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--then-verify',action='store_true',help='Run full P3 acceptance only when both small probes pass')
    args=parser.parse_args()
    candidates=[]
    for path in (ROOT/'docs/evidence/docker-stage').glob('*/summary.json'):
        info=json.loads(path.read_text(encoding='utf-8'))
        if info.get('acceptance_phase')!='P3-five-agent':continue
        for step in info.get('steps',[]):
            if step['name']=='image-health-ports' and step['exit_code']==0:
                candidates.append((path.parent/step['log'],info))
    if not candidates:raise RuntimeError('No existing P3 image evidence; no image will be guessed or built')
    source,info=sorted(candidates,key=lambda x:str(x[0]))[-1]
    image_info=json.loads(next(line for line in source.read_text(encoding='utf-8').splitlines() if line.startswith('{"Image":')))
    image=image_info['Image']
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',image):raise ValueError('Invalid image digest')
    key=(os.environ.get('DEEPSEEK_API_KEY') or getpass.getpass('DeepSeek API key (hidden, probe + optional verification only): ')).strip()
    if not key:raise RuntimeError('Empty key')
    env=dict(os.environ,DEEPSEEK_API_KEY=key,PYTHONIOENCODING='utf-8')
    out=ROOT/'docs/evidence/docker-stage'/datetime.now(timezone.utc).strftime('model-probe-%Y%m%dT%H%M%S%f')
    out.mkdir(parents=True,exist_ok=False)
    def probe(argv):
        try:
            p=subprocess.run(argv,cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=50)
            if p.returncode:
                return {'ok':False,'error_kind':'probe_process_failure','exit_code':p.returncode}
            row=json.loads(p.stdout)
            # Defense against an unexpected response ever including the key.
            if key in json.dumps(row):return {'ok':False,'error_kind':'sensitive_output_withheld'}
            return row
        except subprocess.TimeoutExpired:
            return {'ok':False,'error_kind':'probe_process_timeout'}
        except (ValueError,OSError):
            return {'ok':False,'error_kind':'probe_execution_or_output_error'}
    host=probe([sys.executable,'-c',PROBE_CODE])
    print('Host probe: '+json.dumps(host),flush=True)
    name='logimate-network-probe-'+uuid.uuid4().hex[:10]
    try:
        container=probe(['docker','run','--rm','--name',name,'--env','DEEPSEEK_API_KEY','--env','LOGIMATE_MODEL',image,'python','-c',PROBE_CODE])
    finally:
        # Remove only this probe if a CLI timeout left it running. No volumes are used.
        try:subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=10)
        except (OSError,subprocess.TimeoutExpired):pass
    print('Container probe: '+json.dumps(container),flush=True)
    report={'host':host,'container':container,'classification':classify(host,container),
            'image':image,'source_log':str(source.relative_to(ROOT)),
            'probe_code_sha256':hashlib.sha256(PROBE_CODE.encode()).hexdigest(),
            'note':'At most two tiny authenticated calls; no output text, credentials or provider error bodies saved.'}
    (out/'probe.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    (out/'record.md').write_text('# DeepSeek路径诊断\n\n结论：'+report['classification']+'\n\n仅说明两次小请求，不能代替P3完整验收。详情见probe.json。\n',encoding='utf-8')
    print('Evidence: '+str(out),flush=True)
    if not (host.get('ok') and container.get('ok')):return 1
    if args.then_verify:
        # Pass key through process environment, not command arguments or files.
        return subprocess.call([sys.executable,'-m','scripts.verify_docker','--p3','--wait-engine','30'],cwd=ROOT,env=env)
    return 0


if __name__=='__main__':
    raise SystemExit(main())

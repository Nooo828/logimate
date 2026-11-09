"""P7.1 own persistent-volume case container, no model keys, pulls or Milvus needed."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from logimate.retrieval import ROOT
from scripts.local_image_parent import validate_image, validate_child, file_probe
from scripts.smoke_cases import READ_TOKEN, REVIEW_TOKEN

GATE = 'docs/evidence/p6/shared-docker-20261104T052414384153'


def build_files(root, out, parent_tag, project, port):
    recipe=out/'Dockerfile.cases'; runtime=out/'compose.json'
    recipe.write_text('\n'.join(['FROM '+parent_tag,'USER 0','RUN mkdir -p /case-state && chown 10001:10001 /case-state',
        'WORKDIR /app','COPY requirements-retrieval.lock ./','COPY logimate ./logimate','COPY data ./data','COPY prompts ./prompts',
        'USER 10001','ENV LOGIMATE_RESILIENT_RETRIEVAL=0 LOGIMATE_CASE_DB=/case-state/cases.sqlite3',
        'CMD ["python","-m","uvicorn","logimate.api:app","--host","0.0.0.0","--port","8000"]','']),encoding='utf-8')
    config={'services':{'api':{'build':{'context':str(root),'dockerfile':str(recipe),'pull':False,'network':'none'},
        'image':project+'-api:verification','ports':[f'127.0.0.1:{port}:8000'],'environment':{'LOGIMATE_DEMO_TOKEN':READ_TOKEN,
        'LOGIMATE_REVIEW_TOKEN':REVIEW_TOKEN,'LOGIMATE_RESILIENT_RETRIEVAL':'0','LOGIMATE_CASE_DB':'/case-state/cases.sqlite3'},
        'volumes':['case-data:/case-state']}},'volumes':{'case-data':{}}}
    runtime.write_text(json.dumps(config,indent=2),encoding='utf-8')
    return recipe,runtime


def parent_plan(root):
    gate=json.loads((root/GATE/'summary.json').read_text(encoding='utf-8'))
    if (gate['status']!='passed' or not gate['cleanup_passed'] or not gate['image_credentials_absent'] or
            len(gate['steps'])!=39 or any(s['exit_code']!=0 for s in gate['steps'])):
        raise ValueError('P6.2 actual container gate missing')
    if hashlib.sha256((root/'requirements-retrieval.lock').read_bytes()).hexdigest()!=gate['input_sha256']['requirements-retrieval.lock']:
        raise ValueError('dependency lock changed; cannot reuse P6.2 dependencies')
    for name in ('data/synthetic_v1.json','data/knowledge/corpus-v1.json','data/knowledge/index-v1.json','data/knowledge/qa-v1.json'):
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=gate['input_sha256'][name]: raise ValueError('frozen data changed')
    tag=gate['project']+'-api:verification'
    rows=json.loads((root/GATE/'service-images.txt').read_text(encoding='utf-8'))
    images=[v for v in rows if v['Repository']+':'+v['Tag']==tag]
    if len(images)!=1: raise ValueError('missing parent image identity')
    return {'tag':tag,'id':images[0]['ID'],'file_sha256':{n:h for n,h in gate['input_sha256'].items()
        if n.startswith('logimate/') or n=='requirements-retrieval.lock'}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desktop-linux',action='store_true')
    parser.add_argument('--port',type=int,default=18085,choices=range(1,65536),metavar='PORT')
    args=parser.parse_args();stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    out=ROOT/'docs/evidence/p7'/('docker-'+stamp);out.mkdir(parents=True)
    project='logimate-p71-'+stamp.lower();image=project+'-api:verification'
    docker=['docker']+(['--host','npipe:////./pipe/dockerDesktopLinuxEngine'] if args.desktop_linux else [])
    runtime=out/'compose.json';prefix=docker+['compose','-p',project,'-f',str(runtime)]
    env=dict(os.environ,PYTHONIOENCODING='utf-8',BUILDX_BUILDER='default')
    report={'status':'running','phase':'P7.1-durable-review-cases','project':project,'real_llm_calls':False,
        'native_langgraph_checkpoint':False,'steps':[],'results':{},'input_sha256':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
        for pattern in ('logimate/*.py','scripts/*case*.py','scripts/local_image_parent.py','data/synthetic_v1.json','requirements-retrieval.lock') for p in ROOT.glob(pattern)}}
    def save(): (out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def command(name,argv,timeout=30):
        report['active_stage']=name;save()
        try:
            p=subprocess.run(argv,cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
            code,output=p.returncode,p.stdout+p.stderr
        except subprocess.TimeoutExpired: code,output=124,'Command timed out; private process output withheld'
        except OSError as e: code,output=127,type(e).__name__
        (out/(name+'.txt')).write_text(output,encoding='utf-8')
        report['steps'].append({'name':name,'exit_code':code});save();print(name+': exit '+str(code),flush=True)
        if code: raise RuntimeError(name+' failed')
        return p.stdout
    def smoke(name,mode):
        argv=[sys.executable,'-m','scripts.smoke_cases','--base',f'http://127.0.0.1:{args.port}','--mode',mode]
        if report.get('case_id'):argv+=['--case-id',report['case_id']]
        result=json.loads(command(name,argv));report['results'][name]=result
        report['case_id']=result['case_id'];save()
    ready=created=False;save()
    try:
        parent=parent_plan(ROOT);report['parent']=parent;save()
        command('engine-preflight',docker+['version','--format','{{json .Server}}']);ready=True
        actual=json.loads(command('parent-local-image',docker+['image','inspect',parent['tag']]))[0]
        layers=validate_image(actual,parent)
        builder=command('builder-driver',docker+['buildx','inspect','default'])
        if not any(v.startswith('Driver:') and v.split(':',1)[1].strip()=='docker' for v in builder.splitlines()): raise ValueError('Docker builder required')
        command('parent-file-provenance',docker+['run','--rm','--pull=never','--network=none','--read-only','--cap-drop=ALL',
            '--security-opt=no-new-privileges','--entrypoint','python',parent['tag'],'-c',file_probe(parent['file_sha256'])])
        recipe,runtime=build_files(ROOT,out,parent['tag'],project,args.port)
        report['generated_dockerfile_sha256']=hashlib.sha256(recipe.read_bytes()).hexdigest();save()
        command('compose-config',prefix+['config','--quiet'])
        command('image-build',prefix+['--progress','plain','build','api'],timeout=600)
        child=json.loads(command('child-image',docker+['image','inspect',image]))[0];validate_child(child,layers)
        if any(v.startswith('DEEPSEEK_API_KEY=') for v in child['Config']['Env']):raise ValueError('model key in image')
        hashes={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for pattern in ('logimate/*.py','data/**/*.json','prompts/*.txt','requirements-retrieval.lock') for p in ROOT.glob(pattern)}
        report['child_file_sha256']=hashes;save()
        command('child-file-provenance',docker+['run','--rm','--pull=never','--network=none','--read-only','--entrypoint','python',image,'-c',file_probe(hashes)])
        created=True
        command('api-start',prefix+['up','-d','--wait','--wait-timeout','60'],timeout=90)
        command('api-nonroot',prefix+['exec','-T','api','python','-c','import os; assert os.getuid()!=0; print(os.getuid())'])
        smoke('create-and-guards','create')
        command('api-restart',prefix+['restart','api'],timeout=60)
        command('restart-ready',prefix+['up','-d','--wait','--wait-timeout','60'],timeout=90)
        smoke('waiting-after-restart','paused')
        smoke('manual-decision','decide')
        before=command('container-id-before',prefix+['ps','-q','api']).strip()
        command('api-recreate',prefix+['up','-d','--force-recreate','--wait','--wait-timeout','60'],timeout=90)
        after=command('container-id-after',prefix+['ps','-q','api']).strip()
        if not before or not after or before==after:raise ValueError('container not recreated')
        report['different_container_same_volume']=True;save()
        smoke('reviewed-after-recreate','reviewed')
        command('service-images',prefix+['images','--format','json'])
        command('container-state',prefix+['ps','--format','json'])
        report['status']='passed'
    except Exception as exc:report.update(status='failed' if ready else 'blocked',error_type=type(exc).__name__)
    finally:
        if created:
            try:command('container-logs',prefix+['logs','--no-color'])
            except Exception:pass
            try:command('cleanup-own-project',prefix+['down'],timeout=90);report['cleanup_passed']=True
            except Exception:report.update(status='failed',cleanup_passed=False)
        report['finished_at_utc']=datetime.now(timezone.utc).isoformat();save()
        print('Status: '+report['status']);print('Evidence: '+str(out))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())

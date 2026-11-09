"""P7.3 offline child image; actual MCP fixture + optional paid analysis and durable native review."""
import argparse
import getpass
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from logimate.retrieval import ROOT, cache_dir
from scripts.local_image_parent import validate_image, validate_child, file_probe
from scripts.smoke_cases import READ_TOKEN, REVIEW_TOKEN

GATE='docs/evidence/p7/docker-20261104T105300541208'


def parent_plan(root):
    gate=json.loads((root/GATE/'summary.json').read_text(encoding='utf-8'))
    if (gate['status']!='passed' or not gate['cleanup_passed'] or not gate['different_container_same_volume'] or
        not gate['same_native_checkpoint_after_restart'] or len(gate['steps'])!=24 or any(s['exit_code'] for s in gate['steps'])):
        raise ValueError('P7.2 accepted container gate missing')
    for name in ('requirements-retrieval.lock','requirements-checkpoint.lock'):
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=gate['input_sha256'][name]:raise ValueError('accepted dependency lock changed')
    for name,sha in gate['child_file_sha256'].items():
        if name.startswith('data/') and hashlib.sha256((root/name).read_bytes()).hexdigest()!=sha:raise ValueError('frozen data changed')
    tag=gate['project']+'-api:verification'
    rows=json.loads((root/GATE/'service-images.txt').read_text(encoding='utf-8'))
    images=[v for v in rows if v['Repository']+':'+v['Tag']==tag]
    if len(images)!=1:raise ValueError('recorded parent image not found')
    return {'tag':tag,'id':images[0]['ID'],'file_sha256':gate['child_file_sha256']}


def build_files(root,out,parent_tag,project,port):
    recipe=out/'Dockerfile.analysis';runtime=out/'compose.json'
    recipe.write_text('\n'.join(['FROM '+parent_tag,'WORKDIR /app',
        'COPY logimate ./logimate','COPY data ./data','COPY prompts ./prompts','COPY scripts ./scripts',
        'COPY requirements-retrieval.lock requirements-checkpoint.lock ./',
        'USER 10001','CMD ["python","-m","uvicorn","logimate.api:app","--host","0.0.0.0","--port","8000"]','']),encoding='utf-8')
    config={'services':{'api':{'build':{'context':str(root),'dockerfile':str(recipe),'pull':False,'network':'none'},
        'image':project+'-api:verification','ports':[f'127.0.0.1:{port}:8000'],
        'environment':{'LOGIMATE_DEMO_TOKEN':READ_TOKEN,'LOGIMATE_REVIEW_TOKEN':REVIEW_TOKEN,
            'LOGIMATE_NATIVE_REVIEW':'1','LOGIMATE_ANALYSIS_REVIEW':'1','LOGIMATE_CASE_DB':'/case-state/cases.sqlite3',
            'LOGIMATE_RESILIENT_RETRIEVAL':'1','LOGIMATE_DENSE_BACKEND':'exact',
            'LOGIMATE_RETRIEVAL_CACHE':'/models','LOGIMATE_LEXICAL_CACHE':'/tmp/lexical'},
        'volumes':['case-data:/case-state',{'type':'bind','source':str(cache_dir().resolve()),'target':'/models','read_only':True}]}},
        'volumes':{'case-data':{}}}
    runtime.write_text(json.dumps(config,indent=2),encoding='utf-8')
    return recipe,runtime,config


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desktop-linux',action='store_true');parser.add_argument('--live',action='store_true')
    parser.add_argument('--port',type=int,default=18087,choices=range(1,65536),metavar='PORT')
    args=parser.parse_args();stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    out=ROOT/'docs/evidence/p7'/('analysis-docker-'+stamp);out.mkdir(parents=True)
    project='logimate-p73-'+stamp.lower();image=project+'-api:verification'
    docker=['docker']+(['--host','npipe:////./pipe/dockerDesktopLinuxEngine'] if args.desktop_linux else [])
    prefix=docker+['compose','-p',project,'-f',str(out/'compose.json')]
    env=dict(os.environ,PYTHONIOENCODING='utf-8',BUILDX_BUILDER='default');env.pop('DEEPSEEK_API_KEY',None)
    report={'status':'running','phase':'P7.3-analysis-receipt-native-review','project':project,'live_requested':args.live,
        'steps':[],'results':{},'input_sha256':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for pattern in ('logimate/*.py','scripts/*.py','data/**/*.json','prompts/*.txt','requirements*.lock') for p in ROOT.glob(pattern)}}
    secret=''
    def save():(out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def command(name,argv,timeout=30):
        report['active_stage']=name;save()
        try:
            p=subprocess.run(argv,cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
            code,output=p.returncode,p.stdout+p.stderr
            if secret and secret in output:output='Sensitive output withheld';code=125
        except subprocess.TimeoutExpired:code,output=124,'Command timed out; private process output withheld'
        except OSError as exc:code,output=127,type(exc).__name__
        (out/(name+'.txt')).write_text(output,encoding='utf-8')
        report['steps'].append({'name':name,'exit_code':code});save();print(name+': exit '+str(code),flush=True)
        if code:raise RuntimeError(name+' failed')
        return p.stdout
    def smoke(name,mode):
        argv=[sys.executable,'-m','scripts.smoke_analysis_review','--base',f'http://127.0.0.1:{args.port}','--mode',mode]
        if mode=='create':argv+=['--live']
        else:argv+=['--analysis-id',report['analysis_id']]
        result=json.loads(command(name,argv,timeout=150));report['results'][name]=result
        report['analysis_id']=result['analysis_id'];save();return result
    ready=created=False;save()
    try:
        parent=parent_plan(ROOT);report['parent']=parent;save()
        command('engine-preflight',docker+['version','--format','{{json .Server}}']);ready=True
        actual=json.loads(command('parent-local-image',docker+['image','inspect',parent['tag']]))[0]
        layers=validate_image(actual,parent)
        builder=command('builder-driver',docker+['buildx','inspect','default'])
        if not any(v.startswith('Driver:') and v.split(':',1)[1].strip()=='docker' for v in builder.splitlines()):raise ValueError('Docker builder required')
        command('parent-file-provenance',docker+['run','--rm','--pull=never','--network=none','--read-only','--entrypoint','python',parent['tag'],'-c',file_probe(parent['file_sha256'])])
        recipe,runtime,config=build_files(ROOT,out,parent['tag'],project,args.port)
        report['generated_dockerfile_sha256']=hashlib.sha256(recipe.read_bytes()).hexdigest();save()
        command('compose-config',prefix+['config','--quiet'])
        command('image-build',prefix+['--progress','plain','build','api'],timeout=600)
        child=json.loads(command('child-image',docker+['image','inspect',image]))[0];validate_child(child,layers)
        if any(v.startswith('DEEPSEEK_API_KEY=') for v in child['Config']['Env']):raise ValueError('model key in image')
        hashes={n:h for n,h in report['input_sha256'].items() if n.startswith(('logimate/','scripts/','data/','prompts/')) or n in ('requirements-retrieval.lock','requirements-checkpoint.lock')}
        report['child_file_sha256']=hashes;save()
        command('child-file-provenance',docker+['run','--rm','--pull=never','--network=none','--read-only','--entrypoint','python',image,'-c',file_probe(hashes)])
        if args.live:
            secret=(os.environ.get('DEEPSEEK_API_KEY') or getpass.getpass('DeepSeek API key (hidden, one P7.3 analysis only): ')).strip()
            if not secret:raise ValueError('empty model key')
            env['DEEPSEEK_API_KEY']=secret
            config['services']['api']['environment'].update(DEEPSEEK_API_KEY='${DEEPSEEK_API_KEY:?required}',LOGIMATE_MODEL='${LOGIMATE_MODEL:-deepseek-flash}')
            runtime.write_text(json.dumps(config,indent=2),encoding='utf-8')
        command('runtime-compose-config',prefix+['config','--quiet'])
        created=True;command('api-start',prefix+['up','-d','--wait','--wait-timeout','60'],timeout=90)
        command('api-nonroot',prefix+['exec','-T','api','python','-c','import os; assert os.getuid()!=0; print(os.getuid())'])
        # A separate trusted fixture API uses real CPU workers and MCP; never claims live model evidence.
        fixture=json.loads(command('fixture-mcp-native-lifecycle',prefix+['exec','-T','-e','DEEPSEEK_API_KEY=','api','python','-m','scripts.probe_analysis_review_http','--stdout'],timeout=180))
        assert fixture['status']=='passed' and fixture['real_mcp_stdio'] and fixture['real_cpu_embeddings'] and not fixture['real_llm_calls']
        report['results']['fixture']=fixture;save()
        if args.live:
            # Wait for the main API's independent workers; health contains no credentials.
            warm="import json,time; from urllib.request import Request,urlopen; deadline=time.monotonic()+30\nwhile True:\n r=json.load(urlopen(Request('http://127.0.0.1:8000/v1/knowledge/resilient/health',headers={'Authorization':'Bearer '+"+repr(READ_TOKEN)+"}),timeout=5))\n if r.get('primary',{}).get('ready') and r.get('keyword',{}).get('ready'):break\n if time.monotonic()>deadline:raise TimeoutError('CPU warmup')\n time.sleep(.1)\nprint('ready')"
            command('analysis-workers-ready',prefix+['exec','-T','api','python','-c',warm],timeout=40)
            first=smoke('live-analysis-create','create');report['real_model_responses']=first['new_provider_responses'];report['real_model_tokens']=first['new_provider_tokens']
            report['initial_checkpoint_id']=first['view']['thread']['checkpoint_id'];save()
            command('api-restart',prefix+['restart','api'],timeout=60)
            command('restart-ready',prefix+['up','-d','--wait','--wait-timeout','60'],timeout=90)
            before=command('container-id-before',prefix+['ps','-q','api']).strip()
            settings=config['services']['api']['environment'];settings.pop('DEEPSEEK_API_KEY');settings.pop('LOGIMATE_MODEL');settings['LOGIMATE_RESILIENT_RETRIEVAL']='0'
            runtime.write_text(json.dumps(config,indent=2),encoding='utf-8');env.pop('DEEPSEEK_API_KEY',None)
            command('api-recreate-no-model',prefix+['up','-d','--force-recreate','--wait','--wait-timeout','60'],timeout=90)
            after=command('container-id-after',prefix+['ps','-q','api']).strip()
            if not before or not after or before==after:raise ValueError('container not recreated')
            report['different_container_same_volume']=True
            command('model-and-retrieval-disabled',prefix+['exec','-T','api','python','-c',"import os; assert not os.environ.get('DEEPSEEK_API_KEY'); assert os.environ['LOGIMATE_RESILIENT_RETRIEVAL']=='0'; print('disabled')"])
            disabled="from scripts.smoke_analysis_review import call,PATH,QUERY; base='http://127.0.0.1:8000'; call(base,PATH,{'query':QUERY},key='disabled-analysis-0001',expected=503); call(base,'/v1/analysis-reviews/by-key/disabled-analysis-0001',expected=404); print('new analysis fails closed without recording an external attempt')"
            command('new-analysis-disabled-fails-closed',prefix+['exec','-T','api','python','-c',disabled])
            result=smoke('resume-without-model-or-retrieval','resume')
            # The replayed POST is the original binding receipt, independently of subsequent review state.
            cached=[r['body'] for r in result['checks'] if r['body'].get('replayed') and 'receipt_sha256' in r['body'] and r['body'].get('thread',{}).get('version')==1]
            assert cached and cached[0]['thread']['checkpoint_id']==report['initial_checkpoint_id']
            report['same_native_checkpoint_after_restart']=True;report['resume_without_model_or_retrieval']=True;save()
            command('reviewed-api-recreate',prefix+['up','-d','--force-recreate','--wait','--wait-timeout','60'],timeout=90)
            smoke('reviewed-after-recreate','reviewed')
            report['status']='passed'
        else:report['status']='fixture_passed_live_pending'
        command('service-images',prefix+['images','--format','json']);command('container-state',prefix+['ps','--format','json'])
    except Exception as exc:report.update(status='failed' if ready else 'blocked',error_type=type(exc).__name__)
    finally:
        if created:
            try:command('container-logs',prefix+['logs','--no-color'])
            except Exception:pass
            try:command('cleanup-own-project',prefix+['down'],timeout=90);report['cleanup_passed']=True
            except Exception:report.update(status='failed',cleanup_passed=False)
        report['finished_at_utc']=datetime.now(timezone.utc).isoformat();save()
        print('Status: '+report['status']);print('Evidence: '+str(out))
    return 0 if report['status'] in ('passed','fixture_passed_live_pending') else 1


if __name__=='__main__':raise SystemExit(main())

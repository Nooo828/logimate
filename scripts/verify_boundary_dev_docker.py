"""P8.5 new dev-only image; fixture pipeline before optional paid development collection."""
import argparse,getpass,hashlib,json,os,subprocess,sys
from datetime import datetime,timezone
from logimate.retrieval import ROOT,cache_dir
from scripts.local_image_parent import validate_image,validate_child,file_probe

GATE='docs/evidence/p8/paired-docker-20261105T074259913219'


def parent_plan(root):
    r=json.loads((root/GATE/'summary.json').read_text(encoding='utf-8'))
    if r['status']!='passed' or len(r['steps'])!=21 or any(s['exit_code'] for s in r['steps']):raise ValueError('P8.2 actual gate missing')
    if not all(r[k] for k in ('cleanup_passed','restart_ledger_preserved','evidence_exported','exported_artifact_hashes_verified')):raise ValueError('P8.2 persistence/export gate missing')
    for n,h in r['child_file_sha256'].items():
        if n.startswith(('data/','prompts/')) or n in ('requirements-retrieval.lock','requirements-checkpoint.lock'):
            if hashlib.sha256((root/n).read_bytes()).hexdigest()!=h:raise ValueError('frozen runtime input changed')
    protocol=root/'docs/evidence/p8/boundary-protocol-20261105T112755164504/protocol.json'
    frozen=json.loads(protocol.read_text(encoding='utf-8'))
    lock=json.loads((root/'eval/boundary_v2.freeze.json').read_text(encoding='utf-8'))
    if lock['protocol_sha256']!=hashlib.sha256(protocol.read_bytes()).hexdigest() or lock['sources']!=frozen['input_sha256']:raise ValueError('portable lock differs from published P8.4 freeze')
    from logimate.dev_collection import select_dev
    _,selected=select_dev(root)
    if len(selected)!=16:raise ValueError('dev-only selection failed')
    tag=r['project']+'-worker:verification'
    images=json.loads((root/GATE/'service-images.txt').read_text(encoding='utf-8'));selected=[v for v in images if v['Repository']+':'+v['Tag']==tag]
    if len(selected)!=1:raise ValueError('missing recorded parent identity')
    return {'tag':tag,'id':selected[0]['ID'],'file_sha256':r['child_file_sha256']}


def build_files(root,out,parent,project):
    recipe=out/'Dockerfile.eval';runtime=out/'compose.json'
    recipe.write_text('\n'.join(['FROM '+parent,'USER 0','RUN mkdir -p /evidence && chown 10001:10001 /evidence','WORKDIR /app',
        'COPY logimate ./logimate','COPY scripts ./scripts','COPY eval ./eval','COPY data ./data','COPY prompts ./prompts',
        'COPY requirements-retrieval.lock requirements-checkpoint.lock ./','USER 10001',
        'CMD ["python","-c","import time; time.sleep(86400)"]','']),encoding='utf-8')
    config={'services':{'worker':{'build':{'context':str(root),'dockerfile':str(recipe),'network':'none','pull':False},
        'image':project+'-worker:verification','healthcheck':{'disable':True},'environment':{
            'LOGIMATE_CASE_DB':'','LOGIMATE_NATIVE_REVIEW':'0','LOGIMATE_ANALYSIS_REVIEW':'0','LOGIMATE_RESILIENT_RETRIEVAL':'0',
            'LOGIMATE_DENSE_BACKEND':'exact','LOGIMATE_RETRIEVAL_CACHE':'/models'},
        'volumes':['eval-data:/evidence',{'type':'bind','source':str(cache_dir().resolve()),'target':'/models','read_only':True}]}},'volumes':{'eval-data':{}}}
    runtime.write_text(json.dumps(config,indent=2),encoding='utf-8');return recipe,runtime


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--desktop-linux',action='store_true');p.add_argument('--live',action='store_true')
    p.add_argument('--split',choices=['dev'],default='dev');p.add_argument('--max-attempts',type=int,default=120,choices=range(1,201));p.add_argument('--max-tokens',type=int,default=60000,choices=range(1,200001));a=p.parse_args()
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f');out=ROOT/'docs/evidence/p8'/('boundary-dev-docker-'+stamp);out.mkdir(parents=True)
    project='logimate-p85-'+stamp.lower();image=project+'-worker:verification'
    docker=['docker']+(['--host','npipe:////./pipe/dockerDesktopLinuxEngine'] if a.desktop_linux else [])
    prefix=docker+['compose','-p',project,'-f',str(out/'compose.json')]
    env=dict(os.environ,PYTHONIOENCODING='utf-8',BUILDX_BUILDER='default');env.pop('DEEPSEEK_API_KEY',None);secret=''
    report={'phase':'P8.5-boundary-development','split':'dev','quality_threshold_enforced':False,'status':'running','project':project,'live_requested':a.live,'steps':[],
        'input_sha256':{q.relative_to(ROOT).as_posix():hashlib.sha256(q.read_bytes()).hexdigest()
            for pattern in ('logimate/*.py','scripts/*.py','eval/*.json','data/**/*.json','prompts/*.txt','requirements*.lock') for q in ROOT.glob(pattern)}}
    def save():(out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def command(name,argv,timeout=30):
        report['active_stage']=name;save()
        try:
            r=subprocess.run(argv,cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
            code,output=r.returncode,r.stdout+r.stderr
            if secret and secret in output:code,output=125,'Sensitive output withheld'
        except subprocess.TimeoutExpired:code,output=124,'Timed out; partial private process output withheld'
        except OSError as exc:code,output=127,type(exc).__name__
        (out/(name+'.txt')).write_text(output,encoding='utf-8');report['steps'].append({'name':name,'exit_code':code});save();print(f'{name}: exit {code}',flush=True)
        if code:raise RuntimeError(name+' failed')
        return r.stdout
    def copied_summary(mode):
        # Read summaries inside the owned volume, before and after restart; no new model calls.
        code="import json; from pathlib import Path; r=json.loads(Path('/evidence/"+mode+"/summary.json').read_text()); print(json.dumps({k:v for k,v in r.items() if k!='input_sha256'}))"
        return json.loads(command(mode+'-summary-'+str(len(report['steps'])),prefix+['exec','-T','worker','python','-c',code]))
    ready=created=False;save()
    try:
        parent=parent_plan(ROOT);report['parent']=parent;save()
        command('engine-preflight',docker+['version','--format','{{json .Server}}']);ready=True
        actual=json.loads(command('parent-local-image',docker+['image','inspect',parent['tag']]))[0];layers=validate_image(actual,parent)
        builder=command('builder-driver',docker+['buildx','inspect','default'])
        if not any(v.startswith('Driver:') and v.split(':',1)[1].strip()=='docker' for v in builder.splitlines()):raise ValueError('Docker builder required')
        command('parent-file-provenance',docker+['run','--rm','--pull=never','--network=none','--read-only','--entrypoint','python',parent['tag'],'-c',file_probe(parent['file_sha256'])])
        recipe,runtime=build_files(ROOT,out,parent['tag'],project)
        report['dockerfile_sha256']=hashlib.sha256(recipe.read_bytes()).hexdigest();command('compose-config',prefix+['config','--quiet'])
        command('image-build',prefix+['--progress','plain','build','worker'],timeout=600)
        child=json.loads(command('child-image',docker+['image','inspect',image]))[0];validate_child(child,layers)
        if any(v.startswith('DEEPSEEK_API_KEY=') for v in child['Config']['Env']):raise ValueError('image contains model credential')
        report['child_file_sha256']={n:h for n,h in report['input_sha256'].items() if n in ('requirements-retrieval.lock','requirements-checkpoint.lock') or n.startswith(('logimate/','scripts/','eval/','data/','prompts/'))};save()
        command('child-file-provenance',docker+['run','--rm','--pull=never','--network=none','--read-only','--entrypoint','python',image,'-c',file_probe(report['child_file_sha256'])])
        created=True;command('worker-start',prefix+['up','-d'],timeout=60)
        command('worker-nonroot',prefix+['exec','-T','worker','python','-c','import os; assert os.getuid()!=0; print(os.getuid())'])
        command('fixture-three-profiles',prefix+['exec','-T','-e','DEEPSEEK_API_KEY=','worker','python','-m','scripts.collect_boundary_dev','--output','/evidence/fixture'],timeout=240)
        fixture=copied_summary('fixture');report['fixture']=fixture
        if fixture['status']!='collected' or not fixture['scores_complete'] or any(n!=16 for n in fixture['task_matches'].values()) or any(fixture['boundary_failures'].values()) or not fixture['dev_only_verified'] or fixture['non_dev_attempts_detected'] or fixture['holdout_target_model_calls'] or fixture['known_provider_responses'] or fixture['known_tokens'] or fixture['shared_policy_degraded_cells']:raise ValueError('fixture pipeline acceptance failed')
        if a.live:
            secret=(os.environ.get('DEEPSEEK_API_KEY') or getpass.getpass('DeepSeek API key (hidden, 48 dev-only executions, holdout disabled, request limit '+str(a.max_attempts)+'): ')).strip()
            if not secret:raise ValueError('empty key')
            env['DEEPSEEK_API_KEY']=secret
            command('live-three-profiles',prefix+['exec','-T','-e','DEEPSEEK_API_KEY','-e','LOGIMATE_MODEL='+os.environ.get('LOGIMATE_MODEL','deepseek-flash'),
                'worker','python','-m','scripts.collect_boundary_dev','--live','--output','/evidence/live','--max-attempts',str(a.max_attempts),'--max-tokens',str(a.max_tokens)],timeout=1500)
            live=copied_summary('live');report['live']=live
            if live['status']!='collected' or not live['scores_complete'] or not live['billing_is_complete'] or not live['dev_only_verified'] or live['non_dev_attempts_detected'] or live['holdout_target_model_calls'] or live['unverified_usage_rows']:raise ValueError('live collection incomplete')
            env.pop('DEEPSEEK_API_KEY',None)
        command('worker-restart',prefix+['restart','worker'],timeout=60)
        mode='live' if a.live else 'fixture';before=report[mode]
        after=copied_summary(mode)
        if before!=after:raise ValueError('stored summary changed after restart')
        command('read-only-ledger-after-restart',prefix+['exec','-T','worker','python','-m','scripts.inspect_paired_eval','/evidence/'+mode+'/ledger.sqlite3'])
        report['restart_ledger_preserved']=True
        command('service-images',prefix+['images','--format','json']);report['status']='passed' if a.live else 'fixture_passed_live_pending'
    except Exception as exc:report.update(status='failed' if ready else 'blocked',error_type=type(exc).__name__)
    finally:
        if created:
            target=out/'runtime';target.mkdir(exist_ok=True)
            try:
                command('export-owned-evidence',prefix+['cp','worker:/evidence/.',str(target)],timeout=90);report['evidence_exported']=True
                for mode in ('fixture','live') if a.live else ('fixture',):
                    summaries=[q for q in target.rglob('summary.json') if q.parent.name==mode]
                    if len(summaries)!=1:raise ValueError('missing exported mode summary')
                    data=json.loads(summaries[0].read_text(encoding='utf-8'))
                    for name,sha in data['output_sha256'].items():
                        if hashlib.sha256((summaries[0].parent/name).read_bytes()).hexdigest()!=sha:raise ValueError('exported artifact hash mismatch')
                report['exported_artifact_hashes_verified']=True
            except Exception:report.update(status='failed',evidence_exported=False)
            try:command('worker-logs',prefix+['logs','--no-color'])
            except Exception:pass
            try:command('cleanup-own-project',prefix+['down'],timeout=90);report['cleanup_passed']=True
            except Exception:report.update(status='failed',cleanup_passed=False)
        report['finished_at_utc']=datetime.now(timezone.utc).isoformat();save()
        print('Status: '+report['status']);print('Evidence: '+str(out),flush=True)
    return 0 if report['status'] in ('passed','fixture_passed_live_pending') else 1


if __name__=='__main__':raise SystemExit(main())

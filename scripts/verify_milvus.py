"""Actual isolated Milvus acceptance; exact-baseline comparison before/after restart."""
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from logimate.retrieval import ROOT, KnowledgeSearch, load_encoder
from logimate.milvus_backend import MilvusANN


def docker_commands(project, out, direct_registry=False, desktop_linux=False, embedded=False):
    """Scoped image-source override; never edits the daemon's mirror settings."""
    docker=['docker']
    if desktop_linux:
        docker+=['--host','npipe:////./pipe/dockerDesktopLinuxEngine']
    compose_file='compose.milvus.embedded.json' if embedded else 'compose.milvus.json'
    prefix=docker+['compose','-p',project,'-f',compose_file]
    config=json.loads((ROOT/compose_file).read_text(encoding='utf-8'))
    images={name:service['image'] for name,service in config['services'].items()}
    if direct_registry:
        for name in ('minio','milvus'):
            if name not in images:continue
            # Keep the same repository/version; explicitly address Docker's registry.
            images[name]='registry-1.docker.io/'+images[name]
        override=out/'official-registry.json'
        override.write_text(json.dumps({'services':{name:{'image':images[name]} for name in ('minio','milvus') if name in images}},indent=2),encoding='utf-8')
        prefix+=['-f',str(override)]
    return docker,prefix,images


def compare(exact, remote):
    qa=json.loads((ROOT/'data/knowledge/qa-v1.json').read_text(encoding='utf-8'))
    rows=[]
    for q in [q for q in qa['cases'] if q['split']=='holdout']:
        for mode in ['dense','hybrid']:
            a=exact.search(q['query'],'demo-a',mode=mode);b=remote.search(q['query'],'demo-a',mode=mode)
            ai=[c['chunk_id'] for c in a['hits']];bi=[c['chunk_id'] for c in b['hits']]
            assert set(ai)==set(bi),(q['id'],mode,'ANN/exact Top5 difference')
            rows.append({'query_id':q['id'],'mode':mode,'exact_ids':ai,'milvus_ids':bi,'same_set':True,'same_order':ai==bi})
    for tenant,domain in [('demo-a','warehouse'),('demo-b',None)]:
        a=exact.search('退货退款',tenant,mode='dense',domain=domain)
        b=remote.search('退货退款',tenant,mode='dense',domain=domain)
        assert {c['chunk_id'] for c in a['hits']}=={c['chunk_id'] for c in b['hits']}
        rows.append({'tenant':tenant,'domain':domain,'same_set':True})
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--direct-registry',action='store_true',help='Explicit official registry hostname for Hub images; no daemon settings changes')
    parser.add_argument('--desktop-linux',action='store_true',help='Connect to the known Windows Docker Desktop Linux pipe')
    parser.add_argument('--embedded',action='store_true',help='Separate real Milvus standalone profile with embedded etcd and local storage; does not test MinIO/S3')
    args=parser.parse_args()
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    out=ROOT/'docs/evidence/p5'/('milvus-'+stamp);out.mkdir(parents=True)
    project='logimate-milvus-'+stamp.lower()
    docker,prefix,images=docker_commands(project,out,args.direct_registry,args.desktop_linux,args.embedded)
    report={'status':'running','simulated_business_data':True,'real_llm_calls':False,'steps':[],
            'project':project,'direct_registry':args.direct_registry,'effective_images':images,
            'deployment_mode':'embedded-etcd-local-storage' if args.embedded else 'external-etcd-minio',
            'minio_s3_tested':False,
            'input_sha256':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for pattern in ['logimate/*.py','scripts/verify_milvus.py','compose.milvus*.json','deploy/milvus-embed-*.yaml','data/knowledge/*.json'] for p in ROOT.glob(pattern)}}
    def save():(out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def run(label,args,timeout=30):
        report['active_stage']=label;save()
        try:
            p=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
            code,output=p.returncode,p.stdout+p.stderr
        except subprocess.TimeoutExpired as exc:
            def decode(value):return value.decode('utf-8',errors='replace') if isinstance(value,bytes) else value or ''
            code,output=124,decode(exc.stdout)+decode(exc.stderr)+f'\nTimeout after {timeout}s'
        except OSError as exc:
            code,output=127,str(exc)
        (out/(label+'.txt')).write_text(output,encoding='utf-8')
        report['steps'].append({'name':label,'exit_code':code});save()
        print(label+': exit '+str(code),flush=True)
        if code:raise RuntimeError(label+' failed')
    created=False
    try:
        gate=json.loads((ROOT/'docs/evidence/docker-stage/20261030T072612-26952/summary.json').read_text(encoding='utf-8'))
        assert gate['status']=='passed' and gate['cleanup_passed']
        run('engine-preflight',docker+['version','--format','{{json .Server}}'])
        run('compose-config',prefix+['config','--quiet'])
        run('image-pull',prefix+['pull'],timeout=900)
        created=True
        run('milvus-start',prefix+['up','-d','--wait','--wait-timeout','240'],timeout=900)
        run('service-images',prefix+['images','--format','json'])
        run('container-state',prefix+['ps','--format','json'])
        report['active_stage']='local-model-and-index';save()
        encoder=load_encoder();exact=KnowledgeSearch(encoder=encoder)
        report['active_stage']='provision-milvus';save()
        backend=MilvusANN(exact.index);backend.provision()
        report['index_description']={str(t):backend.client.call('indexes/describe',{'collectionName':backend.collection(t),'indexName':'dense_hnsw'}) for t in [True,False]}
        remote=KnowledgeSearch(encoder=encoder,dense_backend=backend)
        report['active_stage']='compare-before-restart';save()
        report['before_restart']=compare(exact,remote);save();print('before-restart comparisons passed',flush=True)
        run('milvus-restart',prefix+['restart','milvus'])
        run('restart-ready',prefix+['up','-d','--wait','--wait-timeout','240'],timeout=270)
        # No re-insert here: demonstrate persisted collections survive service restart.
        for title in [True,False]:
            backend.client.call('collections/load',{'collectionName':backend.collection(title)})
        report['active_stage']='compare-after-restart';save()
        report['after_restart']=compare(exact,remote)
        report['minio_s3_tested']=not args.embedded
        report['status']='passed'
    except Exception as exc:
        report.update(status='failed' if created else 'blocked',error_type=type(exc).__name__)
    finally:
        if created:
            try:run('container-logs',prefix+['logs','--no-color'])
            except Exception:pass
            try:run('cleanup-own-project',prefix+['down']);report['cleanup_passed']=True
            except Exception:report.update(status='failed',cleanup_passed=False)
        report['finished_at_utc']=datetime.now(timezone.utc).isoformat();save()
        print('Evidence: '+str(out),flush=True)
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':sys.exit(main())

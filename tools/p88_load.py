"""P8.8a real loopback load against owned frozen API processes. No model calls."""
import argparse, asyncio, hashlib, json, math, os, platform, secrets, socket, sys, time
from collections import Counter
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

ROOT = Path(os.environ.get('LOGIMATE_PROJECT_ROOT', str(Path(__file__).resolve().parents[1])))
sys.path.insert(0, str(ROOT))
SEARCH='/v1/knowledge/resilient/search'
HEALTH='/v1/knowledge/resilient/health'
RUNNER_REVISION='p88a-r3-startup-diagnostics'


def read(path):return json.loads(path.read_text(encoding='utf-8'))
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def write(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def retrieval_cache_location(root=ROOT,environment=None,system=None):
    env=os.environ if environment is None else environment
    supplied=env.get('LOGIMATE_RETRIEVAL_CACHE')
    if supplied:return supplied  # Do not evaluate a platform-specific default first.
    system=os.name if system is None else system
    if system=='nt':return str(root.parent.parent/'work/retrieval-cache')
    if system=='posix':
        import tempfile
        return str(Path(tempfile.gettempdir())/'logimate-retrieval')
    raise ValueError('unsupported operating system')


def quantiles(values):
    values=sorted(values)
    return {'n':len(values),'method':'empirical_nearest_rank',**{f'p{p}':values[math.ceil(p/100*len(values))-1] if values else None for p in (50,95,99)}}


def freeze_check():
    release=read(ROOT/'eval/holdout_release_v2.json')
    for n,h in release['runtime_sha256'].items():
        if sha(ROOT/n)!=h:raise ValueError('frozen application drift: '+n)
    return {'release_sha256':sha(ROOT/'eval/holdout_release_v2.json'),'runtime_sha256':release['runtime_sha256']}


def validate_protocol(p):
    if p['protocol_id']!='p88a-cpu-exact-load-v1' or p['backend']!='exact' or p['endpoint']!=SEARCH or p['tenant']!='demo-a' or p['retry_count']!=0:
        raise ValueError('unsupported load protocol')
    if p['default_policy']!={'active_limit':4,'per_tenant_per_minute':30,'global_per_minute':60,'retrieval_budget_ms':800}:raise ValueError('default policy differs')
    if not p['queries'] or any(set(q)!={'query','domain','top_k'} for q in p['queries']):raise ValueError('invalid query payload')
    for profile in p['profiles']:
        if profile['acceptance'] not in ('all_hybrid','explicit_overload','thirty_then_rate_limit') or profile['concurrency']<1 or profile['requests']<1 or profile['interval_seconds']<0 or profile['repeats']<1:raise ValueError('invalid profile')


def classify(status,body,args,index):
    """Validate returned source evidence even under overload; never drop bad responses."""
    from logimate.retrieval import eligible
    if not isinstance(body,dict) or body.get('simulated') is not True or not body.get('trace_id') or body.get('retrieval_budget_ms')!=800:raise ValueError('invalid service envelope')
    elapsed=body.get('retrieval_elapsed_ms')
    if isinstance(elapsed,bool) or not isinstance(elapsed,(int,float)) or not math.isfinite(elapsed) or elapsed<0:raise ValueError('invalid server timing')
    if status==200:
        mode=body.get('served_mode')
        if mode not in ('hybrid','keyword') or body.get('mode')!=mode or body.get('degraded')!=(mode=='keyword') or body.get('backend')!='in-memory-exact':raise ValueError('unlabelled mode')
        if body.get('corpus_sha256')!=index['corpus_sha256'] or body.get('corpus_id')!=index['corpus_id'] or body.get('as_of')!=index['as_of']:raise ValueError('wrong source version')
        if body.get('deadline_overrun') or elapsed>800 or body.get('status') not in ('ok','no_match'):raise ValueError('invalid successful budget/status')
        pool={c['chunk_id']:c for c in index['chunks'] if eligible(c,'demo-a',index['as_of'],args['domain'])}
        hits=body.get('hits')
        if not isinstance(hits,list) or len(hits)>args['top_k'] or (mode=='hybrid' and len(hits)!=min(len(pool),args['top_k'])):raise ValueError('wrong evidence count')
        seen=set()
        for rank,hit in enumerate(hits,1):
            ref=pool.get(hit.get('chunk_id'))
            if ref is None or hit['chunk_id'] in seen or set(hit)!=set(ref)|{'rank','score'}:raise ValueError('foreign or duplicate source')
            if any(hit[k]!=v or type(hit[k]) is not type(v) for k,v in ref.items()) or hit['rank']!=rank:raise ValueError('altered evidence')
            if isinstance(hit['score'],bool) or not isinstance(hit['score'],(int,float)) or not math.isfinite(hit['score']):raise ValueError('invalid score')
            seen.add(hit['chunk_id'])
        return 'hybrid' if mode=='hybrid' else 'degraded'
    if status==429 and body.get('status')=='rate_limited' and body.get('degradation_reason') in ('admission_rate','admission_capacity') and not body.get('hits'):
        return body['degradation_reason']
    if status==503 and body.get('status')=='unavailable' and isinstance(body.get('degradation_reason'),str) and not body.get('hits'):
        return 'unavailable'
    raise ValueError('unexpected HTTP response')


def summarize(rows,duration):
    categories=Counter(r['category'] for r in rows)
    good=[r for r in rows if r['category'] in ('hybrid','degraded')]
    hybrid=[r for r in rows if r['category']=='hybrid']
    return {'offered':len(rows),'categories':dict(categories),'http_status_counts':dict(Counter(str(r.get('http_status')) for r in rows)),
            'duration_seconds':round(duration,6),'offered_completion_rate_per_second':len(rows)/duration if duration else None,
            'hybrid_completion_rate_per_second':len(hybrid)/duration if duration else None,
            'hybrid_fraction_of_offered':len(hybrid)/len(rows) if rows else None,
            'served_fraction_of_offered':len(good)/len(rows) if rows else None,
            'all_client_rtt_ms':quantiles([r['client_rtt_ms'] for r in rows]),
            'all_sojourn_ms':quantiles([r['sojourn_ms'] for r in rows]),
            'hybrid_client_rtt_ms':quantiles([r['client_rtt_ms'] for r in hybrid]),
            'degraded_client_rtt_ms':quantiles([r['client_rtt_ms'] for r in rows if r['category']=='degraded']),
            'all_server_retrieval_ms':quantiles([r['body']['retrieval_elapsed_ms'] for r in rows if r.get('body') and 'retrieval_elapsed_ms' in r['body']]),
            'max_launch_lag_ms':max((r['launch_lag_ms'] for r in rows),default=0)}


def acceptance(profile,rows,before,after,samples,max_lag):
    problems=[]
    counts=Counter(r['category'] for r in rows)
    if len(rows)!=profile['requests']:problems.append('missing_offered_requests')
    if counts['invalid_response'] or counts['transport_error']:problems.append('invalid_or_transport_response')
    traces=[r['body']['trace_id'] for r in rows if r.get('category') not in ('invalid_response','transport_error')]
    if len(traces)!=len(set(traces)):problems.append('duplicate_trace')
    metrics={k:after['metrics'][k]-before['metrics'][k] for k in before['metrics']}
    if metrics['requests']!=len(rows):problems.append('server_request_denominator')
    if metrics['responses_ok']!=counts['hybrid']+counts['degraded'] or metrics['responses_degraded']!=counts['degraded'] or metrics['responses_unavailable']!=counts['unavailable'] or metrics['admission_rejected']!=counts['admission_rate']+counts['admission_capacity']:problems.append('server_response_accounting')
    if after['active_requests'] or after['primary']['busy'] or after['keyword']['busy']:problems.append('work_not_drained')
    if after['integrity_failed'] or metrics['integrity_failure']:problems.append('source_integrity_failed')
    if any(s['active_requests']>4 for s in samples):problems.append('sampled_active_above_limit')
    if profile['interval_seconds'] and any(r['launch_lag_ms']>max_lag for r in rows):problems.append('paced_generator_late')
    if profile['acceptance']=='all_hybrid' and counts['hybrid']!=len(rows):problems.append('expected_full_hybrid')
    if profile['acceptance']=='thirty_then_rate_limit':
        if [r['category'] for r in rows]!=['hybrid']*30+['admission_rate']*2:problems.append('rate_boundary_differs')
    return {'passed':not problems,'problems':problems,'server_metric_delta':metrics,'sampled_active_max':max((s['active_requests'] for s in samples),default=0),'health_samples':len(samples)}


async def serve(port):
    import uvicorn
    from logimate.api import create_app
    app=create_app(token=os.environ['LOGIMATE_LOAD_TOKEN'])
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning',access_log=False))
    async def stop():
        await asyncio.to_thread(sys.stdin.buffer.readline)
        server.should_exit=True
    control=asyncio.create_task(stop())
    try:await server.serve()
    finally:
        control.cancel()
        await asyncio.gather(control,return_exceptions=True)


def readiness_observation(status,body):
    keys=('enabled','ready','initialization_error','primary','keyword','integrity_failed','metrics','active_requests')
    return {'http_status':status,'health':{k:body[k] for k in keys if k in body}}


async def run_profile(protocol,profile,repeat,out,*,startup_only=False):
    import httpx
    index=read(ROOT/'data/knowledge/index-v1.json')
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    token=secrets.token_urlsafe(32)
    allowed={'SYSTEMROOT','WINDIR','PATH','PATHEXT','TEMP','TMP','COMSPEC','HOME','USERPROFILE'}
    env={k:v for k,v in os.environ.items() if k.upper() in allowed}
    env.update(PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1',LOGIMATE_PROJECT_ROOT=str(ROOT),LOGIMATE_LOAD_TOKEN=token,
               LOGIMATE_RETRIEVAL_CACHE=retrieval_cache_location(),
               LOGIMATE_LEXICAL_CACHE=str(out/'lexical-cache'),LOGIMATE_DENSE_BACKEND='exact',LOGIMATE_RESILIENT_RETRIEVAL='1',
               LOGIMATE_CASE_DB='',LOGIMATE_NATIVE_REVIEW='0',LOGIMATE_ANALYSIS_REVIEW='0')
    logfile=(out/'server.log').open('wb')
    process=await asyncio.create_subprocess_exec(sys.executable,str(Path(__file__).resolve()),'--serve','--port',str(port),
        cwd=ROOT,env=env,stdin=asyncio.subprocess.PIPE,stdout=logfile,stderr=asyncio.subprocess.STDOUT,
        **({'creationflags':__import__('subprocess').CREATE_NO_WINDOW} if os.name=='nt' else {}))
    started=time.perf_counter();result={'status':'failed','profile':profile,'repeat':repeat,'api_pid':process.pid}
    transport=httpx.AsyncHTTPTransport(retries=0)
    try:
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',headers={'Authorization':'Bearer '+token},
            timeout=protocol['http_timeout_seconds'],transport=transport,trust_env=False) as client:
            while True:
                if process.returncode is not None:raise RuntimeError('owned API exited')
                try:
                    response=await client.get(HEALTH);h=response.json()
                    result['readiness_last']=readiness_observation(response.status_code,h)
                    if response.status_code==200 and h.get('primary',{}).get('ready') and h.get('keyword',{}).get('ready'):break
                except (httpx.HTTPError,ValueError):pass
                if time.perf_counter()-started>protocol['readiness_timeout_seconds']:raise TimeoutError('worker readiness')
                await asyncio.sleep(.05)
            result['startup_ready_ms']=(time.perf_counter()-started)*1000
            if startup_only:
                result.update(status='passed',phase='P8.8a-startup-probe',startup_probe_only=True,measured_requests=0)
                return result
            warm=[]
            for i in range(profile['warmup_searches']):
                args=protocol['queries'][i%len(protocol['queries'])]
                resp=await client.post(SEARCH,json=args);body=resp.json()
                if classify(resp.status_code,body,args,index)!='hybrid':raise ValueError('warmup not hybrid')
                warm.append(body)
            result['warmup']=warm
            before=(await client.get(HEALTH)).json();rows=[];samples=[];sem=asyncio.Semaphore(profile['concurrency'])
            origin=time.perf_counter();running=True
            async def sample():
                while running:
                    response=await client.get(HEALTH)
                    if response.status_code!=200:raise RuntimeError('health sample failed')
                    samples.append(response.json())
                    await asyncio.sleep(.05)
            sampler=asyncio.create_task(sample())
            async def send(i):
                scheduled=origin+i*profile['interval_seconds']
                await asyncio.sleep(max(0,scheduled-time.perf_counter()))
                launched=time.perf_counter()
                async with sem:
                    began=time.perf_counter();args=protocol['queries'][i%len(protocol['queries'])]
                    row={'request_index':i,'query_index':i%len(protocol['queries']),'launch_lag_ms':(launched-scheduled)*1000,
                         'client_queue_ms':(began-launched)*1000,'category':'transport_error'}
                    try:
                        response=await client.post(SEARCH,json=args);row['http_status']=response.status_code
                        try:
                            row['body']=response.json();row['category']=classify(response.status_code,row['body'],args,index)
                        except (ValueError,KeyError,TypeError):row['category']='invalid_response'
                    except httpx.HTTPError as exc:row['error_type']=type(exc).__name__
                    finally:
                        ended=time.perf_counter();row.update(client_rtt_ms=(ended-began)*1000,sojourn_ms=(ended-scheduled)*1000)
                        rows.append(row)
            try:await asyncio.gather(*(send(i) for i in range(profile['requests'])))
            finally:
                duration=time.perf_counter()-origin;running=False
                await sampler
            rows.sort(key=lambda r:r['request_index'])
            after=(await client.get(HEALTH)).json()
            checks=acceptance(profile,rows,before,after,samples,protocol['max_paced_launch_lag_ms'])
            result.update(status='passed' if checks['passed'] else 'failed',rows=rows,health_before=before,health_after=after,
                          health_samples=samples,checks=checks,summary=summarize(rows,duration))
    except Exception as exc:result.update(error_type=type(exc).__name__)
    finally:
        if process.returncode is None:
            try:
                process.stdin.write(b'stop\n');await process.stdin.drain();process.stdin.close()
                await asyncio.wait_for(process.wait(),timeout=15)
            except (BrokenPipeError,ConnectionResetError,TimeoutError):
                if process.returncode is None:process.kill();await process.wait()
                result['forced_cleanup']=True
        logfile.close();result['api_exit_code']=process.returncode
        result['graceful_cleanup_passed']=process.returncode==0 and not result.get('forced_cleanup')
        if not result['graceful_cleanup_passed']:result['status']='failed'
        write(out/'result.json',result)
    return result


async def collect(protocol,out):
    report={'phase':'P8.8a-CPU-exact-load','status':'running','runner_revision':RUNNER_REVISION,'protocol_sha256':sha(Path(__file__).with_name('p88-load-protocol.json')),
            'runner_sha256':sha(Path(__file__)),'application_freeze':freeze_check(),'real_tcp_http':False,'real_cpu_embeddings':False,
            'real_milvus':False,'real_model_calls':0,'new_model_tokens':0,'profiles':[],
            'environment':{'os':platform.platform(),'python':platform.python_version(),'cpu_count':os.cpu_count(),
                           'cpu_description':os.environ.get('PROCESSOR_IDENTIFIER',platform.processor()),
                           'versions':{n:version(n) for n in ('fastapi','uvicorn','httpx','fastembed','onnxruntime','jieba')},
                           'generator_and_api_same_host':True,'api_processes':1,'primary_slots':1,'keyword_slots':1}}
    write(out/'protocol.json',protocol);write(out/'summary.json',report)
    try:
        for profile in protocol['profiles']:
            for repeat in range(1,profile['repeats']+1):
                report['active_profile']={'name':profile['name'],'repeat':repeat}
                target=out/f'{profile["name"]}-r{repeat}';target.mkdir()
                r=await run_profile(protocol,profile,repeat,target)
                if r.get('rows') or r.get('warmup'):report['real_tcp_http']=True
                if r.get('warmup') or any(row['category']=='hybrid' for row in r.get('rows',[])):report['real_cpu_embeddings']=True
                report['profiles'].append({'name':profile['name'],'repeat':repeat,'status':r['status'],
                    'result_sha256':sha(target/'result.json'),'result':str(target.relative_to(out)/'result.json'),
                    'summary':r.get('summary'),'checks':r.get('checks'),'graceful_cleanup_passed':r['graceful_cleanup_passed']})
                write(out/'summary.json',report)
                print(profile['name']+f' repeat {repeat}: '+r['status'],flush=True)
                if r['status']!='passed' and not r.get('rows') and not r.get('warmup'):
                    report['stop_reason']='pre_measurement_failure; remaining repeats not executed'
                    raise RuntimeError('startup failed before measurements; inspect readiness_last')
        report['status']='passed' if all(r['status']=='passed' for r in report['profiles']) else 'failed'
    except Exception as exc:
        report.update(status='failed',error_type=type(exc).__name__,failed_profile=report.get('active_profile'))
        raise
    finally:
        report['application_unchanged']=report['application_freeze']==freeze_check()
        report['runner_unchanged']=report['runner_sha256']==sha(Path(__file__))
        report['protocol_unchanged']=report['protocol_sha256']==sha(Path(__file__).with_name('p88-load-protocol.json'))
        if not all(report[k] for k in ('application_unchanged','runner_unchanged','protocol_unchanged')):report['status']='failed'
        report['measured_requests_recorded']=sum((r.get('summary') or {}).get('offered',0) for r in report['profiles'])
        report['finished_at_utc']=datetime.now(timezone.utc).isoformat();write(out/'summary.json',report)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--serve',action='store_true',help=argparse.SUPPRESS)
    p.add_argument('--startup-probe',action='store_true',help='ready both real workers, save health, stop without search requests')
    p.add_argument('--port',type=int,help=argparse.SUPPRESS);p.add_argument('--output',type=Path)
    a=p.parse_args()
    if a.serve:
        asyncio.run(serve(a.port));return 0
    protocol=read(Path(__file__).with_name('p88-load-protocol.json'));validate_protocol(protocol)
    out=a.output or ROOT/'docs/evidence/p8'/datetime.now(timezone.utc).strftime('load-local-%Y%m%dT%H%M%S%f')
    out.mkdir(parents=True,exist_ok=False)
    if a.startup_probe:
        result=asyncio.run(run_profile(protocol,protocol['profiles'][0],1,out,startup_only=True))
        write(out/'summary.json',result)
        print('Startup probe: '+result['status']);print('Evidence: '+str(out),flush=True)
        return 0 if result['status']=='passed' else 1
    try:report=asyncio.run(collect(protocol,out))
    except Exception as exc:
        print('Failure: '+type(exc).__name__);raise
    finally:print('Evidence: '+str(out),flush=True)
    print('Status: '+report['status'],flush=True)
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())

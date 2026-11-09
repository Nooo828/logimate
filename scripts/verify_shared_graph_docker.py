"""P6.2 shared graph/MCP containers. --live adds hidden-key paired model checks."""
import argparse
import hashlib
import json
import os
import getpass
import copy
import subprocess
import sys
from datetime import datetime, timezone

from logimate.milvus_backend import MilvusANN
from logimate.retrieval import ROOT, cache_dir
from scripts.smoke_milvus_api import exact_service
from scripts.verify_milvus import docker_commands
from scripts.verify_milvus_api import joint_runtime


def write_runtime(path, config, *, live=False):
    """Build config has no model references; only post-build runtime needs a secret."""
    rendered = copy.deepcopy(config)
    environment = rendered['services']['api'].setdefault('environment', {})
    environment.pop('DEEPSEEK_API_KEY', None)
    environment.pop('LOGIMATE_MODEL', None)
    if live:
        environment['DEEPSEEK_API_KEY'] = '${DEEPSEEK_API_KEY:?Hidden runtime key required}'
        environment['LOGIMATE_MODEL'] = '${LOGIMATE_MODEL:-deepseek-flash}'
    path.write_text(json.dumps(rendered, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desktop-linux', action='store_true')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--local-parent', action='store_true', help='Explicitly rebuild from recorded, verified local P6.2 image; no remote dependency downloads')
    parser.add_argument('--port', type=int, default=18084, choices=range(1, 65536), metavar='PORT')
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    out = ROOT / 'docs/evidence/p6' / ('shared-docker-' + stamp); out.mkdir(parents=True, exist_ok=False)
    project = 'logimate-p62-' + stamp.lower()
    docker, prefix, images = docker_commands(project, out, True, args.desktop_linux, True)
    runtime = joint_runtime(out, project, cache_dir(), args.port)
    config = json.loads(runtime.read_text(encoding='utf-8'))
    config['services']['api']['environment']['LOGIMATE_RESILIENT_RETRIEVAL'] = '1'
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    write_runtime(runtime, config)
    prefix += ['-f', str(runtime)]
    report = {'status': 'running', 'phase': 'P6.2-shared-policy-graph', 'live_requested': args.live, 'project': project,
        'real_llm_calls': False, 'graph_connection_implemented': True, 'graph_connected_to_p6': False, 'simulated_business_data': True,
        'minio_s3_tested': False, 'build_mode': 'verified-local-parent' if args.local_parent else 'official-registry',
        'steps': [], 'results': {}, 'effective_images': images,
        'input_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for pattern in ['logimate/*.py', 'scripts/*resilient*.py', 'scripts/*shared*.py', 'scripts/retrieval_fault_worker.py',
                            'scripts/verify_milvus.py', 'scripts/verify_milvus_api.py', 'scripts/local_image_parent.py', 'scripts/smoke_policy_graph.py',
                            'scripts/verify_mcp_graph.py', 'scripts/smoke_milvus_api.py', 'data/synthetic_v1.json', 'prompts/*.txt', 'Dockerfile.retrieval',
                            'requirements-retrieval.lock', 'data/knowledge/*.json', 'compose.milvus*.json',
                            'deploy/milvus-embed-*.yaml'] for p in ROOT.glob(pattern)}}
    def save(): (out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    def command(name, argv, timeout=30):
        report['active_stage'] = name; save()
        try:
            p = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
            code, stdout, stderr = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as exc:
            def dec(v): return v.decode('utf-8', errors='replace') if isinstance(v, bytes) else v or ''
            code, stdout, stderr = 124, dec(exc.stdout), dec(exc.stderr) + '\nCommand timed out'
        except OSError as exc: code, stdout, stderr = 127, '', str(exc)
        safe = stdout + stderr
        if env.get('DEEPSEEK_API_KEY'): safe = safe.replace(env['DEEPSEEK_API_KEY'], '[REDACTED]')
        (out / (name + '.txt')).write_text(safe, encoding='utf-8')
        report['steps'].append({'name': name, 'exit_code': code}); save()
        print(name + ': exit ' + str(code), flush=True)
        if code: raise RuntimeError(name + ' failed')
        return stdout
    def smoke(name, mode):
        raw = command(name, [sys.executable, '-m', 'scripts.smoke_resilient', '--base',
                            f'http://127.0.0.1:{args.port}', '--mode', mode], timeout=120)
        report['results'][name] = json.loads(raw); save()
    def graph(name, mode):
        raw = command(name, [sys.executable, '-m', 'scripts.smoke_shared_graph', '--base',
            f'http://127.0.0.1:{args.port}', '--mode', mode], timeout=300)
        report['results'][name] = json.loads(raw)
        report['completed_live_step_usage'] = {
            'provider_responses': sum(v.get('provider_responses', 0) for v in report['results'].values()),
            'total_tokens': sum(v.get('total_tokens', 0) for v in report['results'].values())}
        report['real_llm_calls'] = report['completed_live_step_usage']['provider_responses'] > 0
        save()
    def fixture(name, scenario):
        raw = command(name, prefix + ['exec', '-T', 'api', 'python', '-m',
            'scripts.probe_shared_graph', '--stdout', '--scenario', scenario], timeout=240)
        report['results'][name] = json.loads(raw); save()
        if report['results'][name].get('status') != 'passed': raise ValueError('shared graph fixture failed')
        report['graph_connected_to_p6'] = True; save()
    created = ready = False
    save()
    try:
        gatepath = ROOT / 'docs/evidence/p6/docker-20261104T012331932813/summary.json'
        gate = json.loads(gatepath.read_text(encoding='utf-8'))
        if gate['status'] != 'passed' or not gate['cleanup_passed'] or len(gate['steps']) != 22 or any(s['exit_code'] != 0 for s in gate['steps']):
            raise ValueError('P6.1 actual container gate not passed')
        for name in ('data/knowledge/corpus-v1.json', 'data/knowledge/index-v1.json', 'data/knowledge/qa-v1.json'):
            if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != gate['input_sha256'][name]:
                raise ValueError('frozen v1 dataset changed')
        report['p61_gate_sha256'] = hashlib.sha256(gatepath.read_bytes()).hexdigest()
        command('engine-preflight', docker + ['version', '--format', '{{json .Server}}']); ready = True
        local_layers = None
        if args.local_parent:
            from scripts.local_image_parent import plan, validate_image, validate_child, dockerfile, file_probe
            local = plan(ROOT)
            report['local_parent'] = local; save()
            for role, expected in local['images'].items():
                actual = json.loads(command(role + '-local-image', docker + ['image', 'inspect', expected['tag']]))[0]
                layers = validate_image(actual, expected)
                if role == 'api': local_layers = layers
            builder = command('local-builder-inspect', docker + ['buildx', 'inspect', 'default'])
            if not any(line.startswith('Driver:') and line.split(':', 1)[1].strip() == 'docker' for line in builder.splitlines()):
                raise ValueError('local parent requires default Docker driver; builder configuration not changed')
            env['BUILDX_BUILDER'] = 'default'
            command('parent-file-provenance', docker + ['run', '--rm', '--pull=never', '--network=none', '--read-only',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=64', '--entrypoint', 'python',
                local['images']['api']['tag'], '-c', file_probe(local['file_sha256'])])
            recipe = out / 'Dockerfile.local-parent'
            recipe.write_text(dockerfile((ROOT / 'Dockerfile.retrieval').read_text(encoding='utf-8'), local['images']['api']['tag']), encoding='utf-8')
            child_hashes = dict(local['file_sha256'])
            for pattern in ('data/**/*.json', 'prompts/*.txt'):
                child_hashes.update({p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob(pattern)})
            for line in recipe.read_text(encoding='utf-8').splitlines():
                if line.startswith('COPY scripts/'):
                    name = line.split()[1]
                    child_hashes[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            report['child_file_sha256'] = child_hashes
            config['services']['api']['build']['dockerfile'] = str(recipe)
            config['services']['api']['build']['pull'] = False
            config['services']['api']['build']['network'] = 'none'
            write_runtime(runtime, config)
            report['generated_dockerfile_sha256'] = hashlib.sha256(recipe.read_bytes()).hexdigest(); save()
        command('compose-config', prefix + ['config', '--quiet'])
        if not args.local_parent: command('milvus-image-pull', prefix + ['pull', 'milvus'], timeout=900)
        command('api-image-build', prefix + ['--progress', 'plain', 'build', 'api'], timeout=900)
        if args.local_parent:
            child = json.loads(command('local-child-image', docker + ['image', 'inspect', project + '-api:verification']))[0]
            validate_child(child, local_layers)
            command('child-file-provenance', docker + ['run', '--rm', '--pull=never', '--network=none', '--read-only',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=64', '--entrypoint', 'python',
                project + '-api:verification', '-c', file_probe(child_hashes)])
            report['local_parent_build_verified'] = True; save()
        if args.live and not env.get('DEEPSEEK_API_KEY'):
            if not sys.stdin.isatty(): raise RuntimeError('Hidden key requires own interactive terminal')
            env['DEEPSEEK_API_KEY'] = getpass.getpass('DeepSeek API key (hidden, this run only): ')
        if args.live and not env.get('DEEPSEEK_API_KEY'): raise ValueError('empty model key')
        write_runtime(runtime, config, live=args.live)
        command('runtime-compose-config', prefix + ['config', '--quiet'])
        created = True
        command('milvus-start', prefix + ['up', '-d', '--wait', '--wait-timeout', '240', 'milvus'], timeout=270)
        report['active_stage'] = 'provision-v1'; save()
        backend = MilvusANN(exact_service().index); backend.provision()
        command('api-start', prefix + ['up', '-d', '--wait', '--wait-timeout', '90', 'api'], timeout=120)
        command('service-images', prefix + ['images', '--format', 'json'])
        command('api-nonroot', prefix + ['exec', '-T', 'api', 'python', '-c',
            'import os; assert os.getuid()!=0; print("non-root uid="+str(os.getuid()))'])
        # Real CPU roles and real OS cancellation; stall/closed-port sources explicitly labelled test doubles.
        report['results']['owned-worker-faults'] = json.loads(command('owned-worker-faults', prefix + [
            'exec', '-T', 'api', 'python', '-m', 'scripts.verify_resilient_local', '--stdout'], timeout=180)); save()
        smoke('http-input-guards', 'guards')
        smoke('http-real-hybrid', 'normal')
        graph('shared-graph-guards', 'guards')
        no_key = "import os; os.environ.pop('DEEPSEEK_API_KEY',None); from fastapi.testclient import TestClient; from logimate.api import create_app; c=TestClient(create_app(token='no-key-shared')); c.__enter__(); r=c.post('/v1/graph/mcp/resilient/queries',json={'query':'SIM-008'},headers={'Authorization':'Bearer no-key-shared'}); assert r.status_code==503; h=c.get('/v1/knowledge/resilient/health',headers={'Authorization':'Bearer no-key-shared'}); assert h.json()['metrics']['requests']==0; c.__exit__(None,None,None); print('503 with zero searches, no configured model or fixture fallback')"
        command('shared-missing-key-fails-closed', prefix + ['exec', '-T', 'api', 'python', '-c', no_key])
        fixture('fixture-shared-normal', 'normal')
        if args.live: graph('live-shared-normal', 'live')
        command('stop-own-milvus', prefix + ['stop', 'milvus'], timeout=90)
        smoke('http-keyword-and-circuit', 'outage')
        fixture('fixture-shared-outage', 'outage')
        if args.live: graph('live-shared-outage', 'live-outage')
        command('restart-own-milvus', prefix + ['up', '-d', '--wait', '--wait-timeout', '240', 'milvus'], timeout=270)
        for title in (True, False): backend.client.call('collections/load', {'collectionName': backend.collection(title)})
        smoke('http-half-open-recovery', 'recover')
        fixture('fixture-shared-recovered', 'normal')
        smoke('http-burst-admission', 'burst')
        command('api-restart', prefix + ['restart', 'api'], timeout=90)
        command('api-restart-ready', prefix + ['up', '-d', '--wait', '--wait-timeout', '90', 'api'], timeout=120)
        smoke('http-restart-hybrid', 'normal')
        fixture('fixture-shared-api-restart', 'normal')
        graph('shared-restart-guards', 'guards')
        if args.live: graph('live-shared-restart', 'live-restart')
        report['real_llm_calls'] = bool(args.live and report.get('completed_live_step_usage', {}).get('provider_responses', 0) > 0)
        image_env = json.loads(command('image-credential-check', docker + ['image', 'inspect', project + '-api:verification', '--format', '{{json .Config.Env}}']))
        assert not any(x.startswith('DEEPSEEK_API_KEY=') or x.startswith('LOGIMATE_MCP_SHARED_TOKEN=') for x in image_env)
        report['image_credentials_absent'] = True
        command('container-state', prefix + ['ps', '--format', 'json'])
        report['status'] = 'passed'
    except Exception as exc:
        report.update(status='failed' if ready else 'blocked', error_type=type(exc).__name__,
                      failed_stage=report.get('active_stage', 'P6.1-gate'))
    finally:
        if created:
            try: command('container-logs', prefix + ['logs', '--no-color'], timeout=60)
            except Exception: pass
            try:
                command('cleanup-own-project', prefix + ['down'], timeout=120); report['cleanup_passed'] = True
            except Exception: report.update(status='failed', cleanup_passed=False)
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat(); save()
        print('Status: ' + report['status'], flush=True)
        print('Evidence: ' + str(out), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())

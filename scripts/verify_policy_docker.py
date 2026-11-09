"""P5.3 actual policy-aware MCP graph containers. --live adds hidden-key model checks."""
import argparse
import getpass
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from logimate.milvus_backend import MilvusANN
from logimate.retrieval import ROOT, cache_dir
from scripts.smoke_milvus_api import exact_service
from scripts.verify_milvus import docker_commands
from scripts.verify_milvus_api import CRITICAL, GATE, joint_runtime, validate_gate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desktop-linux', action='store_true')
    parser.add_argument('--live', action='store_true', help='Real model paired HTTP, outage and restart checks; hidden key')
    parser.add_argument('--port', type=int, default=18082, choices=range(1, 65536), metavar='PORT')
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    out = ROOT / 'docs/evidence/p5' / ('policy-docker-' + stamp)
    out.mkdir(parents=True, exist_ok=False)
    project = 'logimate-policy-' + stamp.lower()
    docker, prefix, images = docker_commands(project, out, True, args.desktop_linux, True)
    runtime = joint_runtime(out, project, cache_dir(), args.port)
    if args.live:
        config = json.loads(runtime.read_text(encoding='utf-8'))
        config['services']['api']['environment'].update(
            DEEPSEEK_API_KEY='${DEEPSEEK_API_KEY:?Hidden runtime key required}',
            LOGIMATE_MODEL='${LOGIMATE_MODEL:-deepseek-flash}')
        runtime.write_text(json.dumps(config, indent=2), encoding='utf-8')
    prefix += ['-f', str(runtime)]
    env = dict(os.environ)
    report = {'status': 'running', 'phase': 'P5.3-policy-context-graph', 'project': project,
        'live_requested': args.live, 'real_llm_calls_verified': False, 'simulated_business_data': True,
        'deployment_mode': 'embedded-etcd-local-storage', 'minio_s3_tested': False,
        'effective_images': images, 'steps': [], 'results': {},
        'input_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for pattern in ['logimate/*.py', 'scripts/*policy*.py', 'scripts/verify_milvus*.py',
                            'scripts/verify_mcp_graph.py', 'scripts/smoke_milvus_api.py', 'prompts/*.txt',
                            'data/synthetic_v1.json',
                            'Dockerfile.retrieval', 'requirements-retrieval.lock', 'data/knowledge/*.json',
                            'compose.milvus*.json', 'deploy/milvus-embed-*.yaml'] for p in ROOT.glob(pattern)}}

    def save():
        (out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def command(name, argv, timeout=30):
        report['active_stage'] = name; save()
        try:
            p = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True,
                text=True, encoding='utf-8', errors='replace', timeout=timeout)
            code, stdout, stderr = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as exc:
            def decode(value): return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''
            code, stdout, stderr = 124, decode(exc.stdout), decode(exc.stderr) + '\nCommand timed out'
        except OSError as exc: code, stdout, stderr = 127, '', str(exc)
        safe = stdout + stderr
        if env.get('DEEPSEEK_API_KEY'): safe = safe.replace(env['DEEPSEEK_API_KEY'], '[REDACTED]')
        (out / (name + '.txt')).write_text(safe, encoding='utf-8')
        report['steps'].append({'name': name, 'exit_code': code}); save()
        print(name + ': exit ' + str(code), flush=True)
        if code: raise RuntimeError(name + ' failed')
        return stdout

    def smoke(name, mode, inside=False):
        argv = prefix + ['exec', '-T', 'api', 'python'] if inside else [sys.executable]
        argv += ['-m', 'scripts.smoke_policy_graph', '--mode', mode,
                 '--base', 'http://127.0.0.1:' + ('8000' if inside else str(args.port))]
        report['results'][name] = json.loads(command(name, argv, timeout=300))
        report['completed_live_step_usage'] = {
            'provider_responses': sum(v.get('provider_responses', 0) for v in report['results'].values()),
            'total_tokens': sum(v.get('total_tokens', 0) for v in report['results'].values())}
        save()

    created = ready = False
    save()
    try:
        gate = json.loads((ROOT / GATE).read_text(encoding='utf-8'))
        validate_gate(gate, {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in CRITICAL})
        joint = json.loads((ROOT / 'docs/evidence/p5/milvus-api-20261103T111704748814/summary.json').read_text(encoding='utf-8'))
        if joint['status'] != 'passed' or not joint['cleanup_passed']: raise ValueError('joint gate failed')
        manifest = json.loads((ROOT / 'docs/evidence/p5/chunking-stage-manifest-20261103.json').read_text(encoding='utf-8'))
        if hashlib.sha256((ROOT.parent / manifest['archive']).read_bytes()).hexdigest() != manifest['archive_sha256']:
            raise ValueError('frozen ablation archive mismatch')
        command('engine-preflight', docker + ['version', '--format', '{{json .Server}}'])
        ready = True
        if args.live and not env.get('DEEPSEEK_API_KEY'):
            if not sys.stdin.isatty(): raise RuntimeError('Hidden runtime key requires own interactive terminal')
            env['DEEPSEEK_API_KEY'] = getpass.getpass('DeepSeek API key (hidden, this run only): ')
        if args.live and not env.get('DEEPSEEK_API_KEY'): raise ValueError('empty model key')
        command('compose-config', prefix + ['config', '--quiet'])
        command('milvus-image-pull', prefix + ['pull', 'milvus'], timeout=900)
        command('api-image-build', prefix + ['--progress', 'plain', 'build', 'api'], timeout=900)
        created = True
        command('milvus-start', prefix + ['up', '-d', '--wait', '--wait-timeout', '240', 'milvus'], timeout=270)
        report['active_stage'] = 'provision-policy-index'; save()
        index = exact_service().index
        backend = MilvusANN(index); backend.provision()
        command('api-start', prefix + ['up', '-d', '--wait', '--wait-timeout', '90', 'api'], timeout=120)
        command('service-images', prefix + ['images', '--format', 'json'])
        command('container-state', prefix + ['ps', '--format', 'json'])
        command('api-nonroot', prefix + ['exec', '-T', 'api', 'python', '-c',
            'import os; assert os.getuid()!=0; print("non-root uid="+str(os.getuid()))'])
        report['results']['policy-protocol'] = json.loads(command('policy-protocol', prefix + [
            'exec', '-T', 'api', 'python', '-m', 'scripts.verify_policy_protocol', '--stdout'], timeout=60))
        smoke('fixture-old-new-graphs', 'fixture', inside=True)
        smoke('graph-guards-host', 'guards')
        smoke('graph-guards-container', 'guards', inside=True)
        no_key = "import os; os.environ.pop('DEEPSEEK_API_KEY',None); from fastapi.testclient import TestClient; from logimate.api import create_app; c=TestClient(create_app(token='no-key-test')); r=c.post('/v1/graph/mcp/knowledge/queries',json={'query':'SIM-002'},headers={'Authorization':'Bearer no-key-test'}); assert r.status_code==503; print('503, no configured model, no fixture fallback')"
        command('missing-key-fails-closed', prefix + ['exec', '-T', 'api', 'python', '-c', no_key])
        if args.live: smoke('live-paired-http', 'live')
        command('stop-own-milvus', prefix + ['stop', 'milvus'], timeout=90)
        smoke('fixture-database-outage', 'outage-fixture', inside=True)
        if args.live: smoke('live-database-outage', 'live-outage')
        command('restart-own-milvus', prefix + ['up', '-d', '--wait', '--wait-timeout', '240', 'milvus'], timeout=270)
        for title in (True, False): backend.client.call('collections/load', {'collectionName': backend.collection(title)})
        command('api-restart', prefix + ['restart', 'api'], timeout=90)
        command('api-restart-ready', prefix + ['up', '-d', '--wait', '--wait-timeout', '90', 'api'], timeout=120)
        smoke('fixture-restart-graphs', 'fixture', inside=True)
        smoke('restart-guards', 'guards')
        if args.live:
            smoke('live-restart-http', 'live-restart')
            report['real_llm_calls_verified'] = True
            report['live_usage'] = {'provider_responses': sum(v.get('provider_responses', 0) for v in report['results'].values()),
                                    'total_tokens': sum(v.get('total_tokens', 0) for v in report['results'].values())}
        # Inspect only image environment, in memory. Never persist its raw value.
        cid = command('api-container-id', prefix + ['ps', '-q', 'api']).strip()
        image = command('api-image-id', docker + ['inspect', '--format', '{{.Image}}', cid]).strip()
        inspected = subprocess.run(docker + ['image', 'inspect', '--format', '{{json .Config.Env}}', image],
            cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=20)
        if inspected.returncode or any(s.startswith('DEEPSEEK_API_KEY=') for s in json.loads(inspected.stdout)) or (
                env.get('DEEPSEEK_API_KEY') and env['DEEPSEEK_API_KEY'] in inspected.stdout):
            raise RuntimeError('image credential isolation failed; raw environment withheld')
        report['image_credential_check'] = 'passed'
        report['status'] = 'passed'
    except Exception as exc:
        report.update(status='failed' if ready else 'blocked', error_type=type(exc).__name__,
                      failed_stage=report.get('active_stage', 'baseline-gate'))
    finally:
        if created:
            try: command('container-logs', prefix + ['logs', '--no-color'], timeout=60)
            except Exception: pass
            try:
                command('cleanup-own-project', prefix + ['down'], timeout=120)
                report['cleanup_passed'] = True
            except Exception: report.update(status='failed', cleanup_passed=False)
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat(); save()
        print('Status: ' + report['status'], flush=True)
        print('Evidence: ' + str(out), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())

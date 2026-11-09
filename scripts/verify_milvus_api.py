"""P5.2b real FastAPI + embedded Milvus containers, outage and restart acceptance."""
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone

from logimate.milvus_backend import MilvusANN
from logimate.retrieval import ROOT, KnowledgeSearch, cache_dir
from scripts.smoke_milvus_api import exact_service
from scripts.verify_milvus import compare, docker_commands

GATE = 'docs/evidence/p5/milvus-20261103T091606709573/summary.json'
CRITICAL = ['logimate/milvus_backend.py', 'logimate/retrieval.py',
            'data/knowledge/corpus-v1.json', 'data/knowledge/index-v1.json', 'data/knowledge/qa-v1.json',
            'compose.milvus.embedded.json', 'deploy/milvus-embed-etcd.yaml', 'deploy/milvus-embed-user.yaml']
TOKEN = 'milvus-joint-verification-only'


def validate_gate(report, hashes):
    if report.get('status') != 'passed' or report.get('cleanup_passed') is not True:
        raise ValueError('Standalone Milvus acceptance gate not passed')
    if report.get('deployment_mode') != 'embedded-etcd-local-storage':
        raise ValueError('Unexpected standalone baseline deployment')
    if len(report.get('steps', [])) != 10 or any(s.get('exit_code') != 0 for s in report['steps']):
        raise ValueError('Incomplete standalone command checks')
    for phase in ('before_restart', 'after_restart'):
        rows = report.get(phase, [])
        if len(rows) != 26 or any(row.get('same_set') is not True for row in rows):
            raise ValueError('Incomplete standalone comparisons')
    for name in CRITICAL:
        if hashes.get(name) != report.get('input_sha256', {}).get(name) or not hashes.get(name):
            raise ValueError('Standalone baseline inputs changed: ' + name)


def joint_runtime(out, project, model_cache, port):
    """Generate only this project's API service. Cache is public, read-only model data."""
    if not 1 <= port <= 65535: raise ValueError('invalid local port')
    api = {'build': {'context': str(ROOT), 'dockerfile': 'Dockerfile.retrieval'},
           'image': project + '-api:verification',
           'ports': [f'127.0.0.1:{port}:8000'],
           'environment': {'LOGIMATE_DEMO_TOKEN': TOKEN,
                           'LOGIMATE_DENSE_BACKEND': 'milvus',
                           'LOGIMATE_MILVUS_URI': 'http://milvus:19530',
                           'LOGIMATE_RETRIEVAL_CACHE': '/models',
                           'LOGIMATE_LEXICAL_CACHE': '/tmp/logimate-lexical'},
           'volumes': [{'type': 'bind', 'source': str(model_cache.resolve()),
                        'target': '/models', 'read_only': True}],
           'depends_on': {'milvus': {'condition': 'service_healthy'}}}
    path = out / 'joint-api.json'
    path.write_text(json.dumps({'services': {'api': api}}, indent=2), encoding='utf-8')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desktop-linux', action='store_true')
    parser.add_argument('--port', type=int, default=18081, choices=range(1, 65536), metavar='PORT')
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    out = ROOT / 'docs/evidence/p5' / ('milvus-api-' + stamp)
    out.mkdir(parents=True, exist_ok=False)
    project = 'logimate-milvus-api-' + stamp.lower()
    docker, prefix, images = docker_commands(project, out, True, args.desktop_linux, True)
    runtime = joint_runtime(out, project, cache_dir(), args.port)
    prefix += ['-f', str(runtime)]
    report = {'status': 'running', 'acceptance_scope': 'P5.2b-FastAPI-Milvus-joint',
              'started_at_utc': datetime.now(timezone.utc).isoformat(), 'project': project,
              'simulated_business_data': True, 'real_llm_calls': False,
              'deployment_mode': 'embedded-etcd-local-storage', 'minio_s3_tested': False,
              'effective_images': images, 'steps': [], 'http_results': {},
              'input_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for pattern in ['logimate/*.py', 'scripts/verify_milvus*.py', 'scripts/smoke_milvus_api.py',
                                  'Dockerfile.retrieval', 'requirements-retrieval.lock', 'compose.milvus*.json',
                                  'deploy/milvus-embed-*.yaml', 'data/knowledge/*.json'] for p in ROOT.glob(pattern)}}

    def save():
        (out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def command(label, argv, timeout=30):
        report['active_stage'] = label
        save()
        try:
            p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=timeout)
            code, stdout, stderr = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as exc:
            def decode(value): return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''
            code, stdout, stderr = 124, decode(exc.stdout), decode(exc.stderr) + '\nCommand timed out'
        except OSError as exc:
            code, stdout, stderr = 127, '', str(exc)
        (out / (label + '.txt')).write_text(stdout + stderr, encoding='utf-8')
        report['steps'].append({'name': label, 'exit_code': code})
        save()
        print(label + ': exit ' + str(code), flush=True)
        if code: raise RuntimeError(label + ' failed')
        return stdout

    def http(label, inside=False, outage=False):
        base = 'http://127.0.0.1:' + ('8000' if inside else str(args.port))
        argv = (prefix + ['exec', '-T', 'api', 'python'] if inside else [sys.executable])
        argv += ['-m', 'scripts.smoke_milvus_api', '--base', base, '--token', TOKEN]
        if outage: argv += ['--outage']
        report['http_results'][label] = json.loads(command(label, argv, timeout=180))
        save()

    created = ready = False
    save()
    try:
        gate = json.loads((ROOT / GATE).read_text(encoding='utf-8'))
        validate_gate(gate, {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in CRITICAL})
        report['standalone_gate_sha256'] = hashlib.sha256((ROOT / GATE).read_bytes()).hexdigest()
        command('engine-preflight', docker + ['version', '--format', '{{json .Server}}'])
        ready = True
        command('compose-config', prefix + ['config', '--quiet'])
        command('milvus-image-pull', prefix + ['pull', 'milvus'], timeout=900)
        command('api-image-build', prefix + ['--progress', 'plain', 'build', 'api'], timeout=900)
        created = True
        command('milvus-start', prefix + ['up', '-d', '--wait', '--wait-timeout', '240', 'milvus'], timeout=270)
        report['active_stage'] = 'provision-and-baseline'; save()
        exact = exact_service()
        # Warm and verify the frozen model weights before sharing the CPU encoder.
        exact.search('退货退款', 'demo-a', mode='dense')
        backend = MilvusANN(exact.index)
        backend.provision()
        remote = KnowledgeSearch(encoder=exact.encoder, dense_backend=backend)
        report['database_before_restart'] = compare(exact, remote); save()
        command('api-start', prefix + ['up', '-d', '--wait', '--wait-timeout', '90', 'api'], timeout=120)
        command('joint-service-images', prefix + ['images', '--format', 'json'])
        command('joint-container-state', prefix + ['ps', '--format', 'json'])
        command('api-nonroot', prefix + ['exec', '-T', 'api', 'python', '-c',
                'import os; assert os.getuid()!=0; print("non-root uid="+str(os.getuid()))'])
        http('http-before-host')
        http('http-before-container', inside=True)
        # Warm API stays alive. Explicit keyword requests are independent, not an automatic fallback.
        command('stop-own-milvus', prefix + ['stop', 'milvus'], timeout=90)
        http('http-outage-host', outage=True)
        http('http-outage-container', inside=True, outage=True)
        command('restart-own-milvus', prefix + ['up', '-d', '--wait', '--wait-timeout', '240', 'milvus'], timeout=270)
        for title in (True, False):
            backend.client.call('collections/load', {'collectionName': backend.collection(title)})
        report['active_stage'] = 'database-after-restart'; save()
        report['database_after_restart'] = compare(exact, remote); save()
        http('http-recovered-warm-api')
        command('api-restart', prefix + ['restart', 'api'], timeout=90)
        command('api-restart-ready', prefix + ['up', '-d', '--wait', '--wait-timeout', '90', 'api'], timeout=120)
        http('http-restart-host')
        http('http-restart-container', inside=True)
        report['status'] = 'passed'
    except Exception as exc:
        report.update(status='failed' if ready else 'blocked', error_type=type(exc).__name__)
        report['failed_stage'] = report.get('active_stage', 'standalone-baseline-gate')
    finally:
        if created:
            try: command('container-logs', prefix + ['logs', '--no-color'], timeout=60)
            except Exception: pass
            try:
                command('cleanup-own-project', prefix + ['down'], timeout=120)
                report['cleanup_passed'] = True
            except Exception:
                report.update(status='failed', cleanup_passed=False)
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        save()
        print('Status: ' + report['status'], flush=True)
        print('Evidence: ' + str(out), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

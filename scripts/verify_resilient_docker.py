"""P6.1 actual CPU/Milvus HTTP budget/degradation/circuit containers; no model key."""
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone

from logimate.milvus_backend import MilvusANN
from logimate.retrieval import ROOT, cache_dir
from scripts.smoke_milvus_api import exact_service
from scripts.verify_milvus import docker_commands
from scripts.verify_milvus_api import joint_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desktop-linux', action='store_true')
    parser.add_argument('--port', type=int, default=18083, choices=range(1, 65536), metavar='PORT')
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    out = ROOT / 'docs/evidence/p6' / ('docker-' + stamp); out.mkdir(parents=True, exist_ok=False)
    project = 'logimate-p6-' + stamp.lower()
    docker, prefix, images = docker_commands(project, out, True, args.desktop_linux, True)
    runtime = joint_runtime(out, project, cache_dir(), args.port)
    config = json.loads(runtime.read_text(encoding='utf-8'))
    config['services']['api']['environment']['LOGIMATE_RESILIENT_RETRIEVAL'] = '1'
    runtime.write_text(json.dumps(config, indent=2), encoding='utf-8')
    prefix += ['-f', str(runtime)]
    report = {'status': 'running', 'phase': 'P6.1-resilient-retrieval-api', 'project': project,
        'real_llm_calls': False, 'graph_connected_to_p6': False, 'simulated_business_data': True,
        'minio_s3_tested': False, 'steps': [], 'results': {}, 'effective_images': images,
        'input_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for pattern in ['logimate/*.py', 'scripts/*resilient*.py', 'scripts/retrieval_fault_worker.py',
                            'scripts/verify_milvus.py', 'scripts/verify_milvus_api.py', 'Dockerfile.retrieval',
                            'requirements-retrieval.lock', 'data/knowledge/*.json', 'compose.milvus*.json',
                            'deploy/milvus-embed-*.yaml'] for p in ROOT.glob(pattern)}}
    def save(): (out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    def command(name, argv, timeout=30):
        report['active_stage'] = name; save()
        try:
            p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
            code, stdout, stderr = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as exc:
            def dec(v): return v.decode('utf-8', errors='replace') if isinstance(v, bytes) else v or ''
            code, stdout, stderr = 124, dec(exc.stdout), dec(exc.stderr) + '\nCommand timed out'
        except OSError as exc: code, stdout, stderr = 127, '', str(exc)
        (out / (name + '.txt')).write_text(stdout + stderr, encoding='utf-8')
        report['steps'].append({'name': name, 'exit_code': code}); save()
        print(name + ': exit ' + str(code), flush=True)
        if code: raise RuntimeError(name + ' failed')
        return stdout
    def smoke(name, mode):
        raw = command(name, [sys.executable, '-m', 'scripts.smoke_resilient', '--base',
                            f'http://127.0.0.1:{args.port}', '--mode', mode], timeout=120)
        report['results'][name] = json.loads(raw); save()
    created = ready = False
    save()
    try:
        gatepath = ROOT / 'docs/evidence/p5/policy-docker-20261103T142423472388/summary.json'
        gate = json.loads(gatepath.read_text(encoding='utf-8'))
        if gate['status'] != 'passed' or not gate['cleanup_passed'] or not gate['real_llm_calls_verified']:
            raise ValueError('P5.3 real container/model gate not passed')
        for name in ('data/knowledge/corpus-v1.json', 'data/knowledge/index-v1.json', 'data/knowledge/qa-v1.json'):
            if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != gate['input_sha256'][name]:
                raise ValueError('frozen v1 dataset changed')
        report['p53_gate_sha256'] = hashlib.sha256(gatepath.read_bytes()).hexdigest()
        command('engine-preflight', docker + ['version', '--format', '{{json .Server}}']); ready = True
        command('compose-config', prefix + ['config', '--quiet'])
        command('milvus-image-pull', prefix + ['pull', 'milvus'], timeout=900)
        command('api-image-build', prefix + ['--progress', 'plain', 'build', 'api'], timeout=900)
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
        command('stop-own-milvus', prefix + ['stop', 'milvus'], timeout=90)
        smoke('http-keyword-and-circuit', 'outage')
        command('restart-own-milvus', prefix + ['up', '-d', '--wait', '--wait-timeout', '240', 'milvus'], timeout=270)
        for title in (True, False): backend.client.call('collections/load', {'collectionName': backend.collection(title)})
        smoke('http-half-open-recovery', 'recover')
        smoke('http-burst-admission', 'burst')
        command('api-restart', prefix + ['restart', 'api'], timeout=90)
        command('api-restart-ready', prefix + ['up', '-d', '--wait', '--wait-timeout', '90', 'api'], timeout=120)
        smoke('http-restart-hybrid', 'normal')
        command('container-state', prefix + ['ps', '--format', 'json'])
        report['status'] = 'passed'
    except Exception as exc:
        report.update(status='failed' if ready else 'blocked', error_type=type(exc).__name__,
                      failed_stage=report.get('active_stage', 'P5.3-gate'))
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

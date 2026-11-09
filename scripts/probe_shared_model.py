"""One small host model probe; optionally continue current P6.2 only after success."""
import argparse
import getpass
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from scripts.probe_model_network import PROBE_CODE, ROOT


def classify_host(row):
    if row.get('ok') is True: return 'host_probe_passed_not_yet_container_acceptance'
    status = row.get('http_status', 0)
    return {401:'credential_rejected', 402:'balance_or_billing_rejected', 429:'provider_rate_limit',
        400:'provider_request_or_model_configuration'}.get(status,
        'provider_http_failure' if isinstance(status, int) and status >= 500 else 'host_probe_failed')


def probe(key, env, runner=subprocess.run):
    try:
        result = runner([sys.executable, '-c', PROBE_CODE], cwd=ROOT,
            env=dict(env, DEEPSEEK_API_KEY=key, PYTHONIOENCODING='utf-8'),
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=35)
        if result.returncode: return {'ok': False, 'error_kind': 'probe_process_failure'}
        # Never persist unexpected output containing credentials or raw provider error bodies.
        if key in result.stdout: return {'ok': False, 'error_kind': 'sensitive_output_withheld'}
        row = json.loads(result.stdout)
        if not isinstance(row, dict) or type(row.get('ok')) is not bool: raise ValueError('invalid probe envelope')
        if row.get('ok') is True and (row.get('http_status') != 200 or not row.get('response_id')):
            return {'ok': False, 'error_kind': 'invalid_provider_success'}
        allowed = {'ok', 'endpoint', 'model', 'http_status', 'error_kind', 'elapsed_ms',
                   'response_id', 'usage', 'actual_model', 'exception_type', 'errno'}
        return {k: v for k, v in row.items() if k in allowed}
    except subprocess.TimeoutExpired: return {'ok': False, 'error_kind': 'probe_timeout'}
    except (ValueError, OSError, TypeError): return {'ok': False, 'error_kind': 'probe_execution_or_output_error'}


def continue_verification(row, enabled, env, runner=subprocess.call):
    if not enabled or row.get('ok') is not True: return None
    # The key remains in child environment, never command arguments or files.
    return runner([sys.executable, '-m', 'scripts.verify_shared_graph_docker',
        '--desktop-linux', '--local-parent', '--live'], cwd=ROOT, env=env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--then-verify', action='store_true')
    args = parser.parse_args()
    key = (os.environ.get('DEEPSEEK_API_KEY') or getpass.getpass('DeepSeek API key (hidden, small probe + optional P6.2 only): ')).strip()
    if not key:
        print('Status: empty key; no model or Docker request made', flush=True)
        return 1
    env = dict(os.environ, DEEPSEEK_API_KEY=key, PYTHONIOENCODING='utf-8')
    out = ROOT / 'docs/evidence/p6' / datetime.now(timezone.utc).strftime('model-key-probe-%Y%m%dT%H%M%S%f')
    out.mkdir(parents=True, exist_ok=False)
    row = probe(key, env)
    report = {'phase': 'P6.2-host-model-auth-precheck', 'host': row,
        'classification': classify_host(row), 'container_tested': False,
        'probe_only_is_not_graph_or_container_acceptance': True,
        'verification_requested': args.then_verify,
        'probe_code_sha256': hashlib.sha256(PROBE_CODE.encode()).hexdigest(),
        'script_sha256': hashlib.sha256((ROOT/'scripts/probe_shared_model.py').read_bytes()).hexdigest()}
    def save(): (out/'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    print('Host probe: '+json.dumps(row, ensure_ascii=False), flush=True)
    print('Classification: '+report['classification'], flush=True)
    print('Evidence: '+str(out), flush=True)
    if not row.get('ok'): return 1
    code = continue_verification(row, args.then_verify, env)
    report['verification_exit_code'] = code; save()
    return code if code is not None else 0


if __name__ == '__main__': raise SystemExit(main())

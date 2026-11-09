import hashlib
from io import BytesIO
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from scripts import probe_graph_runtime as probe


def image_fixture(root):
    folder = root / 'docs/evidence/docker-stage/run-1'
    folder.mkdir(parents=True)
    hashes = {}
    for name in ['requirements.lock', 'data/synthetic_v1.json', 'prompts/route-v1.txt', 'prompts/tools-v1.txt']:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('fixture', encoding='utf-8')
        hashes[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    image = 'sha256:' + 'a' * 64
    (folder / 'image.txt').write_text('COMMAND\n' + json.dumps({'Image': image}) + '\nEXIT_CODE: 0', encoding='utf-8')
    (folder / 'summary.json').write_text(json.dumps({'acceptance_phase': 'P4.2-graph-mcp',
        'input_sha256': hashes, 'steps': [{'name': 'image-health-ports', 'exit_code': 0, 'log': 'image.txt'}]}), encoding='utf-8')
    return image


def test_exact_recorded_image_only_and_dependency_drift_refused(tmp_path):
    image = image_fixture(tmp_path)
    assert probe.select_image(tmp_path)[0] == image
    (tmp_path / 'requirements.lock').write_text('changed', encoding='utf-8')
    with pytest.raises(RuntimeError, match='mismatch'):
        probe.select_image(tmp_path)


@pytest.mark.parametrize('mcp_ok', [True, False])
def test_probe_mounts_only_app_readonly_and_gates_full_acceptance(monkeypatch, tmp_path, mcp_ok):
    image = image_fixture(tmp_path)
    (tmp_path / 'logimate').mkdir()
    (tmp_path / 'logimate/api.py').write_text('fixture', encoding='utf-8')
    monkeypatch.setattr(probe, 'ROOT', tmp_path)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'EXPLICIT_TEST_CREDENTIAL')
    monkeypatch.setattr('sys.argv', ['probe_graph_runtime', '--then-verify'])
    commands, continuations = [], []
    def fake_run(argv, **kwargs):
        commands.append(argv)
        return SimpleNamespace(returncode=0, stdout=b'{}' if kwargs.get('text') is None else 'ok', stderr='')
    monkeypatch.setattr(probe.subprocess, 'run', fake_run)
    monkeypatch.setattr(probe.subprocess, 'call', lambda argv, **kwargs: continuations.append(argv) or 0)
    class Response(BytesIO):
        code = 200
    success = {'simulated': True, 'status': 'answered', 'diagnosis': {'findings': [
        {'code': 'REFUND_OVERDUE'}, {'code': 'FINANCE_FAILURE_RECORDED'}]},
        'tool_calls': [{}] * 4, 'provider_calls': [{}] * 5, 'usage': {'total_tokens': 5},
        'tool_transport': 'mcp-stdio', 'tool_transport_audit': [{}] * 4}
    def respond(req, **kwargs):
        if req.full_url.endswith('/health'):
            return Response(b'{"status":"ok"}')
        if '/graph/mcp/' in req.full_url and not mcp_ok:
            body = {'status': 'unavailable', 'failure_details': [{'category': 'provider_http', 'http_status': 429}],
                    'reason': 'EXPLICIT_TEST_CREDENTIAL'}
            raise HTTPError(req.full_url, 503, 'failure', {}, BytesIO(json.dumps(body).encode()))
        return Response(json.dumps(success).encode())
    monkeypatch.setattr(probe, 'urlopen', respond)
    assert probe.main() == (0 if mcp_ok else 1)
    assert bool(continuations) == mcp_ok
    assert all('EXPLICIT_TEST_CREDENTIAL' not in str(args) for args in commands + continuations)
    assert not any('build' in args for args in commands)
    run = next(args for args in commands if args[:2] == ['docker', 'run'])
    assert run[-1] == image and 'readonly' in run[run.index('--mount') + 1]
    assert any(args[:3] == ['docker', 'rm', '-f'] and args[-1].startswith('logimate-graph-probe-') for args in commands)
    for p in (tmp_path / 'docs/evidence/docker-stage').glob('graph-probe-*/*'):
        assert 'EXPLICIT_TEST_CREDENTIAL' not in p.read_text(encoding='utf-8')

"""Verifier orchestration tests only; never evidence of a real Docker acceptance."""
import json
from types import SimpleNamespace

import pytest

from scripts import verify_docker as verifier


def setup_fake(monkeypatch, tmp_path, fail_live=False):
    for filename in ("Dockerfile", "compose.yaml", "requirements.lock", "data/synthetic_v1.json"):
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "TEST_SECRET_FOR_CONTAINER_VERIFIER")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        output, code = "ok", 0
        if args[:2] == ["docker", "version"]:
            output = json.dumps({"Version": "fixture-version"})
        elif args[:3] == ["docker", "image", "inspect"]:
            output = '["PATH=/usr/bin"]'
        elif args[:2] == ["docker", "inspect"] and args[3] == "{{.Image}}":
            output = "sha256:test-image"
        elif "ps" in args and "-q" in args:
            output = "test-container"
        elif "logs" in args:
            output = "TEST_SECRET_FOR_CONTAINER_VERIFIER"
        if fail_live and args[0] != "docker" and "P2 container" in args[-1]:
            code = 1
        return SimpleNamespace(returncode=code, stdout=output, stderr="")

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)
    return calls


def test_live_gates_and_secret_redaction(monkeypatch, tmp_path):
    calls = setup_fake(monkeypatch, tmp_path)
    assert verifier.run(p2=True) == 0
    run = next((tmp_path/"docs/evidence/docker-stage").iterdir())
    summary = json.loads((run/"summary.json").read_text(encoding="utf-8"))
    steps = {s["name"] for s in summary["steps"]}
    assert {"p2-live-agent-http", "p2-missing-key-fails-closed", "p2-after-restart-live-agent"} <= steps
    assert summary["image_credential_check"].startswith("passed")
    assert all("TEST_SECRET_FOR_CONTAINER_VERIFIER" not in p.read_text(encoding="utf-8") for p in run.iterdir())
    assert all("TEST_SECRET_FOR_CONTAINER_VERIFIER" not in str(args) for args in calls)
    assert any("config" in args and "--quiet" in args for args in calls)


def test_live_failure_blocks_success_and_cleans_own_project(monkeypatch, tmp_path):
    calls = setup_fake(monkeypatch, tmp_path, fail_live=True)
    assert verifier.run(p2=True) == 1
    run = next((tmp_path/"docs/evidence/docker-stage").iterdir())
    summary = json.loads((run/"summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert summary["cleanup_passed"]
    assert any(args[-1] == "down" for args in calls)
    assert not any("restart" in args for args in calls)


def test_p3_gate_requires_graph_both_boots_and_keeps_p2_regression(monkeypatch, tmp_path):
    setup_fake(monkeypatch, tmp_path)
    assert verifier.run(p3=True)==0
    run = next((tmp_path/"docs/evidence/docker-stage").iterdir())
    summary=json.loads((run/'summary.json').read_text(encoding='utf-8'))
    assert summary['acceptance_phase']=='P3-five-agent'
    names={s['name'] for s in summary['steps']}
    assert {'p3-langgraph-version','p3-live-graph-http','p3-after-restart-live-graph',
            'p3-missing-key-fails-closed','p3-auth-and-input-guards',
            'p3-after-restart-input-guards','p2-live-agent-http','p2-after-restart-live-agent'} <= names


def test_p4_gate_runs_real_protocol_command_both_boots_without_model_key(monkeypatch, tmp_path):
    calls = setup_fake(monkeypatch, tmp_path)
    path = tmp_path / 'scripts/verify_mcp.py'
    path.parent.mkdir()
    path.write_text('fixture', encoding='utf-8')
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    assert verifier.run(p4=True) == 0
    out = next((tmp_path / 'docs/evidence/docker-stage').iterdir())
    summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
    assert summary['acceptance_phase'] == 'P4-mcp'
    assert {'p4-mcp-sdk-version', 'p4-mcp-stdio-protocol', 'p4-after-restart-mcp'} <= {s['name'] for s in summary['steps']}
    assert sum('scripts.verify_mcp' in args for args in calls) == 2
    assert not any('p2-runtime.json' in str(arg) for args in calls for arg in args)


@pytest.mark.parametrize('failure',[False,True])
def test_p5_real_retrieval_gate_without_key_and_failure_blocks_restart(monkeypatch,tmp_path,failure):
    calls=setup_fake(monkeypatch,tmp_path)
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    original=verifier.subprocess.run
    def run(args,**kwargs):
        result=original(args,**kwargs)
        if failure and 'scripts.smoke_retrieval' in args:
            return SimpleNamespace(returncode=1,stdout='explicit retrieval failure',stderr='')
        return result
    monkeypatch.setattr(verifier.subprocess,'run',run)
    assert verifier.run(p5_retrieval=True)==int(failure)
    out=next((tmp_path/'docs/evidence/docker-stage').iterdir())
    summary=json.loads((out/'summary.json').read_text(encoding='utf-8'))
    assert summary['acceptance_phase']=='P5.1-retrieval' and summary['cleanup_passed']
    config=json.loads((out/'p5-runtime.json').read_text(encoding='utf-8'))
    assert config['services']['api']['volumes'][0]['read_only']
    if failure:assert not any('restart' in args for args in calls)
    else:assert sum('scripts.smoke_retrieval' in args for args in calls)==2


def test_p4_graph_gate_includes_real_mcp_graph_and_local_regression_both_boots(monkeypatch, tmp_path):
    calls = setup_fake(monkeypatch, tmp_path)
    (tmp_path / 'scripts').mkdir()
    for name in ['verify_mcp.py', 'verify_mcp_graph.py']:
        (tmp_path / 'scripts' / name).write_text('fixture', encoding='utf-8')
    assert verifier.run(p4_graph=True) == 0
    out = next((tmp_path / 'docs/evidence/docker-stage').iterdir())
    summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
    assert summary['acceptance_phase'] == 'P4.2-graph-mcp'
    names = {s['name'] for s in summary['steps']}
    assert {'p4-graph-live-mcp', 'p4-graph-restart-live', 'p4-graph-input-guards', 'p4-graph-no-key',
            'p4-graph-restart-guards', 'p4-graph-paired-fixture', 'p3-live-graph-http',
            'p3-after-restart-live-graph', 'p2-live-agent-http', 'p2-after-restart-live-agent'} <= names
    assert any('/v1/graph/mcp/queries' in str(arg) for args in calls for arg in args)


def test_p4_graph_failed_mcp_live_request_blocks_acceptance_and_cleans_up(monkeypatch, tmp_path):
    calls = setup_fake(monkeypatch, tmp_path)
    (tmp_path / 'scripts').mkdir()
    for name in ['verify_mcp.py', 'verify_mcp_graph.py']:
        (tmp_path / 'scripts' / name).write_text('fixture', encoding='utf-8')
    original = verifier.subprocess.run
    def fail_mcp(args, **kwargs):
        result = original(args, **kwargs)
        if args[0] != 'docker' and '/v1/graph/mcp/queries' in args[-1] and 'DeepSeek' in args[-1]:
            return SimpleNamespace(returncode=1, stdout='explicit injected MCP failure', stderr='')
        return result
    monkeypatch.setattr(verifier.subprocess, 'run', fail_mcp)
    assert verifier.run(p4_graph=True) == 1
    out = next((tmp_path / 'docs/evidence/docker-stage').iterdir())
    summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
    assert summary['status'] == 'failed' and summary['cleanup_passed']
    assert not any('restart' in args for args in calls)


def test_p3_model_failure_cannot_mark_container_accepted(monkeypatch,tmp_path):
    calls=setup_fake(monkeypatch,tmp_path)
    original=verifier.subprocess.run
    def fail_graph(args,**kwargs):
        result=original(args,**kwargs)
        if args[0]!='docker' and 'real P3 five-agent container' in args[-1]:
            return SimpleNamespace(returncode=1,stdout='graph failed',stderr='')
        return result
    monkeypatch.setattr(verifier.subprocess,'run',fail_graph)
    assert verifier.run(p3=True)==1
    run=next((tmp_path/'docs/evidence/docker-stage').iterdir())
    summary=json.loads((run/'summary.json').read_text(encoding='utf-8'))
    assert summary['status']=='failed' and summary['cleanup_passed']
    assert not any('restart' in args for args in calls)


def test_engine_failure_never_prompts_key_or_builds(monkeypatch,tmp_path):
    calls=setup_fake(monkeypatch,tmp_path)
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    def unavailable(args,**kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1,stdout='null',stderr='engine pipe missing')
    def no_prompt(*args,**kwargs):
        raise AssertionError('key prompt before engine readiness')
    monkeypatch.setattr(verifier.subprocess,'run',unavailable)
    monkeypatch.setattr(verifier.getpass,'getpass',no_prompt)
    assert verifier.run(p3=True)==1
    assert not any('build' in args or 'up' in args for args in calls)


def test_optional_desktop_start_precedes_retry(monkeypatch,tmp_path):
    calls=setup_fake(monkeypatch,tmp_path)
    original=verifier.subprocess.run
    count=0
    def delayed(args,**kwargs):
        nonlocal count
        if args[:2]==['docker','version']:
            count+=1
            if count==1:
                calls.append(args)
                return SimpleNamespace(returncode=1,stdout='null',stderr='not ready')
        return original(args,**kwargs)
    monkeypatch.setattr(verifier.subprocess,'run',delayed)
    monkeypatch.setattr(verifier.time,'sleep',lambda _:None)
    assert verifier.run(p3=True,start_desktop=True,wait_engine=1)==0
    assert any(args[:3]==['docker','desktop','start'] for args in calls)
    assert count==2


def test_http_503_reason_is_preserved_without_weakening_acceptance(monkeypatch,tmp_path,capsys):
    from io import BytesIO
    from urllib.error import HTTPError
    from unittest.mock import patch
    calls=setup_fake(monkeypatch,tmp_path)
    assert verifier.run(p2=True)==0
    code=next(args[-1] for args in calls if args[0]!='docker' and 'def call(oid' in args[-1])
    body=json.dumps({'status':'unavailable','reason':'DeepSeek HTTP 401','model_called':True}).encode()
    failure=HTTPError('http://127.0.0.1:18080/v1/agent/queries',503,'unavailable',{},BytesIO(body))
    with patch('urllib.request.urlopen',side_effect=failure):
        with pytest.raises(AssertionError,match='Agent HTTP failure'):
            exec(code,{})
    output=capsys.readouterr().out
    assert 'DeepSeek HTTP 401' in output and 'http_status' in output

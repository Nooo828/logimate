"""Real Docker acceptance only. Stops at failed engine preflight; never simulates success.

Run from project root: python -m scripts.verify_docker
Optional: LOGIMATE_PORT (default 18080 for this script).
Evidence is append-only by timestamped directory. Only this verification project's
containers/networks are removed; image/cache are retained. No volume deletion.
"""
import argparse
import getpass
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(p2=False, p3=False, start_desktop=False, wait_engine=0, p4=False, p4_graph=False, p5_retrieval=False):
    if p4_graph:
        p3 = p4 = True
    if p3:
        p2 = True  # P3 acceptance also regresses the real single-agent endpoint.
    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}"
    out = ROOT / "docs" / "evidence" / "docker-stage" / run_id
    out.mkdir(parents=True, exist_ok=False)
    project = "logimate-verify-" + run_id.lower()
    env = dict(os.environ, LOGIMATE_PORT=os.environ.get("LOGIMATE_PORT", "18080"),
               LOGIMATE_DEMO_TOKEN="docker-verification-only", PYTHONIOENCODING="utf-8")
    prefix = ["docker", "compose", "--project-name", project, "-f", "compose.yaml"]
    if p5_retrieval:
        from logimate.retrieval import cache_dir
        override = out/'p5-runtime.json'
        override.write_text(json.dumps({'services':{'api':{'build':{'dockerfile':'Dockerfile.retrieval'},
            'environment':{'LOGIMATE_RETRIEVAL_CACHE':'/models','LOGIMATE_LEXICAL_CACHE':'/tmp/logimate-lexical'},
            'volumes':[{'type':'bind','source':str(cache_dir().resolve()),'target':'/models','read_only':True}]}}}),encoding='utf-8')
        prefix += ['-f',str(override)]
    if p2:
        override = out / "p2-runtime.json"
        override.write_text(json.dumps({"services": {"api": {"environment": {
            "DEEPSEEK_API_KEY": "${DEEPSEEK_API_KEY:?P2 requires key}",
            "LOGIMATE_MODEL": "${LOGIMATE_MODEL:-deepseek-flash}"}}}}, indent=2), encoding="utf-8")
        prefix += ["-f", str(override)]
    result = {"started_at_utc": started.isoformat(), "status": "running", "project": project,
              "acceptance_phase": "P5.1-retrieval" if p5_retrieval else "P4.2-graph-mcp" if p4_graph else "P4-mcp" if p4 else "P3-five-agent" if p3 else "P2-live-agent" if p2 else "P1-rules",
              "simulated_business_data": True, "steps": [],
              "input_sha256": {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
                               for p in ("Dockerfile", "compose.yaml", "requirements.lock", "data/synthetic_v1.json")}}
    if p2 or p4 or p5_retrieval:
        for pattern in ("logimate/*.py", "prompts/*.txt"):
            for path in ROOT.glob(pattern):
                result["input_sha256"][path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if p5_retrieval:
        for pattern in ('Dockerfile.retrieval','requirements-retrieval.lock','data/knowledge/*.json','scripts/smoke_retrieval.py'):
            for path in ROOT.glob(pattern): result['input_sha256'][path.relative_to(ROOT).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    if p4:
        path = ROOT / "scripts/verify_mcp.py"
        result["input_sha256"]["scripts/verify_mcp.py"] = hashlib.sha256(path.read_bytes()).hexdigest()
    if p4_graph:
        path = ROOT / "scripts/verify_mcp_graph.py"
        result["input_sha256"]["scripts/verify_mcp_graph.py"] = hashlib.sha256(path.read_bytes()).hexdigest()

    def save():
        (out/"summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def command(label, args, timeout=30, required=True):
        try:
            p = subprocess.run(args, cwd=ROOT, env=env, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=timeout)
            code, output = p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired as exc:
            code = 124
            def decode(value):
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
            output = decode(exc.stdout) + decode(exc.stderr) + f"\nTIMEOUT after {timeout}s\n"
        except OSError as exc:
            code, output = 127, str(exc)
        # Keep Windows paths below legacy limits even in nested test workspaces.
        path = f"{len(result['steps'])+1:02d}-{label[:20]}.txt"
        safe_output = output
        if env.get("DEEPSEEK_API_KEY"):
            safe_output = safe_output.replace(env["DEEPSEEK_API_KEY"], "[REDACTED]")
        (out/path).write_text("COMMAND: " + subprocess.list2cmdline(args) + "\n" + safe_output + f"\nEXIT_CODE: {code}\n", encoding="utf-8")
        result["steps"].append({"name": label, "exit_code": code, "log": path})
        save()
        print(f"{label}: exit {code}", flush=True)
        if required and code:
            raise RuntimeError(f"{label} failed; see {out/path}")
        return code, output

    created = False
    engine_ready = False
    primary_error = None
    try:
        # Do not build or create any containers without a verified server response.
        code, raw = command("engine-preflight", ["docker", "version", "--format", "{{json .Server}}"], timeout=15, required=False)
        if code and start_desktop:
            command("desktop-start", ["docker", "desktop", "start", "--timeout", "60"], timeout=65, required=False)
        deadline=time.monotonic()+wait_engine
        probe=0
        while code and time.monotonic()<deadline:
            time.sleep(min(5,max(0,deadline-time.monotonic())))
            probe+=1
            code,raw=command(f"engine-probe-{probe}",["docker","version","--format","{{json .Server}}"],timeout=10,required=False)
        if code:
            raise RuntimeError("Docker Server unavailable; engine check failed before any build or model calls")
        server = json.loads(raw)
        if not isinstance(server, dict) or not server.get("Version"):
            raise RuntimeError("Docker server version missing")
        result["server_version"] = server["Version"]
        engine_ready = True
        if p2 and not env.get("DEEPSEEK_API_KEY"):
            if not sys.stdin.isatty():
                raise RuntimeError("Live acceptance requires DEEPSEEK_API_KEY or a local interactive terminal")
            env["DEEPSEEK_API_KEY"] = getpass.getpass("DeepSeek API key (hidden, this run only): ")
        if p2 and not env.get("DEEPSEEK_API_KEY"):
            raise RuntimeError("Empty DeepSeek API key; no live model calls performed")
        command("compose-config", prefix+["config", "--quiet"] if p2 else prefix+["config"])
        command("image-build", prefix+["--progress", "plain", "build"], timeout=600)
        created = True
        command("container-start", prefix+["up", "-d", "--wait", "--wait-timeout", "90"], timeout=110)
        command("container-state", prefix+["ps", "--format", "json"])
        command("container-identity", prefix+["exec", "-T", "api", "python", "-c",
                "import os; print('uid='+str(os.getuid())); assert os.getuid() != 0"])
        _, container_id = command("container-id", prefix+["ps", "-q", "api"])
        command("image-health-ports", ["docker", "inspect", "--format",
                '{"Image":"{{.Image}}","Health":"{{.State.Health.Status}}","User":"{{.Config.User}}","Ports":{{json .NetworkSettings.Ports}}}',
                container_id.strip()])
        check = '''import json,urllib.request,urllib.error
checks=[]
def call(name,path,body=None,auth=True,expected=200):
    h={'Content-Type':'application/json'}
    if auth:h['Authorization']='Bearer docker-verification-only'
    r=urllib.request.Request('http://127.0.0.1:8000'+path,headers=h,data=json.dumps(body).encode() if body is not None else None)
    try:res=urllib.request.urlopen(r,timeout=5)
    except urllib.error.HTTPError as e:res=e
    with res: status,payload=res.code,json.loads(res.read())
    assert status==expected,(name,status,payload)
    assert payload['simulated'] is True,(name,payload)
    if name=='refund':
        assert {f['code'] for f in payload['findings']}=={'REFUND_OVERDUE','FINANCE_FAILURE_RECORDED'}
        assert len(payload['evidence'])==4
    if name=='no-invented-cause':
        assert {f['code'] for f in payload['findings']}=={'REFUND_OVERDUE'}
    checks.append({'name':name,'status':status,'body':payload})
call('health','/health')
call('raw','/v1/orders/SIM-002')
call('refund','/v1/diagnoses',{'order_id':'SIM-002'})
call('no-invented-cause','/v1/diagnoses',{'order_id':'SIM-008'})
call('missing','/v1/diagnoses',{'order_id':'SIM-999'},expected=404)
call('tenant','/v1/diagnoses',{'order_id':'SIM-009'},expected=404)
call('auth','/v1/orders/SIM-002',auth=False,expected=401)
call('invalid','/v1/diagnoses',{'order_id':'invalid'},expected=422)
print(json.dumps({'transport':'real HTTP inside actual container','checks':checks},ensure_ascii=False))
'''
        command("container-http", prefix+["exec", "-T", "api", "python", "-c", check])
        host_check = check.replace("127.0.0.1:8000", "127.0.0.1:"+env["LOGIMATE_PORT"]).replace(
            "real HTTP inside actual container", "real HTTP from host mapped port")
        command("host-mapped-http", [sys.executable, "-c", host_check])
        p2_check = '''import json,urllib.request,urllib.error
checks=[]
def call(oid,expected,codes=None):
    req=urllib.request.Request('http://127.0.0.1:PORT/v1/agent/queries',
        headers={'Content-Type':'application/json','Authorization':'Bearer docker-verification-only'},
        data=json.dumps({'query':oid+'退货入库后未退款，请核对四个业务域。'}).encode())
    try:
        with urllib.request.urlopen(req,timeout=90) as r:
            body=json.loads(r.read()); assert r.status==200
    except urllib.error.HTTPError as e:
        try: failure=json.loads(e.read())
        except (ValueError,UnicodeError): failure={}
        safe={k:failure[k] for k in ('implementation','status','reason','error','model_called','usage','failure_details','model_attempts','events','domain_results','tool_transport_audit','tool_startup_ms','elapsed_ms') if k in failure}
        print(json.dumps({'failed_order':oid,'http_status':e.code,'response':safe,'completed_checks':checks}),flush=True)
        raise AssertionError('Agent HTTP failure; response details saved above') from None
    assert body['implementation']=='single-agent-v1'
    assert body['simulated'] and body['model_called']
    assert body['status']==expected,(oid,body['status'],body.get('reason'))
    assert len(body['provider_calls'])>=2
    assert body['usage']['total_tokens']>0
    if codes is not None:
        assert len(body['tool_calls'])==4
        assert {f['code'] for f in body['diagnosis']['findings']}==set(codes)
        assert len(body['diagnosis']['evidence'])==4
    else: assert body['records']=={} and not body['tool_calls']
    checks.append({'order_id':oid,'status':body['status'],'body':body})
call('SIM-002','answered',['REFUND_OVERDUE','FINANCE_FAILURE_RECORDED'])
call('SIM-008','answered',['REFUND_OVERDUE'])
call('SIM-009','not_found')
print(json.dumps({'transport':'host HTTP to real P2 container; real DeepSeek','checks':checks}))
'''.replace("PORT", env["LOGIMATE_PORT"])
        if p2:
            command("p2-live-agent-http", [sys.executable, "-c", p2_check], timeout=200)
            no_key = "import os; os.environ.pop('DEEPSEEK_API_KEY',None); from fastapi.testclient import TestClient; from logimate.api import create_app; c=TestClient(create_app()); r=c.post('/v1/agent/queries',json={'query':'SIM-002'},headers={'Authorization':'Bearer docker-verification-only'}); assert r.status_code==503; print('unconfigured model: 503, no mock fallback')"
            command("p2-missing-key-fails-closed", prefix+["exec", "-T", "api", "python", "-c", no_key])
        if p3:
            command("p3-langgraph-version", prefix+["exec", "-T", "api", "python", "-c",
                "from importlib.metadata import version; v=version('langgraph'); print('langgraph='+v); assert v=='1.2.18'"])
            p3_check = p2_check.replace('/v1/agent/queries', '/v1/graph/queries').replace(
                'single-agent-v1', 'langgraph-five-agent-v1').replace('real P2 container', 'real P3 five-agent container')
            p3_check = p3_check.replace("assert len(body['tool_calls'])==4", "assert len(body['tool_calls'])==4; assert {c['agent'] for c in body['provider_calls']}=={'coordinator','order','logistics','warehouse','aftersales'}; assert body['events'].count('aggregate')==1; assert body['events'].index('order') < body['events'].index('logistics')")
            command("p3-live-graph-http", [sys.executable, "-c", p3_check], timeout=200)
            command("p3-missing-key-fails-closed", prefix+["exec", "-T", "api", "python", "-c",
                no_key.replace('/v1/agent/queries', '/v1/graph/queries')])
            guards = '''import json,urllib.request,urllib.error
base='http://127.0.0.1:PORT/v1/graph/queries'
results=[]
for name,payload,auth,expected in [('auth',{'query':'SIM-002'},False,401),('extra-tenant',{'query':'SIM-002','tenant_id':'demo-b'},True,422),('empty-query',{'query':''},True,422)]:
    h={'Content-Type':'application/json'}
    if auth:h['Authorization']='Bearer docker-verification-only'
    req=urllib.request.Request(base,headers=h,data=json.dumps(payload).encode())
    try:r=urllib.request.urlopen(req,timeout=5)
    except urllib.error.HTTPError as e:r=e
    with r: body=json.loads(r.read()); status=r.code
    assert status==expected,(name,status)
    assert body['simulated'] is True
    results.append({'name':name,'status':status})
print(json.dumps({'graph_input_guards':results}))
'''.replace('PORT',env['LOGIMATE_PORT'])
            command("p3-auth-and-input-guards", [sys.executable, "-c", guards])
        if p4:
            command("p4-mcp-sdk-version", prefix+["exec", "-T", "api", "python", "-c",
                    "import importlib.metadata as m; v=m.version('mcp'); print(v); assert v=='2.3.0'"])
            command("p4-mcp-stdio-protocol", prefix+["exec", "-T", "api", "python", "-m",
                    "scripts.verify_mcp", "--stdout"], timeout=60)
        if p4_graph:
            mcp_graph_check = p3_check.replace('/v1/graph/queries', '/v1/graph/mcp/queries').replace(
                'langgraph-five-agent-v1', 'langgraph-five-agent-mcp-v1').replace(
                'host HTTP to real P3 five-agent container; real DeepSeek', 'host HTTP to MCP-backed LangGraph container; real DeepSeek')
            mcp_graph_check = mcp_graph_check.replace("assert len(body['tool_calls'])==4",
                "assert body['tool_transport']=='mcp-stdio'; assert body['mcp_protocol_version']=='2025-11-25'; assert len(body['tool_transport_audit'])==4; assert all(a.get('server_trace_id') and a.get('evidence') for a in body['tool_transport_audit']); assert len(body['tool_calls'])==4")
            mcp_guards = guards.replace('/v1/graph/queries', '/v1/graph/mcp/queries')
            command("p4-graph-paired-fixture", prefix+["exec", "-T", "api", "python", "-m",
                    "scripts.verify_mcp_graph", "--stdout"], timeout=90)
            command("p4-graph-live-mcp", [sys.executable, "-c", mcp_graph_check], timeout=200)
            command("p4-graph-input-guards", [sys.executable, "-c", mcp_guards])
            command("p4-graph-no-key", prefix+["exec", "-T", "api", "python", "-c",
                    no_key.replace('/v1/agent/queries', '/v1/graph/mcp/queries')])
        if p5_retrieval:
            retrieval_check = [sys.executable,'-m','scripts.smoke_retrieval','--base','http://127.0.0.1:'+env['LOGIMATE_PORT'],'--token','docker-verification-only']
            command('p5-real-retrieval-http',retrieval_check,timeout=90)
        command("container-restart", prefix+["restart", "api"])
        command("restart-ready", prefix+["up", "-d", "--wait", "--wait-timeout", "90"], timeout=110)
        command("after-restart-http", prefix+["exec", "-T", "api", "python", "-c", check])
        command("after-restart-host-http", [sys.executable, "-c", host_check])
        if p5_retrieval:
            command('p5-restart-retrieval-http',retrieval_check,timeout=90)
        if p4:
            command("p4-after-restart-mcp", prefix+["exec", "-T", "api", "python", "-m",
                    "scripts.verify_mcp", "--stdout"], timeout=60)
        if p4_graph:
            command("p4-graph-restart-live", [sys.executable, "-c", mcp_graph_check], timeout=200)
            command("p4-graph-restart-guards", [sys.executable, "-c", mcp_guards])
        if p2:
            command("p2-after-restart-live-agent", [sys.executable, "-c", p2_check], timeout=200)
            if p3:
                command("p3-after-restart-live-graph", [sys.executable, "-c", p3_check], timeout=200)
                command("p3-after-restart-input-guards", [sys.executable, "-c", guards])
            # The API key is passed only into container runtime env, never build args.
            _, raw = command("container-image-id", ["docker", "inspect", "--format", "{{.Image}}", container_id.strip()])
            proc = subprocess.run(["docker", "image", "inspect", "--format", "{{json .Config.Env}}", raw.strip()],
                                  cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=15)
            if proc.returncode != 0 or env["DEEPSEEK_API_KEY"] in proc.stdout or any(
                    item.startswith("DEEPSEEK_API_KEY=") for item in json.loads(proc.stdout)):
                raise RuntimeError("Image credential-isolation check failed; raw output withheld")
            result["image_credential_check"] = "passed; no runtime key in image env"
        result["status"] = "passed"
    except Exception as exc:
        primary_error = str(exc)
        result["status"] = "blocked" if not engine_ready else "failed"
        result["error"] = primary_error
    finally:
        if created:
            command("container-logs", prefix+["logs", "--no-color"], required=False)
            code, _ = command("cleanup-own-project", prefix+["down"], required=False)
            result["cleanup_passed"] = code == 0
            if code:
                result["status"] = "failed"
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save()
        (out/"record.md").write_text(
            f"# Docker验收运行 {run_id}\n\n状态：{result['status']}\n\n"
            + "\n".join(f"- {s['name']}: exit {s['exit_code']}，记录 `{s['log']}`" for s in result["steps"])
            + (f"\n\n阻塞/失败：{primary_error}\n" if primary_error else "\n\n仅验证模拟业务容器；未验证真实业务收益。\n"), encoding="utf-8")
        print(f"Evidence: {out}", flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    phase = parser.add_mutually_exclusive_group()
    phase.add_argument("--p2", action="store_true", help="Validate real P2 model API before and after container restart")
    phase.add_argument("--p3", action="store_true", help="Validate real five-agent API, guards, dependencies and restart, including P2 regression")
    phase.add_argument("--p4", action="store_true", help="Validate real MCP stdio protocol and tenant/ACL guards before/after restart plus P1; no model calls")
    phase.add_argument("--p4-graph", action="store_true", help="Real model MCP graph + local graph/P2/P1 regression and restart; hidden key input")
    phase.add_argument("--p5-retrieval",action="store_true",help="Real CPU keyword/dense/RRF HTTP and restart; readonly public model cache; no LLM key")
    parser.add_argument("--start-desktop",action="store_true",help="If preflight fails, ask installed Docker Desktop to start; no reset or directory repair")
    parser.add_argument("--wait-engine",type=int,default=0,choices=range(0,301),metavar="SECONDS",help="Wait up to this many seconds for engine readiness after optional start")
    options = parser.parse_args()
    sys.exit(run(p2=options.p2,p3=options.p3,p4=options.p4,p4_graph=options.p4_graph,p5_retrieval=options.p5_retrieval,start_desktop=options.start_desktop,wait_engine=options.wait_engine))

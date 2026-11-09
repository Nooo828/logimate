"""Explicit holdout release checks and per-workspace first-exposure reservation."""
import hashlib,json,os
from datetime import datetime,timezone
from dataclasses import dataclass
from .boundary_eval import validate_manifest


@dataclass(frozen=True)
class HoldoutInput:
    case_id:str
    query:str


def select_holdout(root,*,acknowledged=False,live=False,model='deepseek-flash'):
    if not acknowledged:raise ValueError('explicit holdout acknowledgement required')
    lock=json.loads((root/'eval/boundary_v2.freeze.json').read_text(encoding='utf-8'))
    for name,h in lock['sources'].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=h:raise ValueError('P8.4 source drift')
    data=json.loads((root/'eval/boundary_v2.json').read_text(encoding='utf-8'));ds=validate_manifest(data,root)
    if live:
        release=json.loads((root/'eval/holdout_release_v2.json').read_text(encoding='utf-8'))
        if release['model']!=model or release['split']!='holdout' or release['prompt_or_rubric_tuned_on_dev']:raise ValueError('model/release decision mismatch')
        required={'logimate/api.py','logimate/deepseek.py','logimate/single_agent.py','logimate/five_agent.py','logimate/boundary_eval.py',
            'logimate/holdout_collection.py','logimate/dev_collection.py','scripts/collect_boundary_holdout.py','scripts/verify_boundary_holdout_docker.py',
            'eval/boundary_v2.json','eval/boundary_v2.freeze.json','data/synthetic_v1.json','prompts/route-v1.txt','requirements-retrieval.lock'}
        if not required<=set(release['runtime_sha256']) or release['protocol_sha256']!=lock['protocol_sha256']:raise ValueError('incomplete release freeze')
        for name,h in release['runtime_sha256'].items():
            if hashlib.sha256((root/name).read_bytes()).hexdigest()!=h:raise ValueError('released runtime drift')
    cases=[HoldoutInput(c.case_id,c.query) for c in ds.cases if c.split=='holdout']
    if len(cases)!=16:raise ValueError('expected 16 holdout cases')
    return data,cases


def reserve_exposure(path,metadata):
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8') as f:
        json.dump({**metadata,'state':'reserved_before_target_calls','reserved_at_utc':datetime.now(timezone.utc).isoformat()},f,ensure_ascii=False,indent=2)
        f.flush();os.fsync(f.fileno())


def complete_exposure(path,metadata):
    previous=json.loads(path.read_text(encoding='utf-8'))
    # Preserve original reservation even when collection failed or no bill can be proven.
    updated={**previous,**metadata,'finished_at_utc':datetime.now(timezone.utc).isoformat()}
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w',encoding='utf-8') as f:json.dump(updated,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(temporary,path)

"""P8.5 select only public development inputs and guard model user messages."""
from dataclasses import dataclass
import hashlib,json
from .boundary_eval import validate_manifest
from .deepseek import ModelUnavailable


@dataclass(frozen=True)
class DevInput:
    case_id:str
    query:str


def select_dev(root,*,split='dev'):
    if split!='dev':raise ValueError('P8.5 does not enable holdout execution')
    lock=json.loads((root/'eval/boundary_v2.freeze.json').read_text(encoding='utf-8'))
    if lock['target_split_for_p85']!='dev' or lock['holdout_execution_enabled'] is not False:raise ValueError('invalid development lock')
    for name,h in lock['sources'].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=h:raise ValueError('P8.4 frozen input drift')
    data=json.loads((root/'eval/boundary_v2.json').read_text(encoding='utf-8'));ds=validate_manifest(data,root)
    cases=[DevInput(c.case_id,c.query) for c in ds.cases if c.split=='dev']
    if len(cases)!=16:raise ValueError('expected 16 development cases')
    return data,cases


class InputGuard:
    """Before the ledger/external provider, validate only query-derived user payloads."""
    def __init__(self,provider,query):self.provider,self.query=provider,query;self.verified_calls=0

    def complete(self,messages,*args,**kwargs):
        users=[m.get('content') for m in messages if m.get('role')=='user']
        if len(users)!=1:raise ModelUnavailable('Unexpected user payload count',category='configuration')
        text=users[0]
        if text!=self.query:
            try:
                body=json.loads(text)
                if not isinstance(body,dict) or body.get('query')!=self.query or set(body)-{'query','route','required_domains','order_id','policy_context'}:raise ValueError('unexpected fields or query')
            except (ValueError,TypeError):raise ModelUnavailable('User input is outside selected dev query',category='configuration') from None
        self.verified_calls+=1
        return self.provider.complete(messages,*args,**kwargs)

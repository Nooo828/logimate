"""One real-model graph endpoint check; no Docker claim."""
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from logimate.api import create_app


def main():
    with TestClient(create_app()) as client:
        response = client.post('/v1/graph/queries', json={'query':'SIM-002退货入库后未退款，请核对四个业务域。'},
                               headers={'Authorization':'Bearer local-demo-token'})
    body=response.json()
    out=Path(__file__).resolve().parents[1]/'docs/evidence/p3'/datetime.now(timezone.utc).strftime('api-%Y%m%dT%H%M%S%f')
    out.mkdir(parents=True,exist_ok=False)
    (out/'result.json').write_text(json.dumps({'transport':'FastAPI TestClient; real external DeepSeek',
        'http_status':response.status_code,'body':body},ensure_ascii=False,indent=2),encoding='utf-8')
    assert response.status_code==200 and body['status']=='answered'
    assert len(body['provider_calls'])==5 and len(body['tool_calls'])==4
    assert {f['code'] for f in body['diagnosis']['findings']}=={'REFUND_OVERDUE','FINANCE_FAILURE_RECORDED'}
    print(f'Graph API real-model check passed: {out}')


if __name__=='__main__':
    main()

"""Read a collection ledger after interruption. No writes, models, resume or retries."""
import argparse,json,sqlite3
from pathlib import Path


def inspect(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        cells=[dict(r) for r in db.execute('SELECT case_id,mode,state FROM cells')]
        attempts=[dict(r) for r in db.execute('SELECT id,case_id,mode,state,metadata,error FROM attempts')]
        return {'read_only':True,'automatic_retry':False,'cells':cells,'attempts':attempts,
            'known_tokens':sum(json.loads(r['metadata']).get('usage',{}).get('total_tokens',0) for r in attempts if r['metadata']),
            'billing_unknown_attempts':sum(r['state'] in ('started','failed_unknown_billing') for r in attempts)}
    finally:db.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('ledger',type=Path);a=p.parse_args()
    print(json.dumps(inspect(a.ledger),ensure_ascii=False,indent=2))

"""Read-only verification of no-key demonstration receipts and SQLite artifacts."""
import argparse,json,sqlite3
from pathlib import Path
import p88_load as b


def audit(folder,runner_sha):
    r=b.read(folder/'summary.json')
    if r['status']!='passed' or not all(r[k] for k in ['source_unchanged','application_unchanged','scripted_reviewer','simulated','real_native_langgraph']):raise ValueError('unaccepted demonstration')
    if r['runner_sha256']!=runner_sha or r['application_freeze']!=b.freeze_check():raise ValueError('input drift')
    if r['real_model_calls'] or r['real_mcp'] or r['real_cpu_embeddings']:raise ValueError('wrong demo scope')
    rows=r['checks']
    if len(rows)!=18 or len({c['name'] for c in rows})!=18 or any(c['http_status']!=c['expected_status'] for c in rows):raise ValueError('incomplete HTTP transcript')
    if r['api_instances']!=3 or r['api_shutdowns']!=['capture','resume','verify']:raise ValueError('incomplete lifecycle')
    if rows[4]['body']!=rows[5]['body'] or rows[4]['http_status']!=404:raise ValueError('tenant existence leak')
    if rows[7]['http_status']!=503 or rows[10]['http_status']!=401 or rows[11]['http_status']!=409 or rows[14]['http_status']!=409:raise ValueError('guard missing')
    captured=rows[9]['body'];done=rows[16]['body'];replay=rows[17]['body']
    if captured['status']!='waiting_human' or done['status']!='reviewed' or done['version']!=3 or done['capture_count']!=1:raise ValueError('native review lifecycle differs')
    if not replay['replayed'] or len(replay['decisions'])!=2 or done['thread_id']!=r['thread_id']:raise ValueError('idempotency differs')
    if done['case']['snapshot']!=captured['case']['snapshot'] or done['case']['diagnosis']!=captured['case']['diagnosis']:raise ValueError('review altered source')
    databases=list(folder.glob('*.sqlite3'))
    if not {p.name for p in databases}>={'cases.sqlite3','cases.checkpoints.sqlite3','cases.review-lock.sqlite3'}:raise ValueError('missing persistent artifacts')
    for path in databases:
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('SQLite integrity failure')
        finally:db.close()
    return {'status':'demo_receipts_audited','checks':18,'api_instances':3,'scripted_reviewer':True,
            'new_model_calls':0,'summary_sha256':b.sha(folder/'summary.json'),
            'artifact_sha256':{p.name:b.sha(p) for p in sorted(folder.iterdir()) if p.is_file() and (p.suffix in ('.sqlite3','.md') or p.name in ('summary.json','progress.json'))}}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('folder',type=Path);a=p.parse_args()
    print(json.dumps(audit(a.folder,b.sha(Path(__file__).with_name('demo_logimate.py'))),ensure_ascii=False))

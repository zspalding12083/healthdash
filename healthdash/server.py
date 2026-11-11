"""HTTP server and JSON API. Python stdlib only.

Read endpoints are open to the LAN. Import and file-upload endpoints (used by `ingest`)
only accept requests from the addresses given with --import-from.
"""

import html
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .store import KINDS, Store, ValidationError

STATIC = os.path.join(os.path.dirname(__file__), 'static')
SHA_RE = re.compile(r'^[0-9a-f]{64}$')
MAX_BODY = 200 * 1024 * 1024
FILE_TYPES = {'.pdf': 'application/pdf', '.tif': 'image/tiff', '.tiff': 'image/tiff',
              '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
              '.txt': 'text/plain; charset=utf-8', '.wav': 'audio/wav',
              '.doc': 'application/msword',
              '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}


def make_handler(store, files_dir, import_from, title='Health Records'):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'healthdash'

        def log_message(self, fmt, *args):  # keep request logs (which include search terms) out of the journal
            pass

        # ---- plumbing ------------------------------------------------------------------

        def _send(self, code, body, ctype='application/json; charset=utf-8', extra=None):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def _err(self, code, msg):
            self._send(code, {'error': msg})

        def _raw_body(self):
            n = int(self.headers.get('Content-Length') or 0)
            if n > MAX_BODY:
                raise ValidationError('request too large')
            return self.rfile.read(n) if n else b''

        def _json(self):
            raw = self._raw_body()
            try:
                return json.loads(raw or b'{}')
            except json.JSONDecodeError:
                raise ValidationError('body must be JSON')

        def _importer(self):
            return self.client_address[0] in import_from

        def _route(self, method):
            u = urlparse(self.path)
            parts = [p for p in u.path.split('/') if p]
            qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
            try:
                return self._dispatch(method, parts, qs)
            except ValidationError as e:
                return self._err(400, str(e))
            except Exception as e:  # noqa: BLE001
                return self._err(500, f'{type(e).__name__}: {e}')

        def do_GET(self):
            self._route('GET')

        do_HEAD = do_GET

        def do_POST(self):
            self._route('POST')

        def do_PUT(self):
            self._route('PUT')

        def do_DELETE(self):
            self._route('DELETE')

        # ---- routes --------------------------------------------------------------------

        def _dispatch(self, method, parts, qs):
            if method == 'GET' and (not parts or parts == ['index.html']):
                return self._static('index.html')
            if method == 'GET' and parts[0] == 'static' and len(parts) == 2:
                return self._static(parts[1])
            if parts[0] == 'file' and len(parts) == 2 and method == 'GET':
                return self._file(parts[1], qs.get('download') == '1')
            if parts[0] != 'api':
                return self._err(404, 'not found')
            p = parts[1:]
            if method == 'GET':
                if p == ['summary']:
                    return self._send(200, store.summary())
                if p == ['timeline']:
                    return self._send(200, store.timeline(qs.get('financial') == '1'))
                if p == ['search']:
                    return self._send(200, store.search(qs.get('q', '')))
                if p == ['labs', 'series']:
                    return self._send(200, store.lab_series())
                if p == ['review']:
                    return self._send(200, store.review_queue())
                if p == ['documents']:
                    return self._send(200, store.documents())
                if len(p) == 2 and p[0] == 'documents':
                    d = store.document(p[1])
                    return self._send(200, d) if d else self._err(404, 'no such document')
                if p == ['backup']:
                    if not self._importer():
                        return self._err(403, 'backups are only allowed from the ingest host')
                    return self._send(200, store.backup_bytes(), 'application/vnd.sqlite3',
                                      {'Content-Disposition': 'attachment; filename="health.db"'})
                if p == ['files', 'missing']:
                    have = {f.split('.')[0] for f in os.listdir(files_dir)}
                    return self._send(200, [d['id'] for d in store.documents() if d['id'] not in have])
                if len(p) == 1 and p[0] in KINDS:
                    return self._send(200, store.list(p[0]))
                if len(p) == 2 and p[0] in KINDS:
                    r = store.get(p[0], p[1])
                    return self._send(200, r) if r else self._err(404, 'not found')
            if method == 'POST':
                if p == ['import']:
                    if not self._importer():
                        return self._err(403, 'import is only allowed from the ingest host')
                    return self._send(200, self._import(self._json()))
                if len(p) == 2 and p[0] == 'files':
                    if not self._importer():
                        return self._err(403, 'uploads are only allowed from the ingest host')
                    return self._upload(p[1], self._raw_body())
                if len(p) == 1 and p[0] in KINDS:
                    return self._send(201, store.create(p[0], self._json()))
            if method == 'PUT':
                if len(p) == 2 and p[0] == 'documents':
                    d = store.update_document(p[1], self._json())
                    return self._send(200, d) if d else self._err(404, 'no such document')
                if len(p) == 2 and p[0] in KINDS:
                    r = store.update(p[0], p[1], self._json())
                    return self._send(200, r) if r else self._err(404, 'not found')
            if method == 'DELETE' and len(p) == 2 and p[0] in KINDS:
                return self._send(200, {'deleted': store.delete(p[0], p[1])})
            return self._err(404, 'not found')

        def _import(self, body):
            result = {}
            if body.get('documents'):
                result['documents'] = store.upsert_documents(body['documents'])
            for kind in KINDS:
                if body.get(kind):
                    result[kind] = store.upsert_imported(kind, body[kind])
            return result

        def _upload(self, name, data):
            sha, _, ext = name.partition('.')
            if not SHA_RE.match(sha) or ('.' + ext.lower()) not in FILE_TYPES:
                raise ValidationError('file name must be <sha256>.<ext>')
            if not store.document(sha):
                raise ValidationError('import the document record first')
            import hashlib
            if hashlib.sha256(data).hexdigest() != sha:
                raise ValidationError('checksum mismatch')
            tmp = os.path.join(files_dir, name + '.tmp')
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, os.path.join(files_dir, sha + '.' + ext.lower()))
            store.set_has_file(sha)
            return self._send(200, {'stored': name})

        def _file(self, sha, download):
            if not SHA_RE.match(sha):
                return self._err(404, 'not found')
            match = [f for f in os.listdir(files_dir) if f.split('.')[0] == sha and not f.endswith('.tmp')]
            if not match:
                return self._err(404, 'file not on the Pi yet')
            fn = match[0]
            ext = os.path.splitext(fn)[1].lower()
            with open(os.path.join(files_dir, fn), 'rb') as f:
                data = f.read()
            doc = store.document(sha) or {}
            nice = os.path.basename(doc.get('organized_path') or doc.get('path') or fn)
            nice = re.sub(r'[^\w.\- ]', '_', nice)
            disp = 'attachment' if download else 'inline'
            return self._send(200, data, FILE_TYPES.get(ext, 'application/octet-stream'),
                              {'Content-Disposition': f'{disp}; filename="{nice}"'})

        def _static(self, name):
            if '/' in name or name.startswith('.'):
                return self._err(404, 'not found')
            path = os.path.join(STATIC, name)
            if not os.path.isfile(path):
                return self._err(404, 'not found')
            ctype = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                     '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}.get(
                         os.path.splitext(name)[1], 'application/octet-stream')
            with open(path, 'rb') as f:
                data = f.read()
            if name == 'index.html':
                data = data.replace(b'__TITLE__', html.escape(title).encode())
            return self._send(200, data, ctype)

    return Handler


def serve(db_path, port, files_dir, import_from, host='0.0.0.0', title='Health Records'):
    os.makedirs(files_dir, exist_ok=True)
    store = Store(db_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(store, files_dir, set(import_from), title))
    print(f'healthdash on http://{host}:{port}  db={db_path}  files={files_dir}', flush=True)
    try:
        httpd.serve_forever()
    finally:
        store.close()

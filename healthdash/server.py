"""HTTP server and JSON API. Python stdlib only.

When sign-in is on (see auth.py), everything needs a signed-in account, except the sign-in page
itself and the few endpoints the ingest/backup scripts use, which are allowed only from the
addresses given with --import-from. When it is off, anyone who reaches the dashboard can use it.
Either way, requests that change something must come from the dashboard's own page, and the page
must be asked for by an address or a local name (see host_ok), so a website can't reach it
through a look-alike domain (DNS rebinding).
"""

import html
import json
import os
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

from .people import MAIN, People
from .store import KINDS, Store, ValidationError

STATIC = os.path.join(os.path.dirname(__file__), 'static')
SHA_RE = re.compile(r'^[0-9a-f]{64}$')
MAX_BODY = 200 * 1024 * 1024
FILE_TYPES = {'.pdf': 'application/pdf', '.tif': 'image/tiff', '.tiff': 'image/tiff',
              '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
              '.txt': 'text/plain; charset=utf-8', '.wav': 'audio/wav', '.html': 'text/html; charset=utf-8', '.zip': 'application/zip', '.xml': 'application/xml',
              '.doc': 'application/msword',
              '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}


# what the ingest machine's scripts call, without an account
SCRIPT_ROUTES = {('GET', 'summary'), ('GET', 'health'), ('GET', 'files'), ('GET', 'backup'), ('GET', 'uploads'),
                 ('POST', 'import'), ('POST', 'files'), ('POST', 'upload'), ('GET', 'documents'), ('GET', 'people')}
# from this same computer only sending records in is allowed: a browser here also comes from
# 127.0.0.1, and must not read anything without signing in
LOOPBACK_ROUTES = {('GET', 'health'), ('GET', 'files'), ('POST', 'import'), ('POST', 'files')}
LOOPBACK = {'127.0.0.1', '::1', '::ffff:127.0.0.1'}
# Shown with a saved portal page in the dashboard: the portal's own styles can't load (and must
# not), so icons would draw huge and menus would push the record off screen. Not in downloads.
READER_CSS = b"""<style id="healthdash-reader">
html,body{background:#fff!important;color:#1d2421!important}
body{font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif!important;max-width:900px;margin:12px auto!important;padding:0 14px!important}
*{position:static!important;float:none!important;max-width:100%!important;box-sizing:border-box}
svg,img,canvas,video,picture,i[class*="icon"],span[class*="icon"]{width:auto!important;height:auto!important;max-width:28px!important;max-height:28px!important}
nav,[role="navigation"],button,select,input,textarea,.skip-link,a[href="#main"]{display:none!important}
table{border-collapse:collapse;margin:6px 0}td,th{border:1px solid #ddd;padding:3px 7px;text-align:left;vertical-align:top}
h1,h2,h3{line-height:1.25;margin:14px 0 6px}a{color:#256c6c}
</style>
"""


# what a browser-plugin key may do: add files, see how they were filed, check it's connected
KEY_ROUTES = {('POST', 'upload'), ('GET', 'documents'), ('GET', 'auth'), ('GET', 'health'), ('GET', 'people')}
PUBLIC_API = {('GET', 'auth'), ('POST', 'login'), ('POST', 'setup'), ('POST', 'logout')}


def host_ok(host, allow=()):
    """May a request with this Host header be answered? An IP address, a name with no dots (a
    computer on the home network), a local-only name (.local, .lan, .home.arpa, .internal,
    .localhost, .test) or a name given with --allow-host. Anything else could be a website's own
    domain pointed at this computer (DNS rebinding), which must not be able to read the records."""
    import ipaddress
    h = (host or '').strip().lower()
    if not h:
        return True   # HTTP/1.0 clients (and some scripts) send none; browsers always do
    name = h[1:h.index(']')] if h.startswith('[') and ']' in h else h.rsplit(':', 1)[0] if h.count(':') == 1 else h
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    name = name.rstrip('.')
    return ('.' not in name or name.endswith(('.local', '.lan', '.home.arpa', '.internal', '.localhost', '.test', '.home'))
            or name in {a.strip().lower() for a in allow})


def make_handler(store, files_dir, import_from, title='Health Records', uploads=None, auth=None, people=None,
                 allow_hosts=()):
    """The HTTP request handler class for one dashboard: static page, JSON API, file downloads,
    uploads and sign-in. `people` holds each person on the dashboard; requests pick one with X-Person.
    `auth` is None for a dashboard with no accounts at all (the demo); otherwise sign-in is optional
    (auth.required()). `allow_hosts` are extra names the dashboard may be opened by (see host_ok)."""
    people = people or People(store, files_dir, uploads)
    home = store

    class Handler(BaseHTTPRequestHandler):
        """Handles one request. Every API call is checked by _allowed (signed in, plugin key, or the
        ingest host for the script routes) before _dispatch runs it."""
        server_version = 'healthdash'

        def log_message(self, fmt, *args):  # keep request logs (which include search terms) out of the journal
            pass

        # ---- plumbing ------------------------------------------------------------------

        def _send(self, code, body, ctype='application/json; charset=utf-8', extra=None):
            """Send a response: JSON for anything that is not bytes, with no caching of private data."""
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'SAMEORIGIN')     # no other site may frame the dashboard
            self.send_header('Referrer-Policy', 'no-referrer')    # links out never carry the page address
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

        def _token(self):
            c = SimpleCookie()
            try:
                c.load(self.headers.get('Cookie') or '')
            except Exception:  # noqa: BLE001 - a malformed cookie just means not signed in
                return None
            m = c.get('hd_session')
            return m.value if m else None

        def _login_on(self):
            """Is sign-in required right now? (Never when the dashboard runs without accounts.)"""
            if not hasattr(self, '_on'):
                self._on = bool(auth) and auth.required()
            return self._on

        def _key_user(self):
            h = self.headers.get('Authorization') or ''
            if not auth or not h.startswith('Bearer '):
                return None
            if not hasattr(self, '_keyed'):
                self._keyed = auth.key_user(h[7:].strip())
            return self._keyed

        def _user(self):
            """Who is signed in: their user name; 'local' when sign-in is off; None when not signed in."""
            if not self._login_on():
                return 'local'
            if not hasattr(self, '_who'):
                self._who = auth.session_user(self._token())
            return self._who

        def _allowed(self, method, parts):
            """May this request go ahead? Signed in: yes. A plugin key: only its routes. Not signed in:
            the page itself and the sign-in routes, plus the script routes from the ingest host."""
            if not self._login_on() or self._user():
                return True
            if self._key_user():
                return parts[0] == 'api' and len(parts) >= 2 and (method, parts[1]) in KEY_ROUTES and \
                    (parts[1] != 'documents' or len(parts) == 3)
            if method in ('GET', 'HEAD') and (not parts or parts == ['index.html'] or parts[0] == 'static'):
                return True
            if parts[0] == 'api' and len(parts) >= 2 and (method, parts[1]) in PUBLIC_API:
                return True
            if self._importer():
                local = self.client_address[0] in LOOPBACK
                routes = LOOPBACK_ROUTES if local else SCRIPT_ROUTES
                if parts[0] == 'file' and method == 'GET' and not local:
                    return True
                if parts[0] == 'api' and len(parts) >= 2 and (method, parts[1]) in routes:
                    # GET /api/files is only the list of missing files, which holds no record data
                    return not (local and method == 'GET' and parts[1:] != ['files', 'missing'] and parts[1] == 'files')
            return False

        def _same_origin(self):
            origin = self.headers.get('Origin')
            if not origin or origin == 'null':
                return origin is None
            return urlparse(origin).netloc == self.headers.get('Host')

        def _route(self, method):
            """Parse the path, pick the person, check access, and dispatch; errors become JSON replies."""
            u = urlparse(self.path)
            parts = [p for p in u.path.split('/') if p]
            qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
            try:
                if not host_ok(self.headers.get('Host'), allow_hosts):
                    return self._err(421, 'open the dashboard by its address (for example http://192.168.1.20:9380) '
                                          'or start it with --allow-host NAME')
                # a plugin key is sent on purpose, never automatically like a cookie, so the
                # same-page rule (which stops other websites using your sign-in) doesn't apply
                if method not in ('GET', 'HEAD') and not self._same_origin() and not self._key_user():
                    return self._err(403, 'requests must come from the dashboard page')
                if not self._allowed(method, parts):
                    return self._err(401, 'please sign in')
                self.ctx = people.get(self.headers.get('X-Person') or qs.get('p') or MAIN)
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
            """Run one API call (see docs/SYSTEM.md for the routes)."""
            if method == 'GET' and (not parts or parts == ['index.html']):
                return self._static('index.html')
            if method == 'GET' and parts[0] == 'static' and len(parts) == 2:
                return self._static(parts[1])
            if parts[0] == 'file' and len(parts) == 2 and method == 'GET':
                return self._file(parts[1], qs.get('download') == '1')
            if parts == ['report'] and method == 'GET':
                return self._report(qs)
            if parts[0] != 'api':
                return self._err(404, 'not found')
            p = parts[1:]
            r = self._accounts(method, p)
            if r is not False:
                return r
            if method == 'GET':
                if p == ['summary']:
                    return self._send(200, self.ctx.store.summary())
                if p == ['places']:
                    return self._send(200, self.ctx.store.places())
                if p == ['timeline']:
                    return self._send(200, self.ctx.store.timeline())
                if p == ['search']:
                    return self._send(200, self.ctx.store.search(qs.get('q', '')))
                if p == ['labs', 'series']:
                    return self._send(200, self.ctx.store.lab_series())
                if p == ['review']:
                    return self._send(200, self.ctx.store.review_queue())
                if p == ['checks']:
                    from .checks import issues
                    return self._send(200, issues(self.ctx.store))
                if p == ['documents']:
                    return self._send(200, self.ctx.store.documents())
                if len(p) == 2 and p[0] == 'documents':
                    d = self.ctx.store.document(p[1])
                    return self._send(200, d) if d else self._err(404, 'no such document')
                if p == ['backup']:
                    if not self._importer():
                        return self._err(403, 'backups are only allowed from the ingest host')
                    return self._send(200, self.ctx.store.backup_bytes(), 'application/vnd.sqlite3',
                                      {'Content-Disposition': 'attachment; filename="health.db"'})
                if p == ['health']:
                    from .doctor import upload_status
                    return self._send(200, upload_status())
                if p == ['uploads']:
                    if not self._importer():
                        return self._err(403, 'only the ingest host can list uploads')
                    return self._send(200, self.ctx.uploads.uploaded() if self.ctx.uploads else [])
                if p == ['files', 'missing']:
                    have = {f.split('.')[0] for f in os.listdir(self.ctx.files_dir)}
                    return self._send(200, [d['id'] for d in self.ctx.store.documents() if d['id'] not in have])
                if len(p) == 1 and p[0] in KINDS:
                    return self._send(200, self.ctx.store.list(p[0]))
                if len(p) == 2 and p[0] in KINDS:
                    r = self.ctx.store.get(p[0], p[1])
                    return self._send(200, r) if r else self._err(404, 'not found')
            if method == 'POST':
                if len(p) == 3 and p[0] == 'documents' and p[2] == 'retry':
                    if not self.ctx.uploads:
                        return self._err(503, 'uploads are not enabled')
                    if not self.ctx.uploads.retry(p[1]):
                        return self._err(400, 'nothing to retry for this document')
                    return self._send(202, {'id': p[1], 'status': 'processing'})
                if p == ['upload']:
                    # from anyone on the LAN using the dashboard; the file is checked, then read
                    if not self.ctx.uploads:
                        return self._err(503, 'uploads are not enabled')
                    return self._send(202, self.ctx.uploads.add(qs.get('name', ''), self._raw_body(),
                                                       source=qs.get('source', 'dashboard')))
                if p == ['import']:
                    if not self._importer():
                        return self._err(403, 'import is only allowed from the ingest host')
                    return self._send(200, self._import(self._json()))
                if len(p) == 2 and p[0] == 'files':
                    if not self._importer():
                        return self._err(403, 'uploads are only allowed from the ingest host')
                    return self._upload(p[1], self._raw_body())
                if p == ['checks']:
                    from .checks import answer
                    b = self._json() or {}
                    try:
                        return self._send(200, answer(self.ctx.store, str(b.get('key') or ''), str(b.get('action') or '')))
                    except ValueError as e:
                        return self._err(400, str(e))
                if p == ['medications', 'current-list']:
                    b = self._json() or {}
                    return self._send(200, self.ctx.store.apply_med_list(b.get('text'), b.get('as_of'), bool(b.get('apply'))))
                if len(p) == 1 and p[0] in KINDS:
                    return self._send(201, self.ctx.store.create(p[0], self._json()))
            if method == 'PUT':
                if len(p) == 2 and p[0] == 'documents':
                    d = self.ctx.store.update_document(p[1], self._json())
                    return self._send(200, d) if d else self._err(404, 'no such document')
                if len(p) == 2 and p[0] in KINDS:
                    r = self.ctx.store.update(p[0], p[1], self._json())
                    return self._send(200, r) if r else self._err(404, 'not found')
            if method == 'DELETE' and len(p) == 2 and p[0] == 'documents':
                if not self.ctx.uploads or not self.ctx.uploads.delete(p[1]):
                    return self._err(404, 'no such document')
                return self._send(200, {'deleted': True})
            if method == 'DELETE' and len(p) == 2 and p[0] in KINDS:
                return self._send(200, {'deleted': self.ctx.store.delete(p[0], p[1])})
            return self._err(404, 'not found')

        def _accounts(self, method, p):
            """Sign-in and account routes; False when the path isn't one of them."""
            on = self._login_on()
            if p == ['auth'] and method == 'GET':
                return self._send(200, {'auth': on, 'user': (self._user() or self._key_user()) if on else None,
                                        'via_key': bool(on and not self._user() and self._key_user()),
                                        'setup_needed': on and auth.count() == 0,
                                        'can_sign_in': bool(auth), 'accounts': auth.count() if auth else 0})
            if not auth:
                return False
            ip = self.client_address[0]
            if p == ['login-required'] and method == 'POST':
                return self._login_switch(self._json() or {}, ip)
            if p == ['setup'] and method == 'POST':
                body = self._json()
                with home.lock:
                    if auth.count():
                        return self._err(400, 'an account already exists; sign in')
                    name = auth.add_user(body.get('username'), body.get('password'))
                    auth.set_required(True)
                token = auth.login(name, body.get('password'), ip)
                return self._send(200, {'user': name}, extra={'Set-Cookie': auth.cookie(token)})
            if p == ['login'] and method == 'POST':
                body = self._json()
                token = auth.login(body.get('username'), body.get('password'), ip)
                return self._send(200, {'user': auth.session_user(token)}, extra={'Set-Cookie': auth.cookie(token)})
            if p == ['logout'] and method == 'POST':
                auth.logout(self._token())
                return self._send(200, {'signed_out': True}, extra={'Set-Cookie': auth.cookie(None)})
            if p == ['people'] and method == 'GET':
                return self._send(200, people.list())
            if p == ['people'] and method == 'POST' and self._user():
                return self._send(201, people.add((self._json() or {}).get('name')))
            if len(p) == 3 and p[0] == 'documents' and p[2] == 'move' and method == 'POST' and self._user():
                return self._send(200, self._move(p[1], (self._json() or {}).get('to')))
            if len(p) == 2 and p[0] == 'people' and method == 'PUT' and self._user():
                return self._send(200, people.rename(p[1], (self._json() or {}).get('name')))
            if p[:1] == ['keys'] and self._user():
                if p == ['keys'] and method == 'GET':
                    return self._send(200, auth.keys())
                if p == ['keys'] and method == 'POST':
                    from .auth import NO_LOGIN_OWNER
                    return self._send(201, auth.create_key(self._user() if on else NO_LOGIN_OWNER,
                                                           (self._json() or {}).get('label', '')))
                if len(p) == 2 and method == 'DELETE':
                    return self._send(200, {'revoked': auth.revoke_key(p[1])})
            if p[:1] != ['users']:
                return False
            if p == ['users'] and method == 'GET':
                return self._send(200, auth.users())
            if p == ['users'] and method == 'POST':
                body = self._json()
                return self._send(201, {'username': auth.add_user(body.get('username'), body.get('password'))})
            if len(p) == 2 and method == 'DELETE':
                auth.remove_user(p[1])
                return self._send(200, {'removed': p[1]})
            if len(p) == 3 and p[2] == 'password' and method == 'POST':
                body = self._json()
                auth.set_password(p[1], body.get('password'))
                if p[1].lower() == self._user():  # changing your own: stay signed in here
                    token = auth.login(p[1], body.get('password'), ip)
                    return self._send(200, {'changed': True}, extra={'Set-Cookie': auth.cookie(token)})
                return self._send(200, {'changed': True})
            return self._err(404, 'not found')

        def _login_switch(self, body, ip):
            """Turn sign-in on or off from the Accounts dialog.

            On: {on: true, username, password}: signs in with that account, or makes it when no
            account has that name, then requires sign-in; this browser stays signed in.
            Off: {on: false}: only someone signed in can do it; everyone is signed out."""
            if body.get('on'):
                name = (body.get('username') or '').strip().lower()
                with home.lock:
                    if not any(u['username'] == name for u in auth.users()):
                        auth.add_user(name, body.get('password'))
                token = auth.login(name, body.get('password'), ip)   # checks the password of an existing account
                auth.set_required(True)
                return self._send(200, {'auth': True, 'user': name}, extra={'Set-Cookie': auth.cookie(token)})
            if self._login_on() and not self._user():
                return self._err(401, 'please sign in')
            auth.set_required(False)
            return self._send(200, {'auth': False}, extra={'Set-Cookie': auth.cookie(None)})

        def _move(self, did, to):
            """Move a document, and the entries made only from it, to another person."""
            src = people.get(self.headers.get('X-Person') or MAIN)
            dst = people.get(to)
            if dst is src:
                raise ValidationError('that is already who it belongs to')
            doc, records = src.store.take_document(did, dst.id)
            import shutil
            for f in os.listdir(src.files_dir):
                if f.split('.')[0] == did and not f.endswith('.tmp'):
                    shutil.copy2(os.path.join(src.files_dir, f), os.path.join(dst.files_dir, f))
                    os.remove(os.path.join(src.files_dir, f))
            dst.store.give_document(doc, records)
            return {'moved': did, 'to': dst.id, 'entries': {k: len(v) for k, v in records.items()}}

        def _import(self, body):
            """Apply an import from the ingest host: documents, records of each kind and settings (place
            names, over-the-counter words, condition wordings), then the tidy-up passes."""
            result = {}
            st = self.ctx.store
            if body.get('documents'):
                result['documents'] = st.upsert_documents(body['documents'])
            if isinstance(body.get('place_names'), dict):
                st.set_place_names(body['place_names'])
            if isinstance(body.get('condition_names'), dict):
                st.set_condition_names(body['condition_names'])
            if isinstance(body.get('otc'), dict):
                st.set_otc(body['otc'])
            for kind in KINDS:
                if body.get(kind):
                    result[kind] = st.upsert_imported(kind, body[kind])
            if any(body.get(k) for k in KINDS) or 'place_names' in body:
                from .uploads import refresh_doctors
                result['places_renamed'] = st.tidy_places()
                result['procedures_merged'] = st.merge_imported_procedures()
                from .uploads import imaging_from_summaries
                result['imaging_reports'] = imaging_from_summaries(st)
                result['medications_merged'] = st.merge_med_sources()
                result['conditions_merged'] = st.merge_conditions()
                result['doctors'] = refresh_doctors(st)
            return result

        def _upload(self, name, data):
            """Store the bytes of a document already imported (named by its sha256), atomically."""
            sha, _, ext = name.partition('.')
            if not SHA_RE.match(sha) or ('.' + ext.lower()) not in FILE_TYPES:
                raise ValidationError('file name must be <sha256>.<ext>')
            if not self.ctx.store.document(sha):
                raise ValidationError('import the document record first')
            import hashlib
            if hashlib.sha256(data).hexdigest() != sha:
                raise ValidationError('checksum mismatch')
            tmp = os.path.join(self.ctx.files_dir, name + '.tmp')
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, os.path.join(self.ctx.files_dir, sha + '.' + ext.lower()))
            self.ctx.store.set_has_file(sha)
            return self._send(200, {'stored': name})

        def _file(self, sha, download):
            """Send a stored file (inline, or as a download), with a reader style for saved web pages."""
            if not SHA_RE.match(sha):
                return self._err(404, 'not found')
            match = [f for f in os.listdir(self.ctx.files_dir) if f.split('.')[0] == sha and not f.endswith('.tmp')]
            if not match:
                return self._err(404, 'file not on the Pi yet')
            fn = match[0]
            ext = os.path.splitext(fn)[1].lower()
            with open(os.path.join(self.ctx.files_dir, fn), 'rb') as f:
                data = f.read()
            doc = self.ctx.store.document(sha) or {}
            nice = os.path.basename(doc.get('organized_path') or doc.get('path') or fn)
            nice = re.sub(r'[^\w.\- ]', '_', nice)
            disp = 'attachment' if download else 'inline'
            extra = {'Content-Disposition': f'{disp}; filename="{nice}"'}
            if ext == '.html':  # a saved portal page: show it, but never run it or let it load anything
                extra['Content-Security-Policy'] = ("sandbox; default-src 'none'; style-src 'unsafe-inline'; "
                                                    "img-src data:; font-src data:")
                if not download:  # readable in the dashboard; the download stays exactly as saved
                    i = data.lower().find(b'<head>')
                    data = data[:i + 6] + READER_CSS + data[i + 6:] if i >= 0 else READER_CSS + data
            return self._send(200, data, FILE_TYPES.get(ext, 'application/octet-stream'), extra)

        def _report(self, qs):
            """The printable health summary (report.py) for the chosen person. Options in the query:
            from, to (dates), sections and kinds (comma lists), order=oldest, download=1; or view=meds
            (with otc=0 to leave out over-the-counter medicines) for the quick medicines-and-allergies list."""
            from .report import HISTORY_KINDS, SECTIONS, build
            from .store import DATE_RE
            start, end = (qs.get('from') or '').strip(), (qs.get('to') or '').strip()
            for d in (start, end):
                if d and not DATE_RE.match(d):
                    raise ValidationError('dates must look like YYYY-MM-DD (or YYYY-MM, YYYY)')
            pick = lambda key, allowed: [x for x in (qs.get(key) or '').split(',') if x in allowed] or None  # noqa: E731
            import secrets
            nonce = secrets.token_urlsafe(16)   # only the page's own Print script may run
            if qs.get('view') == 'meds':   # the quick list: current medicines and allergies only
                from .report import med_list
                page = med_list(self.ctx.store, self.ctx.name, otc=qs.get('otc') != '0', nonce=nonce)
            else:
                    page = build(self.ctx.store, self.ctx.name, start or None, end or None, pick('sections', SECTIONS),
                             qs.get('order') == 'oldest', pick('kinds', HISTORY_KINDS), nonce=nonce)
            extra = {'Content-Security-Policy': f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'"}
            if qs.get('download') == '1':
                what = 'medicines-and-allergies' if qs.get('view') == 'meds' else 'health-summary'
                fn = re.sub(r'[^A-Za-z0-9-]+', '-', f'{what}-{self.ctx.name}').strip('-')
                extra['Content-Disposition'] = f'attachment; filename="{fn}-{date.today().isoformat()}.html"'
            return self._send(200, page.encode(), 'text/html; charset=utf-8', extra)

        def _static(self, name):
            """Send a file from healthdash/static."""
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


def serve(db_path, port, files_dir, import_from, host='0.0.0.0', title='Health Records', accounts=True,
          main_name=None, allow_hosts=()):
    """Open the database and run the dashboard on host:port until interrupted. With accounts=False
    there is no sign-in at all (the demo); otherwise sign-in is optional, as saved (auth.required())."""
    os.makedirs(files_dir, exist_ok=True)
    store = Store(db_path)
    from .auth import Auth
    from .uploads import Uploads
    uploads = Uploads(store, files_dir)
    auth = Auth(store) if accounts else None
    people = People(store, files_dir, uploads, state_dir=os.path.dirname(os.path.abspath(db_path)),
                    make_uploads=Uploads, main_name=main_name or _first_name(title))
    httpd = ThreadingHTTPServer((host, port), make_handler(store, files_dir, set(import_from), title, uploads, auth,
                                                                people, allow_hosts))
    print(f'healthdash on http://{host}:{port}  db={db_path}  files={files_dir}', flush=True)
    try:
        httpd.serve_forever()
    finally:
        people.close()
        store.close()


def _first_name(title):
    """'Alex's Health' -> 'Alex'; otherwise the whole title."""
    m = re.match(r"^(.+?)['’]s\b", title or '')
    return (m.group(1) if m else (title or 'Main')).strip()[:60]

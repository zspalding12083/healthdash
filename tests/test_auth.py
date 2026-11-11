"""Sign-in, accounts, platform checks. Made-up data only."""

import json
import os
import plistlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from healthdash import auth as authmod
from healthdash import doctor, macos
from healthdash.auth import Auth
from healthdash.server import make_handler
from healthdash.store import Store, ValidationError


class PasswordTests(unittest.TestCase):
    def test_hash_roundtrip_and_salted(self):
        h = authmod.hash_password('correct horse', iterations=1000)
        self.assertTrue(authmod.check_password('correct horse', h))
        self.assertFalse(authmod.check_password('wrong horse', h))
        self.assertNotEqual(h, authmod.hash_password('correct horse', iterations=1000))
        self.assertFalse(authmod.check_password('x', 'garbage'))


class AuthServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.files = os.path.join(self.tmp.name, 'files')
        os.makedirs(self.files)
        self.store = Store(os.path.join(self.tmp.name, 't.db'))
        self.auth = Auth(self.store)
        self.importers = set()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0),
                                         make_handler(self.store, self.files, self.importers, 'T', None, self.auth))
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.p = mock.patch.object(authmod, 'ITERATIONS', 1000)  # fast hashing in tests
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.tmp.cleanup()

    def call(self, path, body=None, method=None, cookie=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {'Content-Type': 'application/json', **(headers or {})}
        if cookie:
            h['Cookie'] = cookie
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b'{}'), r.headers.get('Set-Cookie')
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b'{}'), None

    def signin(self, user='ann', pw='password1'):
        code, _, cookie = self.call('/api/login', {'username': user, 'password': pw})
        self.assertEqual(code, 200)
        return cookie.split(';')[0]

    def test_first_account_then_setup_is_closed(self):
        self.assertTrue(self.call('/api/auth')[1]['setup_needed'])
        self.assertEqual(self.call('/api/summary')[0], 401)
        code, body, cookie = self.call('/api/setup', {'username': 'Ann', 'password': 'password1'})
        self.assertEqual((code, body['user']), (200, 'ann'))
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertEqual(self.call('/api/setup', {'username': 'eve', 'password': 'password1'})[0], 400)
        c = cookie.split(';')[0]
        self.assertEqual(self.call('/api/summary', cookie=c)[0], 200)
        self.assertEqual(self.call('/api/auth', cookie=c)[1]['user'], 'ann')

    def test_everything_needs_sign_in_but_the_page(self):
        self.auth.add_user('ann', 'password1')
        for path in ('/api/summary', '/api/encounters', '/api/documents', '/api/search?q=x', '/file/' + 'a' * 64,
                     '/api/users', '/api/review'):
            self.assertEqual(self.call(path)[0], 401, path)
        with urllib.request.urlopen(self.url + '/') as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(self.call('/api/conditions', {'name': 'x'})[0], 401)
        self.assertEqual(self.call('/api/summary', cookie='hd_session=forged')[0], 401)

    def test_wrong_password_and_lockout(self):
        self.auth.add_user('ann', 'password1')
        for _ in range(authmod.MAX_FAILS):
            self.assertEqual(self.call('/api/login', {'username': 'ann', 'password': 'nope'})[0], 400)
        code, body, _ = self.call('/api/login', {'username': 'ann', 'password': 'password1'})
        self.assertEqual(code, 400)
        self.assertIn('too many', body['error'])

    def test_accounts_all_equal_and_last_one_kept(self):
        self.auth.add_user('ann', 'password1')
        c = self.signin()
        self.assertEqual(self.call('/api/users', {'username': 'bob', 'password': 'password2'}, cookie=c)[0], 201)
        bob = self.signin('bob', 'password2')
        # bob can do what ann can, including resetting her password
        self.assertEqual(self.call('/api/users/ann/password', {'password': 'newpass99'}, cookie=bob)[0], 200)
        self.assertEqual(self.call('/api/summary', cookie=c)[0], 401)  # ann's old session ended
        self.assertEqual(self.call('/api/users/ann', method='DELETE', cookie=bob)[0], 200)
        code, body, _ = self.call('/api/users/bob', method='DELETE', cookie=bob)
        self.assertEqual(code, 400)
        self.assertIn('last account', body['error'])
        self.assertEqual(self.call('/api/users', {'username': 'x', 'password': 'short'}, cookie=bob)[0], 400)

    def test_sign_out(self):
        self.auth.add_user('ann', 'password1')
        c = self.signin()
        self.assertEqual(self.call('/api/logout', {}, cookie=c)[0], 200)
        self.assertEqual(self.call('/api/summary', cookie=c)[0], 401)

    def test_other_websites_cannot_post(self):
        self.auth.add_user('ann', 'password1')
        c = self.signin()
        code = self.call('/api/conditions', {'name': 'x'}, cookie=c, headers={'Origin': 'http://evil.example'})[0]
        self.assertEqual(code, 403)
        host = self.url.split('//')[1]
        self.assertEqual(self.call('/api/conditions', {'name': 'x'}, cookie=c, headers={'Origin': 'http://' + host})[0], 201)

    def test_same_computer_can_send_but_not_read(self):
        self.auth.add_user('ann', 'password1')
        self.importers.add('127.0.0.1')  # a local ingest, and any browser on this computer
        self.assertEqual(self.call('/api/files/missing')[0], 200)
        self.assertEqual(self.call('/api/import', {'conditions': [{'id': 'c1', 'name': 'x'}]})[0], 200)
        for path in ('/api/summary', '/api/backup', '/api/uploads', '/file/' + 'a' * 64, '/api/encounters'):
            self.assertEqual(self.call(path)[0], 401, path)

    def test_ingest_machine_reaches_only_its_endpoints(self):
        self.auth.add_user('ann', 'password1')
        from healthdash import server
        with mock.patch.object(server, 'LOOPBACK', set()):  # pretend 127.0.0.1 is the separate ingest machine
            self._remote_importer_checks()

    def _remote_importer_checks(self):
        self.importers.add('127.0.0.1')
        self.assertEqual(self.call('/api/summary')[0], 200)
        self.assertEqual(self.call('/api/files/missing')[0], 200)
        self.assertEqual(self.call('/api/import', {'conditions': [{'id': 'c1', 'name': 'x'}]})[0], 200)
        self.assertEqual(self.call('/api/encounters')[0], 401)
        self.assertEqual(self.call('/api/users')[0], 401)
        self.assertEqual(self.call('/api/conditions', {'name': 'x'})[0], 401)

    def test_store_level_rules(self):
        with self.assertRaises(ValidationError):
            self.auth.add_user('Bad Name!', 'password1')
        self.auth.add_user('ann', 'password1')
        with self.assertRaises(ValidationError):
            self.auth.add_user('ANN', 'password1')
        self.assertIsNone(self.auth.session_user('nope'))


class PlatformTests(unittest.TestCase):
    def test_install_commands_per_system(self):
        self.assertEqual(doctor.install_command(['pdftotext', 'pdfinfo', 'tesseract'], 'apt'),
                         'sudo apt-get install -y poppler-utils tesseract-ocr')
        self.assertEqual(doctor.install_command(['pdftotext', 'tesseract', 'eng'], 'brew'),
                         'brew install poppler tesseract')
        self.assertEqual(doctor.install_command(['soffice'], 'brew'), 'brew install --cask libreoffice')
        self.assertIn('tesseract-langpack-eng', doctor.install_command(['eng'], 'dnf'))
        self.assertIsNone(doctor.install_command([], 'apt'))

    def test_missing_tools_reported(self):
        with mock.patch('shutil.which', return_value=None), mock.patch('os.path.exists', return_value=False):
            st = doctor.upload_status()
            self.assertFalse(st['can_read'])
            self.assertEqual(st['missing'], ['pdftotext', 'pdfinfo', 'pdftoppm', 'tesseract'])
            ok = {n: v for n, v, _, _ in doctor.checks()}
            self.assertFalse(ok['tesseract (reading scans)'])

    def test_mac_agent_finds_homebrew_tools_and_stays_local(self):
        job = macos.agent(port=9400, python='/usr/bin/python3', project='/Users/x/healthdash',
                          data='/Users/x/Library/Application Support/healthdash', log='/tmp/h.log')
        self.assertIn('/opt/homebrew/bin', job['EnvironmentVariables']['PATH'])
        args = job['ProgramArguments']
        self.assertEqual(args[args.index('--host') + 1], '127.0.0.1')
        self.assertEqual(args[args.index('--port') + 1], '9400')
        self.assertEqual(macos.agent(lan=True, project='/p')['ProgramArguments'][
            macos.agent(lan=True, project='/p')['ProgramArguments'].index('--host') + 1], '0.0.0.0')
        plistlib.loads(plistlib.dumps(job))  # valid plist

    def test_service_refuses_off_mac(self):
        with mock.patch('sys.platform', 'linux'):
            with self.assertRaises(SystemExit):
                macos.install()


if __name__ == '__main__':
    unittest.main()

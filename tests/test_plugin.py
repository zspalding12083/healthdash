"""Browser-plugin keys and saved portal pages. Made-up data only."""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from healthdash import auth as authmod
from healthdash.auth import Auth
from healthdash.extract import html_text
from healthdash.server import make_handler
from healthdash.store import Store
from healthdash.uploads import Uploads

PAGE = b"""<!doctype html><html><head><title>Test Results</title><script>alert(1)</script>
<style>.x{color:red}</style></head><body onload="steal()"><nav>Home Messages Billing</nav>
<h1>Lab Results</h1><p>Riverside General Hospital Laboratory</p>
<table><tr><th>Test</th><th>Result</th></tr><tr><td>Specimen collected</td><td>03/04/2020</td></tr></table>
<pre>Final Result  Collected 04-Mar-2020 07:46
GLUCOSE          130 H    74-106     mg/dL
SODIUM           139      136-145    mmol/L
POTASSIUM        4.1      3.5-5.1    mmol/L
CREATININE       0.9      0.6-1.2    mg/dL</pre>
<p>""" + b"Your results are shown above. " * 12 + b"""</p></body></html>"""


MYCHART = b"""<html><body><img alt="Sample Family Health Center"><h1>GLUCOSE</h1>
<p>Collected on Mar 04, 2020 5:23 PM</p><div>GLUCOSE</div><div>View trends</div>
<div>Normal range: 70 - 200 mg/dL</div><div>Your value is 368 mg/dLThis value is AbnormalNormal range: 70 - 200 mg/dL</div>
<div>POTASSIUM</div><div>Normal range: 3.5 - 5.1 mmol/L</div><div>Your value is 3.2 mmol/LThis value is Low</div>
<div>NAME OF MACHINE</div><div>Value</div><div>1</div><div>Your value is 1</div>
<div>GLU HEMOCUE SERIAL#</div><div>Value</div><div>1,747,119,085</div><div>Your value is 1,747,119,085</div>
<div>Resulting lab:</div><div>IN HOUSE</div></body></html>"""


class MyChartTests(unittest.TestCase):
    def test_values_ranges_flags_and_no_machine_details(self):
        from healthdash import parsers
        rows = {r['analyte']: r for r in parsers.mychart_labs(html_text(MYCHART))}
        self.assertEqual(set(rows), {'Glucose', 'Potassium'})
        self.assertEqual((rows['Glucose']['num'], rows['Glucose']['unit'], rows['Glucose']['ref_range'],
                          rows['Glucose']['flag'], rows['Glucose']['date']), (368, 'mg/dL', '70-200', 'H', '2020-03-04'))
        self.assertEqual(rows['Potassium']['flag'], 'L')

    def test_page_is_titled_by_its_test_and_keeps_the_logo_name(self):
        from healthdash import classify
        t = html_text(MYCHART)
        self.assertIn('Sample Family Health Center', t)
        r = classify.classify(t, 'MyChart - GLUCOSE - Test Details.html', ['Sample Family Health Center'])
        self.assertEqual((r['category'], r['title'], r['facility']),
                         ('labs', 'Lab results: Glucose, Potassium', 'Sample Family Health Center'))


class HtmlTextTests(unittest.TestCase):
    def test_scripts_and_styles_dropped_lines_kept(self):
        t = html_text(PAGE)
        self.assertNotIn('alert', t)
        self.assertNotIn('color:red', t)
        self.assertIn('Lab Results', t)
        self.assertIn('GLUCOSE          130 H    74-106', t)   # columns in <pre> kept
        self.assertIn('Specimen collected  03/04/2020', t)    # table cells two spaces apart
        self.assertIn('Home Messages Billing', t)


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.object(authmod, 'ITERATIONS', 1000)
        self.p.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.files = os.path.join(self.tmp.name, 'files')
        os.makedirs(self.files)
        self.store = Store(os.path.join(self.tmp.name, 't.db'))
        self.auth = Auth(self.store)
        self.auth.add_user('ann', 'password1')
        self.key = self.auth.create_key('ann', 'Firefox')['key']
        self.up = Uploads(self.store, self.files, recheck=0)   # real extractor: .html needs no tools
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0),
                                         make_handler(self.store, self.files, set(), 'T', self.up, self.auth))
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.tmp.cleanup()
        self.p.stop()

    def call(self, path, data=None, method=None, key=None, headers=None):
        h = dict(headers or {})
        if key:
            h['Authorization'] = 'Bearer ' + key
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b'{}'), dict(r.headers)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b'{}'), dict(e.headers)

    def wait(self, sha):
        for _ in range(100):
            d = self.store.document(sha)
            if d is None or d['status'] != 'processing':
                return d
            time.sleep(0.05)

    def test_key_can_add_a_page_from_another_origin_and_check_it(self):
        code, body, _ = self.call('/api/upload?source=plugin&name=MyChart%20-%20Test%20Results.html', PAGE,
                                  'POST', self.key, {'Origin': 'moz-extension://abc'})
        self.assertEqual(code, 202)
        d = self.wait(body['id'])
        self.assertEqual((d['category'], d['ext']), ('labs', '.html'))
        self.assertEqual(len([x for x in d['linked'] if x['kind'] == 'labs']), 4)
        self.assertEqual(self.call(f"/api/documents/{body['id']}", key=self.key)[0], 200)
        self.assertEqual(self.call('/api/auth', key=self.key)[1]['user'], 'ann')

    def test_key_cannot_read_or_change_records(self):
        for path in ('/api/summary', '/api/encounters', '/api/documents', '/api/search?q=x', '/api/keys',
                     '/api/users', '/api/backup', '/file/' + 'a' * 64):
            self.assertEqual(self.call(path, key=self.key)[0], 401, path)
        self.assertEqual(self.call('/api/conditions', b'{"name":"x"}', 'POST', self.key)[0], 401)
        self.assertEqual(self.call('/api/upload?name=x.pdf', b'%PDF', 'POST', 'hdk_wrong')[0], 401)

    def test_revoked_key_stops_working(self):
        kid = self.auth.keys()[0]['id']
        self.assertTrue(self.auth.revoke_key(kid))
        self.assertEqual(self.call('/api/auth', key=self.key)[1]['user'], None)
        self.assertEqual(self.call('/api/upload?source=plugin&name=a.html', PAGE, 'POST', self.key)[0], 401)

    def test_pages_only_from_the_plugin_and_shown_sandboxed(self):
        code = self.call('/api/upload?name=a.html', PAGE, 'POST', self.key)[0]
        self.assertEqual(code, 400)   # the dashboard's own upload button takes PDFs and pictures only
        code, body, _ = self.call('/api/upload?source=plugin&name=a.html', PAGE, 'POST', self.key)
        self.wait(body['id'])
        token = self.auth.login('ann', 'password1')
        req = urllib.request.Request(f"{self.url}/file/{body['id']}", headers={'Cookie': f'hd_session={token}'})
        with urllib.request.urlopen(req) as r:
            csp = r.headers['Content-Security-Policy']
        self.assertIn('sandbox', csp)
        self.assertIn("default-src 'none'", csp)
        with urllib.request.urlopen(req) as r:
            self.assertIn(b'healthdash-reader', r.read())       # shown readable in the dashboard
        req = urllib.request.Request(f"{self.url}/file/{body['id']}?download=1", headers={'Cookie': f'hd_session={token}'})
        with urllib.request.urlopen(req) as r:
            self.assertNotIn(b'healthdash-reader', r.read())    # downloaded exactly as saved

    def test_empty_or_repeated_pages_are_dropped(self):
        code, body, _ = self.call('/api/upload?source=plugin&name=menu.html', b'<html><body>Home Messages</body></html>',
                                  'POST', self.key)
        self.assertIsNone(self.wait(body['id']))   # too little text: not kept
        first = self.call('/api/upload?source=plugin&name=a.html', PAGE, 'POST', self.key)[1]
        self.wait(first['id'])
        again = PAGE.replace(b'<title>Test Results</title>', b'<title>Test Results (refreshed)</title>')
        second = self.call('/api/upload?source=plugin&name=b.html', again, 'POST', self.key)[1]
        self.assertIsNone(self.wait(second['id']))   # same page again: dropped instead of asking
        self.assertIsNotNone(self.store.document(first['id']))

    def test_removing_an_account_removes_its_keys(self):
        self.auth.add_user('bob', 'password2')
        self.auth.remove_user('ann')
        self.assertEqual(self.auth.keys(), [])


if __name__ == '__main__':
    unittest.main()

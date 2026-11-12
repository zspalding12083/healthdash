"""Several people on one dashboard. Made-up data only."""

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
from healthdash.people import People
from healthdash.server import make_handler
from healthdash.store import Store
from healthdash.uploads import Uploads


def fake_extract(path):
    return {'pages': [open(path, 'rb').read().decode('latin-1') * 3], 'ocr_pages': []}


class PeopleTests(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.object(authmod, 'ITERATIONS', 1000)
        self.p.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.files = os.path.join(self.state, 'files'); os.makedirs(self.files)
        self.start()

    def start(self):
        self.store = Store(os.path.join(self.state, 'health.db'))
        self.auth = Auth(self.store)
        if not self.auth.count():
            self.auth.add_user('ann', 'password1')
        mk = lambda st, f: Uploads(st, f, extractor=fake_extract, recheck=0)
        self.people = People(self.store, self.files, mk(self.store, self.files), state_dir=self.state,
                             make_uploads=mk, main_name='Jane')
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(
            self.store, self.files, {'127.0.0.1'}, 'Family', self.people.get('main').uploads, self.auth, self.people))
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.cookie = 'hd_session=' + self.auth.login('ann', 'password1')

    def stop(self):
        self.httpd.shutdown(); self.httpd.server_close()
        self.people.close(); self.store.close()

    def tearDown(self):
        self.stop()
        self.tmp.cleanup()
        self.p.stop()

    def call(self, path, body=None, method=None, person=None, raw=False, headers=None):
        h = {'Cookie': self.cookie, **(headers or {})}
        if person:
            h['X-Person'] = person
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                out = r.read()
                return r.status, (out if raw else json.loads(out or b'{}'))
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b'{}')

    def wait(self, person):
        for _ in range(100):
            if not any(d.get('status') == 'processing' for d in self.people.get(person).store.documents()):
                return
            time.sleep(0.05)

    def test_records_stay_with_their_person(self):
        self.assertEqual(self.call('/api/people')[1], [{'id': 'main', 'name': 'Jane'}])
        code, p = self.call('/api/people', {'name': 'Sam Sample'}, 'POST')
        self.assertEqual((code, p['id']), (201, 'sam-sample'))
        self.assertEqual(self.call('/api/people', {'name': 'sam sample'}, 'POST')[0], 400)   # already there
        # an entry and an upload for Sam
        self.assertEqual(self.call('/api/conditions', {'name': 'Asthma'}, 'POST', person='sam-sample')[0], 201)
        code, up = self.call('/api/upload?name=note.pdf', b'%PDF-1 Visit note for an ankle sprain', 'POST', person='sam-sample')
        self.assertEqual(code, 202)
        self.wait('sam-sample')
        # Jane sees none of it
        self.assertEqual(self.call('/api/conditions')[1], [])
        self.assertEqual(self.call('/api/documents')[1], [])
        self.assertEqual(self.call('/api/search?q=ankle')[1]['documents'], [])
        self.assertEqual(self.call(f"/file/{up['id']}", raw=True)[0], 404)
        # Sam does
        self.assertEqual([c['name'] for c in self.call('/api/conditions', person='sam-sample')[1]], ['Asthma'])
        self.assertTrue(self.call('/api/search?q=ankle', person='sam-sample')[1]['documents'])
        self.assertEqual(self.call(f"/file/{up['id']}?p=sam-sample", raw=True)[0], 200)
        self.assertTrue(os.path.exists(os.path.join(self.state, 'people', 'sam-sample', 'health.db')))

    def test_move_a_document_and_it_stays_moved(self):
        self.call('/api/people', {'name': 'Sam'}, 'POST')
        doc = {'id': 'a' * 64, 'path': 'Folder/sleep study.pdf', 'title': 'Sleep study', 'text': 'Patient Name: Sam',
               'needs_review': False}
        self.store.upsert_documents([doc])
        with open(os.path.join(self.files, 'a' * 64 + '.pdf'), 'wb') as f:
            f.write(b'%PDF-1 sleep study')
        self.store.set_has_file('a' * 64)
        self.store.upsert_imported('tests', [{'id': 'test-sleep', 'name': 'Sleep study', 'date': '2020-01-01', 'docs': ['a' * 64]}])
        self.store.upsert_imported('encounters', [{'id': 'enc-both', 'date': '2020-01-01', 'docs': ['a' * 64, 'b' * 64]}])
        code, r = self.call(f"/api/documents/{'a' * 64}/move", {'to': 'sam'}, 'POST')
        self.assertEqual((code, r['entries']), (200, {'tests': 1}))
        sam = self.people.get('sam').store
        self.assertEqual(sam.document('a' * 64)['title'], 'Sleep study')
        self.assertEqual([t['id'] for t in sam.list('tests')], ['test-sleep'])
        self.assertIsNone(self.store.document('a' * 64))
        self.assertEqual(self.store.list('tests'), [])
        self.assertEqual(self.store.get('encounters', 'enc-both')['docs'], ['b' * 64])     # stays, without it
        self.assertTrue(os.path.exists(os.path.join(self.state, 'people', 'sam', 'files', 'a' * 64 + '.pdf')))
        self.assertFalse(os.path.exists(os.path.join(self.files, 'a' * 64 + '.pdf')))
        # the next import from the records folder doesn't bring it back
        self.store.upsert_documents([doc])
        self.store.upsert_imported('tests', [{'id': 'test-sleep', 'name': 'Sleep study', 'docs': ['a' * 64]}])
        self.assertIsNone(self.store.document('a' * 64))
        self.assertEqual(self.store.list('tests'), [])
        # and it can be moved back
        self.assertEqual(self.call(f"/api/documents/{'a' * 64}/move", {'to': 'main'}, 'POST', person='sam')[0], 200)
        self.assertIsNotNone(self.store.document('a' * 64))

    def test_unknown_person_refused(self):
        self.assertEqual(self.call('/api/summary', person='nobody')[0], 400)

    def test_plugin_key_and_ingest_machine_pick_a_person(self):
        self.call('/api/people', {'name': 'Sam'}, 'POST')
        key = self.auth.create_key('ann', 'Firefox')['key']
        code, up = self.call('/api/upload?source=plugin&p=sam&name=page.html',
                             b'<html><body>' + b'Clinic visit notes for a sore throat. ' * 20 + b'</body></html>',
                             'POST', headers={'Authorization': 'Bearer ' + key, 'Cookie': ''})
        self.assertEqual(code, 202)
        self.wait('sam')
        self.assertTrue(self.people.get('sam').store.document(up['id']))
        self.assertIsNone(self.store.document(up['id']))
        self.assertEqual([p['id'] for p in self.call('/api/people', headers={'Authorization': 'Bearer ' + key, 'Cookie': ''})[1]],
                         ['main', 'sam'])

    def test_people_and_main_records_survive_a_restart(self):
        self.call('/api/conditions', {'name': 'Migraine'}, 'POST')
        self.call('/api/people', {'name': 'Sam'}, 'POST')
        self.call('/api/conditions', {'name': 'Asthma'}, 'POST', person='sam')
        self.stop(); self.start()
        self.assertEqual([p['name'] for p in self.call('/api/people')[1]], ['Jane', 'Sam'])
        self.assertEqual([c['name'] for c in self.call('/api/conditions')[1]], ['Migraine'])
        self.assertEqual([c['name'] for c in self.call('/api/conditions', person='sam')[1]], ['Asthma'])


if __name__ == '__main__':
    unittest.main()

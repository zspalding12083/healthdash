"""Tests use made-up sample data only. Never put real records in this folder."""

import hashlib
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from healthdash import catalog, demo, organize
from healthdash.server import make_handler
from healthdash.store import Store, ValidationError

SAMPLE = {
    'encounters': [{'id': 'enc-1', 'date': '2020-03-02', 'end_date': '2020-03-05', 'type': 'Inpatient',
                    'facility': 'Sample General', 'reason': 'Ankle injury', 'docs': ['DOC1']}],
    'tests': [{'id': 'test-1', 'date': '2020-03-02', 'name': 'Ankle X-ray', 'category': 'imaging',
               'result': 'Hairline fracture', 'abnormal': True, 'encounter_id': 'enc-1'}],
    'labs': [{'id': f'lab-{i}', 'date': f'2020-03-0{i}', 'analyte': 'WBC', 'value': str(v), 'num': v,
              'unit': 'K/uL', 'ref_range': '4.5-11.0', 'flag': 'H' if v > 11 else None}
             for i, v in ((2, 12.1), (3, 9.8), (4, 8.0))],
    'medications': [
        {'id': 'med-1', 'name': 'Examplol', 'dose': '10 mg', 'start_date': '2019-01', 'status': 'active'},
        {'id': 'med-2', 'name': 'Fakeazole', 'dose': '20 mg', 'start_date': '2018-05-01',
         'end_date': '2020-03-05', 'status': 'stopped', 'notes': 'Switched'}],
    'financial': [{'id': 'fin-1', 'date': '2020-04-01', 'kind': 'Statement', 'payee': 'Sample Ortho',
                   'charged': '$1,000.50', 'paid': 900, 'owed': 100.5}],
}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(os.path.join(self.tmp.name, 't.db'))

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def load(self):
        self.s.upsert_documents([{'id': 'DOC1', 'path': 'a.pdf', 'title': 'Discharge summary',
                                  'doc_date': '2020-03-05', 'text': 'Patient had a hairline fracture.',
                                  'needs_review': False}])
        for k, v in SAMPLE.items():
            self.s.upsert_imported(k, json.loads(json.dumps(v)))

    def test_import_and_timeline(self):
        self.load()
        tl = self.s.timeline()
        kinds = [e['kind'] for e in tl]
        self.assertIn('encounters', kinds)
        self.assertNotIn('financial', kinds)
        stops = [e for e in tl if e.get('change') == 'stop']
        self.assertEqual(stops[0]['title'], 'Stopped Fakeazole 20 mg')
        self.assertIn('financial', [e['kind'] for e in self.s.timeline(include_financial=True)])

    def test_money_parsing_and_summary(self):
        self.load()
        self.assertEqual(self.s.get('financial', 'fin-1')['charged'], 1000.5)
        summ = self.s.summary()
        self.assertEqual([m['name'] for m in summ['current_meds']], ['Examplol'])
        self.assertEqual(summ['financial']['owed'], 100.5)

    def test_search_documents_and_records(self):
        self.load()
        r = self.s.search('fracture')
        self.assertEqual(r['documents'][0]['id'], 'DOC1')
        self.assertIn('[[', r['documents'][0]['snippet'])
        self.assertTrue(any(x['id'] == 'test-1' for x in r['records']))
        self.assertEqual(self.s.search('  '), {'documents': [], 'records': []})
        self.s.search('"; DROP TABLE documents; --')  # must not raise
        self.assertEqual(self.s.search('hairl')['documents'][0]['id'], 'DOC1')  # prefix match

    def test_edits_survive_reimport(self):
        self.load()
        self.s.update('encounters', 'enc-1', {'reason': 'Ankle injury, fixed by hand'})
        res = self.s.upsert_imported('encounters', json.loads(json.dumps(SAMPLE['encounters'])))
        self.assertEqual(res['skipped_edited'], 1)
        self.assertEqual(self.s.get('encounters', 'enc-1')['reason'], 'Ankle injury, fixed by hand')

    def test_document_edit_survives_reimport_and_fts_updates(self):
        self.load()
        self.s.update_document('DOC1', {'title': 'Hand title', 'needs_review': 0})
        self.s.upsert_documents([{'id': 'DOC1', 'path': 'a.pdf', 'title': 'Auto', 'text': 'new words zebra'}])
        d = self.s.document('DOC1')
        self.assertEqual(d['title'], 'Hand title')
        self.assertEqual(self.s.search('zebra')['documents'][0]['id'], 'DOC1')
        self.assertEqual(self.s.search('fracture')['documents'], [])
        self.assertEqual(d['linked'][0]['id'], 'enc-1')

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.s.create('encounters', {'date': '03/02/2020'})
        with self.assertRaises(ValidationError):
            self.s.create('nope', {})
        with self.assertRaises(ValidationError):
            self.s.create('financial', {'charged': 'lots'})
        r = self.s.create('conditions', {'name': 'Migraine', 'status': 'active', 'first_noted': '2015'})
        self.assertEqual(r['origin'], 'manual')

    def test_sample_data_loads(self):
        demo.load_sample(self.s)
        summ = self.s.summary()
        self.assertEqual(summ['counts']['documents'], 7)
        self.assertEqual([m['name'] for m in summ['current_meds']], ['Examplol'])
        self.assertTrue(self.s.search('appendicitis')['documents'])

    def test_lab_series(self):
        self.load()
        self.assertEqual([p['num'] for p in self.s.lab_series()['WBC']], [12.1, 9.8, 8.0])


class CatalogTests(unittest.TestCase):
    def test_guess_date(self):
        cases = {
            '2021_04_09_attending_report.pdf': '2021-04-09',
            'CT Head 02-14-2022.pdf': '2022-02-14',
            '06_18_2019_CT_report.pdf': '2019-06-18',
            '7_23_2019_Labs.pdf': '2019-07-23',
            '11152013_sample_ultrasound.pdf': '2013-11-15',
            '20140302_Op_Report.pdf': '2014-03-02',
            '2023_05_Exam_Report.pdf': '2023-05',
            '2018_eye_exam.pdf': '2018',
            'clinic_report.pdf': None,
        }
        for name, want in cases.items():
            self.assertEqual(catalog.guess_date(name), want, name)

    def test_guess_title_drops_name_words(self):
        orig = catalog.config.NAME_WORDS
        catalog.config.NAME_WORDS = ['jdoe', 'jane']
        try:
            self.assertEqual(catalog.guess_title('2020_01_02_jdoe_MRI_report_Jane.pdf'), 'Mri report')
        finally:
            catalog.config.NAME_WORDS = orig

    def test_describe_unknown_needs_review(self):
        d = catalog.describe({'files': {}, 'discs': {}}, 'new/2026_01_02_mystery_letter.pdf')
        self.assertTrue(d['needs_review'])
        self.assertEqual(d['date'], '2026-01-02')


class OrganizeTests(unittest.TestCase):
    def test_names_and_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, 'src'), os.path.join(tmp, 'dst')
            os.makedirs(os.path.join(src, 'sub'))
            for n in ('a.pdf', 'b.pdf', 'sub/c.pdf', '.DS_Store'):
                with open(os.path.join(src, n), 'w') as f:
                    f.write(n)
            cat = {'files': {
                'a.pdf': {'title': 'Echo / heart', 'date': '2020-01-02', 'category': 'tests', 'facility': 'Sample Hosp'},
                'b.pdf': {'title': 'Echo / heart', 'date': '2020-01-02', 'category': 'tests', 'facility': 'Sample Hosp'},
                'sub/c.pdf': {'title': 'Bill', 'date': '2021-05-06', 'category': 'financial'}}, 'discs': {}}
            rows = organize.plan(src, cat)
            names = sorted(r[1] for r in rows)
            self.assertEqual(names, ['2020-01-02_Sample-Hosp_Echo-heart.pdf',
                                     '2020-01-02_Sample-Hosp_Echo-heart_2.pdf', '2021-05-06_Bill.pdf'])
            orig = organize.catalog.load
            organize.catalog.load = lambda: cat
            try:
                organize.organize(src, dst, apply=True, log=lambda *_: None)
                organize.organize(src, dst, apply=True, log=lambda *_: None)  # idempotent
            finally:
                organize.catalog.load = orig
            linked = os.path.join(dst, '10 Financial', '2021-05-06_Bill.pdf')
            self.assertTrue(os.path.samefile(linked, os.path.join(src, 'sub/c.pdf')))
            self.assertTrue(os.path.exists(os.path.join(src, 'a.pdf')))  # originals untouched
            self.assertTrue(os.path.exists(os.path.join(dst, '_rename_map.csv')))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.files = os.path.join(self.tmp.name, 'files')
        os.makedirs(self.files)
        self.store = Store(os.path.join(self.tmp.name, 't.db'))
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.store, self.files, {'127.0.0.1'}, 'Test & Co'))
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.tmp.cleanup()

    def req(self, path, body=None, method=None, raw=False):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(self.url + path, data=data, method=method)
        with urllib.request.urlopen(r) as resp:
            out = resp.read()
            return out if raw else json.loads(out)

    def test_import_upload_and_read(self):
        pdf = b'%PDF-1.4 fake sample'
        sha = hashlib.sha256(pdf).hexdigest()
        self.req('/api/import', {'documents': [{'id': sha, 'path': 'x.pdf', 'title': 'Sample', 'ext': '.pdf',
                                                'text': 'hello world'}], **SAMPLE})
        self.assertEqual(self.req('/api/files/missing'), [sha])
        self.req(f'/api/files/{sha}.pdf', pdf)
        self.assertEqual(self.req('/api/files/missing'), [])
        self.assertEqual(self.req(f'/file/{sha}', raw=True), pdf)
        self.assertEqual(self.req('/api/summary')['counts']['labs'], 3)
        self.assertTrue(self.req('/api/search?q=hello')['documents'])
        self.assertIn(b'<title>Test &amp; Co</title>', self.req('/', raw=True))
        self.assertTrue(self.req('/api/backup', raw=True).startswith(b'SQLite format 3'))

    def test_bad_upload_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.req('/api/files/' + 'a' * 64 + '.pdf', b'x')
        self.assertEqual(cm.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError):
            self.req('/file/../../etc/passwd', raw=True)

    def test_create_edit_delete(self):
        r = self.req('/api/conditions', {'name': 'Migraine', 'status': 'active'})
        r = self.req(f"/api/conditions/{r['id']}", {'status': 'resolved'}, method='PUT')
        self.assertEqual(r['status'], 'resolved')
        self.assertTrue(self.req(f"/api/conditions/{r['id']}", method='DELETE')['deleted'])


if __name__ == '__main__':
    unittest.main()

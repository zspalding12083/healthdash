"""Life events, medical codes, zips of loose records and the printable summary. Made-up data only."""

import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer

from healthdash import ccda, codes, fhir
from healthdash.report import build
from healthdash.server import make_handler
from healthdash.store import Store, ValidationError
from healthdash.uploads import Uploads, is_export

from tests.test_ccda import DOC

# the sample summary with codes on its problem (SNOMED CT, with an ICD-10-CM translation) and a procedure (CPT)
CODED_DOC = DOC.replace(
    b'<value xsi:type="CD"><originalText><reference value="#p1"/></originalText></value>',
    b'<value xsi:type="CD" code="714628002" codeSystem="2.16.840.1.113883.6.96"><originalText><reference value="#p1"/></originalText>'
    b'<translation code="R73.03" codeSystem="2.16.840.1.113883.6.90"/></value>'
).replace(
    b' </structuredBody>',
    b'  <component><section><code code="47519-4"/><title>Procedures</title>'
    b'   <entry><procedure><code code="29881" codeSystem="2.16.840.1.113883.6.12" displayName="Knee arthroscopy"/>'
    b'    <effectiveTime value="20170210"/></procedure></entry></section></component>\n </structuredBody>')


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, 'h.db'))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class CodeTests(StoreCase):
    def test_typed_codes_are_written_one_way(self):
        self.assertEqual(codes.normalize('M5450', 'conditions'), 'ICD-10-CM M54.50')
        self.assertEqual(codes.normalize('icd10 e11.9; snomed 44054006', 'conditions'), 'ICD-10-CM E11.9; SNOMED CT 44054006')
        self.assertEqual(codes.normalize('22612, J1885', 'procedures'), 'CPT 22612; HCPCS J1885')
        self.assertEqual(codes.normalize('CPT: 22612; cpt 22612', 'procedures'), 'CPT 22612')   # once
        self.assertIsNone(codes.normalize('  ', 'conditions'))

    def test_wrong_codes_are_refused(self):
        for text, kind in (('hello', 'conditions'), ('CPT 2261', 'procedures'), ('ICD-10 12345', 'conditions')):
            with self.assertRaises(codes.CodeError, msg=text):
                codes.normalize(text, kind)
        with self.assertRaises(ValidationError):
            self.store.create('conditions', {'name': 'Back pain', 'codes': 'not a code'})

    def test_entry_keeps_codes_and_is_found_by_them(self):
        c = self.store.create('conditions', {'name': 'Low back pain', 'codes': 'm54.50'})
        self.assertEqual(c['codes'], 'ICD-10-CM M54.50')
        hits = self.store.search('M54.50')['records']
        self.assertEqual([h['id'] for h in hits], [c['id']])

    def test_codes_read_from_a_health_summary(self):
        d = ccda.parse(CODED_DOC)
        self.assertEqual(d['problems'][0]['codes'], 'SNOMED CT 714628002; ICD-10-CM R73.03')
        self.assertEqual([(p['name'], p['date'], p['codes']) for p in d['procedures']],
                         [('Knee arthroscopy', '2017-02-10', 'CPT 29881')])

    def test_codes_read_from_fhir(self):
        b = {'resourceType': 'Bundle', 'entry': [
            {'resource': {'resourceType': 'Condition', 'code': {'text': 'Prediabetes', 'coding': [
                {'system': 'http://hl7.org/fhir/sid/icd-10-cm', 'code': 'R7303'}]}}},
            {'resource': {'resourceType': 'Procedure', 'performedDateTime': '2017-02-10', 'code': {'text': 'Knee scope', 'coding': [
                {'system': 'http://www.ama-assn.org/go/cpt', 'code': '29881'}]}}}]}
        f = fhir.parse(json.dumps(b).encode())
        self.assertEqual(f['problems'][0]['codes'], 'ICD-10-CM R73.03')
        self.assertEqual(f['procedures'][0]['codes'], 'CPT 29881')

    def test_merged_conditions_keep_every_code(self):
        self.store.upsert_imported('conditions', [
            {'id': 'ccda-cond-1', 'name': 'Hypertension', 'status': 'active', 'codes': 'ICD-10-CM I10'},
            {'id': 'ccda-cond-2', 'name': 'High blood pressure', 'status': 'active', 'codes': 'SNOMED CT 38341003'}])
        self.store.merge_conditions()
        (c,) = self.store.list('conditions')
        self.assertEqual(set(codes.split(c['codes'])), {('ICD-10-CM', 'I10'), ('SNOMED CT', '38341003')})

    def test_codes_from_a_source_never_replace_a_persons(self):
        c = self.store.create('conditions', {'name': 'Asthma', 'codes': 'J45.909'})
        self.assertFalse(self.store.add_codes('conditions', c['id'], 'SNOMED CT 195967001'))
        bare = self.store.create('conditions', {'name': 'Gout'})
        self.assertTrue(self.store.add_codes('conditions', bare['id'], 'ICD-10-CM M10.9'))


class LifeEventTests(StoreCase):
    def test_life_event_is_on_the_timeline(self):
        e = self.store.create('life_events', {'date': '2019-06', 'title': 'Car accident', 'category': 'Accident or injury',
                                              'health_effect': 'Neck pain since'})
        (ev,) = [x for x in self.store.timeline() if x['kind'] == 'life_events']
        self.assertEqual((ev['id'], ev['date'], ev['title']), (e['id'], '2019-06', 'Car accident'))
        self.assertIn('Neck pain since', ev['detail'])
        self.assertEqual(self.store.summary()['counts']['life_events'], 1)

    def test_life_event_dates_are_checked(self):
        with self.assertRaises(ValidationError):
            self.store.create('life_events', {'date': 'last summer', 'title': 'Move'})


class ReportTests(StoreCase):
    def setUp(self):
        super().setUp()
        s = self.store
        s.create('conditions', {'name': 'Migraine', 'status': 'active', 'first_noted': '2015', 'codes': 'G43.909'})
        s.create('conditions', {'name': 'Broken wrist', 'status': 'resolved', 'first_noted': '2010-04'})
        s.create('allergies', {'substance': 'Sampleillin', 'reaction': 'Hives'})
        s.create('medications', {'name': 'Examplol', 'dose': '10 mg', 'status': 'active', 'start_date': '2019-01-01'})
        s.create('procedures', {'date': '2017-02-10', 'name': 'Knee <arthroscopy>', 'codes': 'CPT 29881'})
        s.create('encounters', {'date': '2021-05-03', 'type': 'Office', 'reason': 'Check-up', 'facility': 'Riverside Clinic'})
        s.create('life_events', {'date': '2018-09', 'title': 'Moved house', 'category': 'Move or home'})
        s.upsert_imported('labs', [{'id': 'l1', 'date': '2021-05-03', 'analyte': 'Glucose', 'value': '130', 'num': 130,
                                    'unit': 'mg/dL', 'flag': 'H', 'ref_range': '70-99'}])

    def test_summary_has_every_section(self):
        page = build(self.store, 'Jane Doe')
        for text in ('Health summary: Jane Doe', 'Migraine', 'ICD-10-CM G43.909', 'Sampleillin', 'Examplol',
                     'CPT 29881', 'Moved house', 'Glucose', '3 May 2021', 'Broken wrist', 'Print or save as PDF'):
            self.assertIn(text, page)
        self.assertIn('Knee &lt;arthroscopy&gt;', page)       # what was typed is shown, never run
        self.assertNotIn('<arthroscopy>', page)

    def test_medicines_and_allergies_list(self):
        from healthdash.report import med_list
        self.store.create('medications', {'name': 'Samplecough syrup', 'dose': '10 ml', 'status': 'as needed'})
        self.store.create('medications', {'name': 'Oldpril', 'start_date': '2015', 'end_date': '2018', 'status': 'stopped'})
        self.store.set_otc({'words': ['samplecough']})
        page = med_list(self.store, 'Jane Doe')
        for text in ('Medicines and allergies: Jane Doe', 'Sampleillin', 'Hives', 'Examplol', 'Samplecough syrup'):
            self.assertIn(text, page)
        self.assertNotIn('Oldpril', page)                     # stopped
        self.assertNotIn('Migraine', page)                    # conditions aren't on it
        self.assertNotIn('Samplecough', med_list(self.store, 'Jane Doe', otc=False))

    def test_dates_and_sections_limit_it(self):
        page = build(self.store, 'Jane Doe', start='2020', sections=['history'])
        self.assertIn('Check-up', page)
        self.assertNotIn('Moved house', page)                 # before the range
        self.assertNotIn('Current conditions', page)          # section left out
        page = build(self.store, 'Jane Doe', sections=['history'], kinds=['life_events'])
        self.assertIn('Moved house', page)
        self.assertNotIn('Check-up', page)


class ServerTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.files = os.path.join(self.tmp.name, 'files')
        os.makedirs(self.files)
        texts = {b'%PDF-1 one': 'Riverside Clinic\nOffice visit 03/04/2020\nFollow-up for a cough.',
                 b'%PDF-1 two': 'Riverside Clinic\nLetter dated 05/06/2021\nYour results were normal.'}
        def extract(path):
            with open(path, 'rb') as f:
                return {'pages': [texts[f.read()]], 'ocr_pages': []}
        self.up = Uploads(self.store, self.files, extractor=extract)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.store, self.files, set(), 'T', self.up))
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path):
        with urllib.request.urlopen(self.url + path) as r:
            return r.read().decode(), r.headers

    def test_report_page_and_download(self):
        self.store.create('life_events', {'date': '2018-09', 'title': 'Moved house'})
        page, h = self.get('/report')
        self.assertIn('Moved house', page)
        self.assertTrue(h['Content-Type'].startswith('text/html'))
        self.assertIn("default-src 'none'", h['Content-Security-Policy'])
        _, h = self.get('/report?download=1&sections=life')
        self.assertRegex(h['Content-Disposition'], r'attachment; filename="health-summary-Main-\d{4}-\d{2}-\d{2}\.html"')
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get('/report?from=yesterday')
        self.assertEqual(e.exception.code, 400)
        e.exception.close()

    def test_medicines_list_page_and_download(self):
        self.store.create('allergies', {'substance': 'Sampleillin'})
        page, h = self.get('/report?view=meds')
        self.assertIn('Sampleillin', page)
        self.assertIn('nonce-', h['Content-Security-Policy'])
        _, h = self.get('/report?view=meds&download=1')
        self.assertIn('filename="medicines-and-allergies-Main-', h['Content-Disposition'])

    def test_codes_backfill_matches_renamed_and_merged_entries(self):
        with open(os.path.join(self.files, 'x' * 64 + '.xml'), 'wb') as f:
            f.write(CODED_DOC.replace(b'<reference value="#p1"/></originalText>', b'</originalText>')
                    .replace(b'<content ID="p1">Prediabetes</content>', b'')
                    .replace(b'<originalText></originalText>', b'<originalText>HTN</originalText>')
                    .replace(b'displayName="Knee arthroscopy"', b'displayName="ARTHROSCOPY KNEE"'))
        self.store.upsert_documents([{'id': 'x' * 64, 'ext': '.xml', 'status': 'done', 'needs_review': False}])
        self.store.set_setting('codes_version', '0')
        htn = self.store.create('conditions', {'name': 'High blood pressure (hypertension)'})   # shown name
        knee = self.store.create('procedures', {'name': 'Right knee arthroscopy', 'date': '2017-02-11'})   # a day off
        other = self.store.create('procedures', {'name': 'Appendectomy', 'date': '2017-02-10'})
        self.assertEqual(self.up.backfill_codes(), 2)
        self.assertEqual(self.store.get('conditions', htn['id'])['codes'], 'SNOMED CT 714628002; ICD-10-CM R73.03')
        self.assertEqual(self.store.get('procedures', knee['id'])['codes'], 'CPT 29881')
        self.assertIsNone(self.store.get('procedures', other['id'])['codes'])     # not related
        self.assertEqual(self.up.backfill_codes(), 0)                              # once only

    def test_zip_of_loose_records_is_read_file_by_file(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('records/visit.pdf', b'%PDF-1 one')
            z.writestr('records/2021/letter.pdf', b'%PDF-1 two')
            z.writestr('records/notes.docx', b'not read')
            z.writestr('__MACOSX/records/._visit.pdf', b'junk')
        data = buf.getvalue()
        req = urllib.request.Request(f'{self.url}/api/upload?name=box.zip', data=data, method='POST')
        with urllib.request.urlopen(req) as r:
            sha = json.loads(r.read())['id']
        for _ in range(200):
            docs = self.store.documents()
            if len(docs) == 3 and all(d['status'] != 'processing' for d in docs):
                break
            time.sleep(0.05)
        else:
            self.fail('the zip was never read')
        zipdoc = self.store.document(sha)
        self.assertIn('2 added', zipdoc['summary'])
        self.assertIn('notes.docx', zipdoc['summary'])         # said what was left out
        self.assertEqual(sorted(d['orig_path'] for d in self.store.documents() if d['id'] != sha),
                         ['letter.pdf', 'visit.pdf'])

    def test_portal_exports_are_still_exports(self):
        def zipped(names):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as z:
                for n in names:
                    z.writestr(n, b'x')
            p = os.path.join(self.tmp.name, 'z.zip')
            with open(p, 'wb') as f:
                f.write(buf.getvalue())
            return p
        self.assertTrue(is_export(zipped(['IHE_XDM/SUBSET01/DOC0001.XML', 'IHE_XDM/SUBSET01/scan.pdf'])))
        self.assertTrue(is_export(zipped(['AllPatientData.json', 'scan.pdf'])))
        self.assertTrue(is_export(zipped(['summary.xml'])))
        self.assertFalse(is_export(zipped(['a.pdf', 'b.jpg', 'summary.xml'])))


if __name__ == '__main__':
    unittest.main()

"""Upload + classify tests. Made-up documents only."""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from healthdash import classify
from healthdash.server import make_handler
from healthdash.store import Store
from healthdash.uploads import Uploads

LAB = """Riverside General Hospital Laboratory
Patient: Jane Doe   DOB: 02/03/1970
Final Result  Collected 04-Mar-2020 07:46
GLUCOSE          130 H    74-106     mg/dL
SODIUM           139      136-145    mmol/L
POTASSIUM        4.1      3.5-5.1    mmol/L
CREATININE       0.9      0.6-1.2    mg/dL
"""

IMAGING = """RIVERSIDE GENERAL HOSPITAL - DEPARTMENT OF RADIOLOGY
Patient: Jane Doe        DOB: 02/03/1970
Exam Date: 06/18/2019
EXAM: XR ANKLE 3 VIEWS RIGHT
TECHNIQUE: Three views of the right ankle.
COMPARISON: None.
FINDINGS: Nondisplaced fracture of the distal fibula.
IMPRESSION: Hairline fracture, distal fibula.
Radiologist: Dr. Robin Example
"""

DENTAL_QUOTE = """Maple Street Dental
Treatment Plan Estimate     Date: 11/20/2023
Patient: Jane Doe   DOB 02/03/1970
D1110 Cleaning, adult            $120.00
D2740 Crown, porcelain            $1,250.00
Total Treatment Plan  $1,370.00
This estimate is not a bill. Insurance estimates may change.
"""

VAGUE = """Dear Jane,
Thank you for your letter. We will be in touch.
Sincerely, the office
"""

KNOWN = ['Riverside General Hospital', 'Maple Street Dental', 'Maple Street Family Practice']


class ClassifyTests(unittest.TestCase):
    def test_lab_report(self):
        r = classify.classify(LAB, 'scan001.pdf', KNOWN)
        self.assertEqual(r['category'], 'labs')
        self.assertEqual(r['doc_date'], '2020-03-04')
        self.assertEqual(r['facility'], 'Riverside General Hospital')
        self.assertEqual(len(r['labs']), 4)
        self.assertEqual(r['questions'], [])

    def test_imaging_report_skips_birth_date(self):
        r = classify.classify(IMAGING, 'IMG_2231.pdf', KNOWN)
        self.assertEqual(r['category'], 'imaging')
        self.assertEqual(r['doc_date'], '2019-06-18')
        self.assertEqual(r['title'], 'Xr ankle 3 views right')
        self.assertEqual(r['questions'], [])

    def test_dental_estimate_is_dental_with_quoted_price(self):
        r = classify.classify(DENTAL_QUOTE, 'quote.pdf', KNOWN)
        self.assertEqual(r['category'], 'dental')
        self.assertEqual(r['facility'], 'Maple Street Dental')
        self.assertEqual(r['money']['kind'], 'Estimate (not billed)')
        self.assertEqual(r['money']['charged'], 1370.0)

    def test_unclear_document_asks(self):
        r = classify.classify(VAGUE, 'letter.pdf', KNOWN)
        self.assertTrue(any('What kind' in q for q in r['questions']))
        self.assertTrue(any('What date' in q for q in r['questions']))
        self.assertTrue(any('Who is it from' in q for q in r['questions']))

    def test_town_in_address_is_not_the_hospital(self):
        r = classify.classify('Seen at a clinic in Springfield, IL 62701 for a cough.', 'x.pdf', ['Springfield Hospital'])
        self.assertIsNone(r['facility'])
        r = classify.classify('Discharged from Springfield Hospital.', 'x.pdf', ['Springfield Hospital'])
        self.assertEqual(r['facility'], 'Springfield Hospital')

    def test_two_labelled_dates_on_one_line(self):
        d, sure = classify.guess_date('Admit date: 06-Mar-2021 Date of service: 05-Mar-2021 07:57')
        self.assertEqual((d, sure), ('2021-03-05', True))

    def test_learns_facility_from_filed_documents_as_a_suggestion(self):
        head = 'Patient Portal Lab Summary\nPrinted from the Valley Health portal\nQuestions call the lab line\n'
        filed = classify.Filed([('Valley Hospital', 'labs', head + 'a'), ('Valley Hospital', 'labs', head + 'b'),
                                ('Other Place', 'office', 'Other Place letterhead line\nsomething else here')])
        r = classify.classify(head + 'Results follow\n', 'x.pdf', [], filed)
        self.assertEqual(r['facility'], 'Valley Hospital')
        self.assertTrue(any('Is it from Valley Hospital' in q for q in r['questions']))


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.files = os.path.join(self.tmp.name, 'files')
        os.makedirs(self.files)
        self.store = Store(os.path.join(self.tmp.name, 't.db'))
        self.store.upsert_documents([{'id': 'd0', 'path': 'x.pdf', 'title': 'Old', 'facility': 'Riverside General Hospital',
                                      'text': 'old', 'needs_review': False}])
        texts = {b'%PDF-1 lab': LAB, b'%PDF-1 quote': DENTAL_QUOTE, b'%PDF-1 vague': VAGUE}

        def fake_extract(path):
            with open(path, 'rb') as f:
                return {'pages': [texts[f.read()]], 'ocr_pages': []}
        self.up = Uploads(self.store, self.files, extractor=fake_extract)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.store, self.files, set(), 'T', self.up))
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.tmp.cleanup()

    def upload(self, name, data):
        req = urllib.request.Request(f'{self.url}/api/upload?name={name}', data=data, method='POST')
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def wait(self, sha):
        for _ in range(100):
            d = self.store.document(sha)
            if d['status'] != 'processing':
                return d
            time.sleep(0.05)
        self.fail('upload never finished')

    def test_lab_upload_is_filed_with_values(self):
        r = self.upload('labs.pdf', b'%PDF-1 lab')
        self.assertEqual(r['status'], 'processing')
        d = self.wait(r['id'])
        self.assertEqual((d['category'], d['doc_date'], d['needs_review']), ('labs', '2020-03-04', 0))
        self.assertEqual(len([x for x in d['linked'] if x['kind'] == 'labs']), 4)
        self.assertTrue(self.upload('labs.pdf', b'%PDF-1 lab')['duplicate'])

    def test_estimate_goes_to_financial_as_estimate(self):
        d = self.wait(self.upload('quote.pdf', b'%PDF-1 quote')['id'])
        self.assertIn('financial', [x['kind'] for x in d['linked']])
        fin = self.store.get('financial', 'fin-u' + d['id'][:10])
        self.assertEqual((fin['kind'], fin['charged']), ('Estimate (not billed)', 1370.0))
        self.assertEqual(self.store.summary()['financial']['charged'], 0)  # not counted as billed

    def test_unclear_upload_asks_and_saving_clears_it(self):
        d = self.wait(self.upload('letter.pdf', b'%PDF-1 vague')['id'])
        self.assertEqual(d['needs_review'], 1)
        self.assertIn('What kind', d['review_note'])
        self.assertIn(d['id'], [x['id'] for x in self.store.review_queue()['documents']])
        self.store.update_document(d['id'], {'category': 'other', 'needs_review': 0, 'review_note': None})
        self.assertNotIn(d['id'], [x['id'] for x in self.store.review_queue()['documents']])

    def test_rejects_non_pdf(self):
        for name, data in (('evil.pdf', b'MZ not a pdf'), ('notes.exe', b'%PDF')):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.upload(name, data)
            self.assertEqual(cm.exception.code, 400)
            cm.exception.close()

    def test_unreadable_file_is_flagged(self):
        up = Uploads(self.store, self.files, extractor=lambda p: (_ for _ in ()).throw(RuntimeError('no tools')))
        r = up.add('scan.pdf', b'%PDF-1 other')
        for _ in range(100):
            d = self.store.document(r['id'])
            if d['status'] != 'processing':
                break
            time.sleep(0.05)
        self.assertEqual(d['status'], 'error')
        self.assertIn('no tools', d['review_note'])


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.files = os.path.join(self.tmp.name, 'f')
        os.makedirs(self.files)
        self.store = Store(os.path.join(self.tmp.name, 't.db'))
        self.texts = {}
        self.up = Uploads(self.store, self.files, extractor=lambda p: {'pages': [self.texts[open(p, 'rb').read()]], 'ocr_pages': []})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, data, text, name='x.pdf'):
        self.texts[data] = text
        sha = self.up.add(name, data)['id']
        for _ in range(100):
            d = self.store.document(sha)
            if d['status'] != 'processing':
                return d
            time.sleep(0.05)

    def test_same_text_different_file_is_flagged_and_adds_nothing(self):
        a = self.add(b'%PDF-1 a', LAB)
        self.assertEqual(len(self.store.list('labs')), 4)
        b = self.add(b'%PDF-1 b (re-exported)', LAB + '\nPrinted again\n')
        self.assertEqual(b['duplicate_of'], a['id'])
        self.assertEqual(b['needs_review'], 1)
        self.assertIn('looks like a copy', b['review_note'])
        self.assertEqual(len(self.store.list('labs')), 4)  # no doubled trend points
        self.assertTrue(self.up.delete(b['id']))
        self.assertIsNone(self.store.document(b['id']))
        self.assertFalse(any(f.startswith(b['id']) for f in os.listdir(self.files)))
        self.assertFalse(self.up.delete(b['id']))

    def test_lab_values_already_recorded_are_not_added_twice(self):
        self.add(b'%PDF-1 a', LAB)
        other = LAB.replace('Riverside General Hospital Laboratory', 'Cumulative report, other layout\n' * 30) \
                   .replace('CREATININE       0.9', 'CREATININE       1.4')
        self.add(b'%PDF-1 c', other)
        labs = self.store.list('labs')
        self.assertEqual(len(labs), 5)  # only the new creatinine value was added
        self.assertEqual(sorted(x['num'] for x in labs if x['analyte'] == 'Creatinine'), [0.9, 1.4])

    def test_different_lab_days_are_not_duplicates(self):
        day2 = LAB.replace('04-Mar-2020', '05-Mar-2020').replace('130 H', '101  ').replace('139', '141') \
                  .replace('4.1', '3.9').replace('0.9 ', '1.0 ')
        self.add(b'%PDF-1 a', LAB)
        d = self.add(b'%PDF-1 d', day2)
        self.assertIsNone(d['duplicate_of'])

    def test_only_uploads_can_be_deleted(self):
        self.store.upsert_documents([{'id': 'orig', 'path': 'Hospital/x.pdf', 'title': 'x', 'text': 't'}])
        self.assertFalse(self.up.delete('orig'))
        self.assertIsNotNone(self.store.document('orig'))


class DoctorAndRetryTests(unittest.TestCase):
    def test_failed_upload_can_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = os.path.join(tmp, 'f')
            os.makedirs(files)
            store = Store(os.path.join(tmp, 't.db'))
            state = {'tools': False}

            def extract(path):
                if not state['tools']:
                    raise RuntimeError("the dashboard can't read files yet (missing: tesseract)")
                return {'pages': [LAB], 'ocr_pages': []}
            up = Uploads(store, files, extractor=extract)

            def wait(sha):
                for _ in range(100):
                    d = store.document(sha)
                    if d['status'] != 'processing':
                        return d
                    time.sleep(0.05)
            sha = up.add('labs.pdf', b'%PDF-1 retry')['id']
            self.assertEqual(wait(sha)['status'], 'error')
            state['tools'] = True
            self.assertTrue(up.retry(sha))
            d = wait(sha)
            self.assertEqual((d['status'], d['category']), ('done', 'labs'))
            self.assertFalse(up.retry('nope'))
            store.close()


if __name__ == '__main__':
    unittest.main()

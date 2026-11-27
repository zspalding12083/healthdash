"""The emergency card: its own details, what it shows from the records, and its routes. Made-up data only."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from healthdash import emergency
from healthdash.pdf import render
from healthdash.report import to_html
from healthdash.server import make_handler
from healthdash.store import Store, ValidationError


def texts(doc):
    out = []
    for b in doc['blocks']:
        if b[0] == 'table':
            out += [c['text'] for r in b[2] if isinstance(r, list) for c in r]
        else:
            out.append(b[1])
    return ' | '.join(out)


class CardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, 'h.db'))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_details_are_checked(self):
        for bad in ({'dob': '03/07/1960'}, {'blood_type': 'C+'}, {'notes': 'x' * 900},
                    {'contacts': [{'name': f'P{i}'} for i in range(5)]}):
            with self.assertRaises(ValidationError, msg=bad):
                emergency.save(self.store, bad)
        d = emergency.save(self.store, {'blood_type': 'o+', 'contacts': [{'name': ' Pat Sample ', 'phone': '555-0100'}, {}]})
        self.assertEqual(d['blood_type'], 'O+')
        self.assertEqual(d['contacts'], [{'name': 'Pat Sample', 'relation': '', 'phone': '555-0100'}])   # empty rows dropped
        self.assertEqual(emergency.load(self.store), d)

    def test_card_from_details_and_records(self):
        emergency.save(self.store, {'dob': '1960-03-07', 'blood_type': 'A-', 'devices': 'Sample pacemaker (2019)',
                                    'contacts': [{'name': 'Pat Sample', 'relation': 'Daughter', 'phone': '555-0100'}],
                                    'doctor': {'name': 'Dr. Example', 'phone': '555-0199'}, 'notes': 'Takes a blood thinner'})
        self.store.create('allergies', {'substance': 'Sampleillin', 'reaction': 'Hives', 'severity': 'Severe'})
        self.store.create('medications', {'name': 'Examplol', 'dose': '10 mg', 'schedule': 'daily'})
        self.store.create('medications', {'name': 'Sample multivitamin'})
        self.store.create('conditions', {'name': 'Sample arrhythmia', 'first_noted': '2018-02'})
        self.store.create('conditions', {'name': 'Old sprain', 'status': 'resolved'})
        self.store.create('procedures', {'name': 'Sample ablation', 'date': '2019-05-01'})
        doc = emergency.card(self.store, 'Sam', today=date(2026, 3, 6))
        t = texts(doc)
        for want in ('Born 7 Mar 1960 (age 65)', 'Blood type A-', 'Pat Sample', '555-0100', 'Sampleillin',
                     'Examplol', 'Sample arrhythmia', 'Sample pacemaker', 'Sample ablation', 'Dr. Example',
                     'Takes a blood thinner', '1 over-the-counter medicine'):
            self.assertIn(want, t)
        self.assertNotIn('Old sprain', t)                         # resolved
        self.assertNotIn('multivitamin', t)                        # over the counter: only counted
        allergy = next(b for b in doc['blocks'] if b[0] == 'table' and b[1][0][0] == 'Allergic to')
        self.assertTrue(allergy[2][0][2]['flag'])                 # severe shown in red
        self.assertIn('<h1>Emergency health information: Sam</h1>', to_html(doc, 'n'))
        self.assertTrue(render(doc).startswith(b'%PDF'))

    def test_chosen_conditions_and_the_rest_counted(self):
        ids = [self.store.create('conditions', {'name': f'Sample condition {i}', 'first_noted': f'20{10 + i}'})['id']
               for i in range(15)]
        t = texts(emergency.card(self.store, 'Sam'))
        self.assertIn('3 more current conditions', t)             # the 12 newest by default
        emergency.save(self.store, {'conditions': [ids[0]]})
        t = texts(emergency.card(self.store, 'Sam'))
        self.assertIn('Sample condition 0', t)
        self.assertNotIn('Sample condition 14', t)
        self.assertIn('14 more current conditions are on the dashboard.', t)

    def test_long_allergy_list_stays_compact(self):
        self.store.create('allergies', {'substance': 'Samplenut', 'reaction': 'Anaphylaxis', 'severity': 'severe'})
        for i in range(14):
            self.store.create('allergies', {'substance': f'Sample allergen {i}', 'reaction': 'Rash' if i % 2 else None})
        doc = emergency.card(self.store, 'Sam')
        allergy = next(b for b in doc['blocks'] if b[0] == 'table' and b[1][0][0] == 'Allergic to')
        self.assertEqual([r[0]['text'] for r in allergy[2]], ['Samplenut'])        # the severe one in the table
        rest = next(b[1] for b in doc['blocks'] if b[0] == 'strong' and b[1].startswith('Also allergic to: '))
        self.assertIn('Sample allergen 0; Sample allergen 1 (Rash);', rest)

    def test_empty_card_says_what_to_add(self):
        t = texts(emergency.card(self.store, 'Sam'))
        self.assertIn('None entered yet', t)
        self.assertIn('No known allergies recorded.', t)


class CardRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, 'h.db'))
        files = os.path.join(self.tmp.name, 'files')
        os.makedirs(files)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.store, files, set(), 'T'))
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.tmp.cleanup()

    def put(self, body):
        req = urllib.request.Request(self.url + '/api/emergency', data=json.dumps(body).encode(), method='PUT',
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def test_edit_and_print(self):
        self.store.create('conditions', {'name': 'Sample asthma'})
        with urllib.request.urlopen(self.url + '/api/emergency') as r:
            e = json.loads(r.read())
        self.assertEqual([c['name'] for c in e['conditions']], ['Sample asthma'])
        self.assertEqual(self.put({'blood_type': 'B+'})['blood_type'], 'B+')
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.put({'dob': 'yesterday'})
        self.assertEqual(err.exception.code, 400)
        err.exception.close()
        with urllib.request.urlopen(self.url + '/report?view=card') as r:
            page = r.read().decode()
            self.assertIn('Blood type B+', page)
            self.assertIn('nonce-', r.headers['Content-Security-Policy'])
        with urllib.request.urlopen(self.url + '/report?view=card&format=pdf&download=1') as r:
            self.assertRegex(r.headers['Content-Disposition'], r'attachment; filename="emergency-card-Main-[\d-]+\.pdf"')
            self.assertTrue(r.read().startswith(b'%PDF'))


if __name__ == '__main__':
    unittest.main()

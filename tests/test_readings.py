"""Home readings: entries, flags, pasted lists and the printout. Made-up data only."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from healthdash import readings
from healthdash.pdf import render
from healthdash.report import to_html
from healthdash.server import make_handler
from healthdash.store import Store, ValidationError

TODAY = date(2026, 11, 27)


def texts(doc):
    out = []
    for b in doc['blocks']:
        if b[0] == 'table':
            out += [c['text'] for r in b[2] if isinstance(r, list) for c in r]
        else:
            out.append(b[1])
    return ' | '.join(out)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, 'h.db'))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, type, reading, reading2=None, d='2026-11-20', t=None, **kw):
        return self.store.create('readings', dict(type=type, reading=reading, reading2=reading2, date=d, time=t, **kw))


class EntryTests(StoreCase):
    def test_checked_and_flagged(self):
        for bad in ({'type': 'pulse', 'date': '2026-11-20'},                       # no reading
                    {'type': 'pulse', 'reading': 'fast', 'date': '2026-11-20'},
                    {'type': 'pulse', 'reading': 70},                              # no date
                    {'type': 'pulse', 'reading': 70, 'date': '2026-11-20', 'time': '8am'},
                    {'reading': 70, 'date': '2026-11-20'}):
            with self.assertRaises(ValidationError, msg=bad):
                self.store.create('readings', bad)
        self.add('blood_pressure', 150, 85, t='08:15')
        self.add('blood_pressure', 118, 76, t='20:00')
        self.add('temperature', 38.5, unit='°C')                                    # not the usual unit: no flag
        self.add('oxygen', 90)
        got = {(r['type'], r['reading']): r['flag'] for r in self.store.list('readings')}
        self.assertEqual(got, {('blood_pressure', 150): 'H', ('blood_pressure', 118): None,
                               ('temperature', 38.5): None, ('oxygen', 90): 'L'})

    def test_numbers_that_cant_be_sent_and_overlong_text_refused(self):
        for bad in ('nan', 'inf', '-Infinity', '1e999'):
            with self.assertRaises(ValidationError, msg=bad):
                self.add('pulse', bad)
            with self.assertRaises(ValidationError, msg=bad):
                self.store.create('labs', {'analyte': 'Sample test', 'num': bad, 'date': '2026-11-20'})
        for f, n in (('type', 61), ('unit', 21), ('notes', 1001)):
            with self.assertRaises(ValidationError, msg=f):
                self.store.create('readings', {'type': 'pulse', 'reading': 70, 'date': '2026-11-20', f: 'x' * n})
        with self.assertRaises(ValidationError):
            self.store.create('questions', {'question': 'x' * 2001})
        self.assertEqual(self.store.list('readings'), [])

    def test_paste_capped(self):
        text = '\n'.join(f'2020-01-{1 + i % 28:02d} {i // 60 % 24:02d}:{i % 60:02d} {60 + i % 40}' for i in range(readings.MAX_PASTE + 1))
        with self.assertRaises(ValidationError):
            readings.add_pasted(self.store, text, 'pulse', apply=True, today=TODAY)
        self.assertEqual(self.store.list('readings'), [])

    def test_newest_first_by_day_and_time(self):
        self.add('pulse', 70, d='2026-11-19', t='21:00')
        self.add('pulse', 71, t='07:00')
        self.add('pulse', 72, t='19:00')
        self.assertEqual([r['reading'] for r in self.store.list('readings')], [72, 71, 70])


class PasteTests(StoreCase):
    TEXT = """Date,Time,Systolic,Diastolic,Pulse
2026-11-20,8:15 AM,128,82,72
11/21/2026 8:30 PM 145/92
11/22/26\t07:05\t118\t76
2026-11-23 9:00 120
2027-01-01 8:00 120/80
written on the fridge: 11/24/2026 130/85"""

    def test_formats_errors_and_pulse(self):
        p = readings.parse(self.TEXT, 'blood_pressure', today=TODAY)
        bp = [(r['date'], r['time'], r['reading'], r['reading2']) for r in p['rows'] if r['type'] == 'blood_pressure']
        self.assertEqual(bp, [('2026-11-20', '08:15', 128, 82), ('2026-11-21', '20:30', 145, 92), ('2026-11-22', '07:05', 118, 76)])
        self.assertEqual([(r['type'], r['reading']) for r in p['rows'] if r['type'] == 'pulse'], [('pulse', 72)])
        self.assertEqual([(n, why) for n, _, why in p['errors']],
                         [(5, 'needs two numbers, like 128/82'), (6, 'the date is in the future')])

    def test_saved_once(self):
        first = readings.add_pasted(self.store, self.TEXT, 'blood_pressure', apply=True, today=TODAY)
        self.assertEqual(first['saved'], 4)
        again = readings.add_pasted(self.store, self.TEXT, 'blood_pressure', apply=True, today=TODAY)
        self.assertEqual((again['new'], again['saved']), (0, 0))
        w = readings.add_pasted(self.store, '2026-11-25 161.4\n2026-11-26 160.8', 'weight', 'kg', apply=True, today=TODAY)
        self.assertEqual(w['saved'], 2)
        self.assertEqual({r['unit'] for r in self.store.list('readings') if r['type'] == 'weight'}, {'kg'})

    def test_odd_numbers_refused(self):
        p = readings.parse('2026-11-20 82/128\n2026-11-21 12/8', 'blood_pressure', today=TODAY)
        self.assertEqual(p['rows'], [])
        self.assertEqual(len(p['errors']), 2)


class SheetTests(StoreCase):
    def test_averages_mornings_and_evenings_and_every_reading(self):
        self.add('blood_pressure', 130, 85, d='2026-11-20', t='07:30')
        self.add('blood_pressure', 140, 90, d='2026-11-21', t='08:00', notes='after coffee')
        self.add('blood_pressure', 121, 79, d='2026-11-21', t='20:00')
        self.add('blood_pressure', 160, 100, d='2026-01-02', t='09:00')            # outside the 30 days
        self.add('weight', 162.0, d='2026-11-01')
        self.add('weight', 160.5, d='2026-11-25')
        doc = readings.sheet(self.store, 'Sam Sample', days=30, today=TODAY)
        t = texts(doc)
        self.assertIn('Average 130/85 mmHg; lowest 121, highest 140; 1 outside the usual range.', t)
        self.assertIn('Mornings average 135/88 (2); afternoons and evenings 121/79 (1).', t)
        self.assertIn('down 1.5 lb over this time', t)
        self.assertIn('after coffee', t)
        self.assertNotIn('160/100', t)
        self.assertEqual([b[1] for b in doc['blocks'] if b[0] == 'h2'], ['Blood pressure (3)', 'Weight (2)'])
        flagged = [r[1] for b in doc['blocks'] if b[0] == 'table' for r in b[2] if r[1]['text'].startswith('140/90')]
        self.assertTrue(flagged[0]['flag'])
        self.assertIn('160/100', texts(readings.sheet(self.store, 'Sam Sample', today=TODAY)))       # all of them
        only = readings.sheet(self.store, 'Sam Sample', types=['weight'], today=TODAY)
        self.assertEqual([b[1] for b in only['blocks'] if b[0] == 'h2'], ['Weight (2)'])
        self.assertTrue(render(doc).startswith(b'%PDF'))
        self.assertIn('after coffee', to_html(doc, 'n', None))

    def test_empty(self):
        self.assertIn('No home readings', texts(readings.sheet(self.store, 'Sam Sample', today=TODAY)))


class RouteTests(unittest.TestCase):
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

    def call(self, path, body=None, method=None):
        req = urllib.request.Request(self.url + path, method=method, headers={'Content-Type': 'application/json'},
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers, r.read()

    def test_add_paste_and_print(self):
        code, _, _ = self.call('/api/readings', {'type': 'pulse', 'reading': 64, 'date': '2026-11-20', 'time': '07:00',
                                                 'notes': '<script>x</script>'}, 'POST')
        self.assertEqual(code, 201)
        _, _, body = self.call('/api/readings/paste', {'type': 'blood_pressure', 'text': '2026-11-20 07:00 128/82'}, 'POST')
        self.assertEqual(json.loads(body)['new'], 1)
        self.assertEqual(len(json.loads(self.call('/api/readings')[2])), 1)          # only checked, not saved
        self.call('/api/readings/paste', {'type': 'blood_pressure', 'text': '2026-11-20 07:00 128/82', 'apply': True}, 'POST')
        rows = json.loads(self.call('/api/readings')[2])
        self.assertEqual(sorted(r['type'] for r in rows), ['blood_pressure', 'pulse'])
        _, h, page = self.call('/report?view=readings')
        self.assertIn(b'&lt;script&gt;x&lt;/script&gt;', page)
        self.assertIn(b'128/82 mmHg', page)
        _, h, pdf = self.call('/report?view=readings&days=30&format=pdf&download=1')
        self.assertTrue(pdf.startswith(b'%PDF'))
        self.assertIn('home-readings-', h['Content-Disposition'])
        for bad in ('/report?view=readings&days=-3', '/report?view=readings&days=lots'):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.call(bad)
            self.assertEqual(e.exception.code, 400)
            e.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.call('/api/readings/paste', {'text': '2026-11-20 128/82'}, 'POST')     # no type
        self.assertEqual(e.exception.code, 400)
        e.exception.close()


if __name__ == '__main__':
    unittest.main()

"""Questions for the next visit: kept like other entries, printed to take along. Made-up data only."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime
from http.server import ThreadingHTTPServer

from healthdash import questions
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


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, 'h.db'))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, question, **kw):
        return self.store.create('questions', dict(question=question, **kw))


class QuestionEntryTests(StoreCase):
    def test_new_question_is_open_and_dated_today(self):
        q = self.add('Can the sample pill be taken at night?')
        self.assertEqual((q['status'], q['date']), ('open', datetime.now().strftime('%Y-%m-%d')))
        self.assertEqual(self.store.summary()['open_questions'], 1)

    def test_checked(self):
        with self.assertRaises(ValidationError):
            self.add('')
        q = self.add('Is the rash anything to worry about?')
        for bad in ({'status': 'maybe'}, {'question': '  '}, {'answered_on': 'last week'}):
            with self.assertRaises(ValidationError, msg=bad):
                self.store.update('questions', q['id'], bad)
        self.store.update('questions', q['id'], {'status': 'answered', 'answer': 'No.', 'answered_on': '2026-03-04'})
        self.assertEqual(self.store.summary()['open_questions'], 0)

    def test_listed_open_first_then_important(self):
        self.add('Old answered one', status='answered', date='2026-01-01')
        self.add('Plain', date='2026-02-01')
        self.add('Urgent', important=1, date='2026-01-15')
        self.assertEqual([q['question'] for q in self.store.list('questions')], ['Urgent', 'Plain', 'Old answered one'])

    def test_search_finds_questions_and_answers(self):
        self.add('Should the knee brace be worn at night?', status='answered', answer='Only after exercise.')
        self.assertEqual([r['kind'] for r in self.store.search('brace')['records']], ['questions'])
        self.assertEqual(len(self.store.search('exercise')['records']), 1)


class SheetTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.add('Can the dose go down?', ask='Dr Sample', date='2026-05-02')
        self.add('Is the knee better?', ask='dr  sample', date='2026-05-01')
        self.add('Flu shot this year?', date='2026-04-01')
        self.add('New inhaler?', ask='Riverside Lung Clinic', important=1, date='2026-05-03')
        self.add('Done already', ask='Dr Sample', status='answered')

    def test_grouped_by_who_to_ask_with_room_to_write(self):
        doc = questions.sheet(self.store, 'Sam Sample', today=date(2026, 6, 1))
        heads = [b[1] for b in doc['blocks'] if b[0] == 'h2']
        self.assertEqual(heads, ['For Dr Sample', 'For Riverside Lung Clinic', 'Any doctor', 'Other questions that came up'])
        t = texts(doc)
        self.assertNotIn('Done already', t)
        self.assertLess(t.index('Is the knee better?'), t.index('Can the dose go down?'))   # oldest first
        self.assertIn('4 questions · printed 1 Jun 2026', t)
        first = [b for b in doc['blocks'] if b[0] == 'table'][0][2]
        self.assertEqual(sum(1 for r in first if r == ('blank',)), 2 * questions.WRITE_ROWS)
        lung = [b for b in doc['blocks'] if b[0] == 'table'][1][2][0][1]
        self.assertTrue(lung['flag'])
        self.assertIn('Important', lung['note'])
        self.assertTrue(render(doc).startswith(b'%PDF'))
        self.assertIn('Can the dose go down?', to_html(doc, 'n', None))

    def test_one_doctors_list_keeps_the_ones_for_anyone(self):
        doc = questions.sheet(self.store, 'Sam Sample', ask='DR SAMPLE')
        t = texts(doc)
        self.assertIn('Questions for DR SAMPLE: Sam Sample', t)
        self.assertIn('Flu shot this year?', t)
        self.assertNotIn('New inhaler?', t)

    def test_empty_list_says_how_to_add(self):
        st = Store(os.path.join(self.tmp.name, 'empty.db'))
        self.assertIn('No open questions', texts(questions.sheet(st, 'Sam Sample')))
        st.close()


class QuestionRouteTests(unittest.TestCase):
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

    def test_add_answer_and_print(self):
        code, _, body = self.call('/api/questions', {'question': 'Is <b>this</b> normal?', 'ask': 'Dr Sample'}, 'POST')
        self.assertEqual(code, 201)
        qid = json.loads(body)['id']
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.call('/api/questions', {'question': ''}, 'POST')
        self.assertEqual(e.exception.code, 400)
        e.exception.close()
        _, h, page = self.call('/report?view=questions&ask=Dr%20Sample')
        self.assertIn(b'Is &lt;b&gt;this&lt;/b&gt; normal?', page)
        self.assertIn("default-src 'none'", h['Content-Security-Policy'])
        _, h, pdf = self.call('/report?view=questions&format=pdf&download=1')
        self.assertTrue(pdf.startswith(b'%PDF'))
        self.assertIn('questions-', h['Content-Disposition'])
        self.call(f'/api/questions/{qid}', {'status': 'answered', 'answer': 'Yes.', 'answered_on': '2026-06-01'}, 'PUT')
        self.assertEqual(json.loads(self.call('/api/summary')[2])['open_questions'], 0)


if __name__ == '__main__':
    unittest.main()

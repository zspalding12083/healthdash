"""Record checks: the questions on the Review tab. Made-up data only."""

import os
import tempfile
import unittest
from datetime import date

from healthdash import checks
from healthdash.store import Store

TODAY = date(2030, 6, 1)


class ChecksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, 't.db'))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ask(self):
        checks._CACHE.clear()
        return {x['key']: x for x in checks.issues(self.store, TODAY)}

    def test_names_that_are_not_the_same(self):
        for a, b in (('Hypokalemia', 'Hyperkalemia'), ('Urine RBC', 'Urine WBC'),
                     ('Example IgG Band 23', 'Example IgG Band 28'), ('Visual acuity (OD)', 'Visual acuity (OS)'),
                     ('Glucose', 'Glucose CSF'), ('Near vision loss', 'Vision loss'), ('Source', 'Specimen Source'),
                     ('Infection', 'Infection, ear'), ('Cast, Rbc, Urine', 'Urine RBC')):
            self.assertFalse(checks._alike(a, b), (a, b))
        for a, b in (('Eczema', 'Atopic eczema'), ('Ferritin', 'Ferritin Level'),
                     ('History of gout', 'Gout'), ('Postsurgical knee replacement status', 'Status post knee replacement')):
            self.assertTrue(checks._alike(a, b), (a, b))

    def test_short_supply_still_current_is_asked_and_stopped(self):
        self.store.upsert_imported('medications', [{
            'id': 'ccda-med-1', 'name': 'Examplol 5 mg oral tablet', 'status': 'active', 'start_date': '2019-03-04',
            'schedule': '5 mg, = 1 tab, Oral, q6hr, # 8 tab, 0 Refill(s), for pain'}])
        q = self.ask()['med-stale:ccda-med-1']
        self.assertIn('8 tabs with no refills', q['note'])
        checks.answer(self.store, q['key'], 'stop')
        m = self.store.get('medications', 'ccda-med-1')
        self.assertEqual((m['status'], m['end_date'], m['edited']), ('stopped', '2019-03-11', 1))
        self.assertNotIn(q['key'], self.ask())

    def test_same_condition_merges_and_is_learned(self):
        self.store.upsert_imported('conditions', [
            {'id': 'ccda-cond-a', 'name': 'Eczema', 'status': 'active', 'first_noted': '2021-05-05', 'docs': ['d1']},
            {'id': 'ccda-cond-b', 'name': 'Atopic eczema', 'status': 'active', 'first_noted': '2018-02-02', 'docs': ['d2']}])
        q = next(x for x in self.ask().values() if x['kind'] == 'conditions')
        checks.answer(self.store, q['key'], 'keep:ccda-cond-a')
        (c,) = self.store.list('conditions')
        self.assertEqual((c['name'], c['first_noted'], sorted(c['docs'])), ('Eczema', '2018-02-02', ['d1', 'd2']))
        # a later import brings the other wording back: it joins without asking
        self.store.upsert_imported('conditions', [{'id': 'ccda-cond-c', 'name': 'Atopic eczema', 'status': 'active', 'docs': ['d3']}])
        self.store.merge_conditions()
        self.assertEqual([x['name'] for x in self.store.list('conditions')], ['Eczema'])

    def test_not_the_same_is_remembered(self):
        self.store.upsert_imported('conditions', [
            {'id': 'ccda-cond-a', 'name': 'Sinus bradycardia', 'status': 'active'},
            {'id': 'ccda-cond-b', 'name': 'Bradycardia', 'status': 'active'}])
        (q,) = self.ask().values()
        checks.answer(self.store, q['key'], 'no')
        self.assertEqual(self.ask(), {})
        self.assertEqual(len(self.store.list('conditions')), 2)

    def test_same_lab_test_is_renamed_and_learned(self):
        self.store.upsert_imported('labs', [
            {'id': 'l1', 'date': '2020-01-01', 'analyte': 'Ferritin', 'value': '25 ng/mL', 'num': 25, 'unit': 'ng/mL'},
            {'id': 'l2', 'date': '2027-07-07', 'analyte': 'Ferritin Level', 'value': '30 ng/mL', 'num': 30, 'unit': 'ng/mL'}])
        q = next(x for x in self.ask().values() if x['kind'] == 'labs')
        self.assertEqual([(s['name'], [r['text'] for r in s['rows']]) for s in q['sides']],
                         [('Ferritin', ['2020-01-01: 25 ng/mL']), ('Ferritin Level', ['2027-07-07: 30 ng/mL'])])
        checks.answer(self.store, q['key'], 'name:Ferritin')
        self.assertEqual({l['analyte'] for l in self.store.list('labs')}, {'Ferritin'})
        self.assertEqual(checks.learned_lab_names(self.store), {'ferritin level': 'Ferritin'})

    def test_same_medication_twice_at_once(self):
        self.store.upsert_imported('medications', [
            {'id': 'ccda-med-a', 'name': 'Examplol 10 MG Oral Tab', 'status': 'active', 'start_date': '2021-01-01', 'docs': ['d1']}])
        self.store.create('medications', {'name': 'Examplol (Brandex)', 'status': 'stopped', 'start_date': '2025-01-01',
                                          'end_date': '2029-09-09'})
        q = next(x for x in self.ask().values() if x['kind'] == 'medications')
        keep = next(a['id'] for a in q['answers'] if a['id'].startswith('keep:') and not a['id'].endswith('ccda-med-a'))
        checks.answer(self.store, q['key'], keep)
        (m,) = self.store.list('medications')
        self.assertEqual((m['name'], m['status'], m['docs']), ('Examplol (Brandex)', 'stopped', ['d1']))

    def test_department_named_as_a_place(self):
        self.store.upsert_imported('encounters', [
            {'id': 'e1', 'date': '2021-04-04', 'type': 'Outpatient', 'facility': 'Sleep Lab Hospital'},
            {'id': 'e2', 'date': '2022-02-02', 'type': 'ER', 'facility': 'Sample General Hospital'}])
        q = next(x for x in self.ask().values() if x['kind'] == 'places')
        self.assertEqual(q['title'], 'Sleep Lab Hospital')
        checks.answer(self.store, q['key'], 'place:Sample General Hospital')
        self.assertEqual(self.store.get('encounters', 'e1')['facility'], 'Sample General Hospital')


if __name__ == '__main__':
    unittest.main()

"""Starting over: records go, accounts and the people menu stay. Made-up data only."""

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from healthdash import auth as authmod
from healthdash.auth import Auth
from healthdash.people import People
from healthdash.store import Store
from healthdash.wipe import wipe


class WipeTests(unittest.TestCase):
    def test_wipe_keeps_accounts_and_people(self):
        with mock.patch.object(authmod, 'ITERATIONS', 1000), tempfile.TemporaryDirectory() as tmp:
            files = os.path.join(tmp, 'files'); os.makedirs(files)
            st = Store(os.path.join(tmp, 'health.db'))
            a = Auth(st); a.add_user('ann', 'password1'); key = a.create_key('ann', 'Firefox')['key']
            st.upsert_documents([{'id': 'd1', 'path': 'x.pdf', 'title': 'Ankle x-ray', 'text': 'fibula', 'needs_review': False}])
            st.upsert_imported('labs', [{'id': 'l1', 'date': '2020-01-01', 'analyte': 'WBC', 'value': '5', 'docs': ['d1']}])
            open(os.path.join(files, 'd1.pdf'), 'wb').write(b'%PDF')
            ppl = People(st, files, None, state_dir=tmp, main_name='Jane')
            ppl.add('Sam')
            ppl.get('sam').store.upsert_imported('conditions', [{'id': 'c1', 'name': 'Asthma'}])
            open(os.path.join(tmp, 'people', 'sam', 'files', 'x.html'), 'w').write('x')
            ppl.close(); st.close()

            self.assertEqual(wipe(os.path.join(tmp, 'health.db'), files, ask=lambda _: 'no'), 1)   # asks first
            self.assertEqual(wipe(os.path.join(tmp, 'health.db'), files, ask=lambda _: 'WIPE'), 0)

            st = Store(os.path.join(tmp, 'health.db'))
            self.assertEqual((st.list('labs'), st.documents()), ([], []))
            self.assertEqual(st.search('fibula')['documents'], [])
            self.assertEqual(os.listdir(files), [])
            a = Auth(st)
            self.assertTrue(a.login('ann', 'password1'))
            self.assertEqual(a.key_user(key), 'ann')
            ppl = People(st, files, None, state_dir=tmp)
            self.assertEqual([p['name'] for p in ppl.list()], ['Jane', 'Sam'])
            self.assertEqual(ppl.get('sam').store.list('conditions'), [])
            self.assertEqual(os.listdir(os.path.join(tmp, 'people', 'sam', 'files')), [])
            ppl.close(); st.close()
            raw = sqlite3.connect(os.path.join(tmp, 'health.db'))
            self.assertNotIn(b'fibula', open(os.path.join(tmp, 'health.db'), 'rb').read())   # really gone
            raw.close()


if __name__ == '__main__':
    unittest.main()

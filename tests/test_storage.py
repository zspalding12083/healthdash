"""Choosing where the original records are kept, and the readable copy there. Made-up data only."""

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from healthdash import storage
from healthdash.people import People
from healthdash.server import make_handler
from healthdash.store import Store
from healthdash.uploads import Uploads
from healthdash.wipe import wipe


def fake_extract(path):
    return {'pages': ['Riverside Clinic\nOffice visit 03/04/2020\nFollow-up for a cough.'], 'ocr_pages': []}


class StorageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.state = os.path.join(t, 'state')
        self.files = os.path.join(self.state, 'files')
        os.makedirs(self.files)
        self.chosen = os.path.join(t, 'Records')
        os.makedirs(self.chosen)
        self.db = os.path.join(self.state, 'health.db')
        self.store = Store(self.db)
        self.up = Uploads(self.store, self.files, extractor=fake_extract)
        self.people = People(self.store, self.files, self.up, state_dir=self.state,
                             make_uploads=lambda st, f: Uploads(st, f, extractor=fake_extract), main_name='Sam Sample')
        self.st = storage.Storage(self.people)

    def tearDown(self):
        self.people.close()
        self.store.close()
        self.tmp.cleanup()

    def add_doc(self, store, files_dir, data, title, date='2020-03-04', category='office', facility='Riverside Clinic'):
        sha = hashlib.sha256(data).hexdigest()
        with open(os.path.join(files_dir, sha + '.pdf'), 'wb') as f:
            f.write(data)
        store.upsert_documents([{'id': sha, 'ext': '.pdf', 'path': f'Uploaded/{title}.pdf', 'title': title,
                                 'doc_date': date, 'category': category, 'facility': facility,
                                 'status': 'done', 'needs_review': False}])
        store.set_has_file(sha)
        return sha

    def wait_moved(self):
        for _ in range(200):
            if self.st.status.get('state') != 'moving':
                return self.st.status
            time.sleep(0.02)
        self.fail('the move never finished')


class CheckFolderTests(unittest.TestCase):
    def test_refuses_what_is_not_a_records_folder(self):
        for bad in ('', 'relative/path', '/no/such/folder/here', '/etc', '/usr/share', '/'):
            with self.assertRaises(storage.StorageError, msg=bad):
                storage.check_folder(bad)
        here = os.path.dirname(os.path.dirname(storage.__file__))
        with self.assertRaises(storage.StorageError):
            storage.check_folder(here)                      # the program's own folder

    def test_accepts_a_writable_folder(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(storage.check_folder(d + '/'), os.path.realpath(d))
            self.assertEqual(os.listdir(d), [])             # the write test leaves nothing behind


class MoveTests(StorageCase):
    def test_move_to_a_folder_and_back(self):
        sha = self.add_doc(self.store, self.files, b'%PDF-1 visit', 'Office visit')
        alex = self.people.add('Alex Sample')
        ap = self.people.get(alex['id'])
        asha = self.add_doc(ap.store, ap.files_dir, b'%PDF-1 letter', 'Results letter', '2021-05-06', 'labs')

        self.st.set_folder(self.chosen)
        self.assertEqual(self.wait_moved()['state'], 'done')
        main_dir = os.path.join(os.path.realpath(self.chosen), '.healthdash-store', 'main')
        self.assertTrue(os.path.isfile(os.path.join(main_dir, sha + '.pdf')))
        self.assertFalse(os.path.exists(os.path.join(self.files, sha + '.pdf')))       # moved, not copied
        self.assertEqual(self.people.get('main').files_dir, main_dir)
        self.assertEqual(self.up.files_dir, main_dir)                                   # new uploads go there
        self.assertEqual(self.store.get_setting('files_folder'), os.path.realpath(self.chosen))

        self.st.sync()
        sam = os.path.join(self.chosen, 'Sam-Sample', '02 Office visits and consults',
                           '2020-03-04_Riverside-Clinic_Office-visit.pdf')
        self.assertTrue(os.path.samefile(sam, os.path.join(main_dir, sha + '.pdf')))   # linked: no extra space
        self.assertTrue(os.path.isfile(os.path.join(self.chosen, 'Alex-Sample', '04 Labs',
                                                    '2021-05-06_Riverside-Clinic_Results-letter.pdf')))

        self.st.set_folder('')                                                          # back to the data folder
        self.assertEqual(self.wait_moved()['state'], 'done')
        self.assertTrue(os.path.isfile(os.path.join(self.files, sha + '.pdf')))
        self.assertTrue(os.path.isfile(os.path.join(ap.files_dir, asha + '.pdf')))
        self.assertFalse(os.path.exists(sam))                                           # the readable copy is gone
        self.assertFalse(os.path.exists(os.path.join(self.chosen, 'Sam-Sample')))
        self.assertEqual(os.listdir(self.chosen), [])                                   # left as it was found

    def test_folder_kept_after_restart(self):
        self.st.set_folder(self.chosen)
        self.wait_moved()
        again = storage.Storage(self.people)
        self.assertEqual(again.folder, os.path.realpath(self.chosen))
        self.assertTrue(self.people.get('main').files_dir.endswith('/.healthdash-store/main'))

    def test_new_person_files_go_in_the_chosen_folder(self):
        self.st.set_folder(self.chosen)
        self.wait_moved()
        p = self.people.get(self.people.add('Alex Sample')['id'])
        self.assertEqual(p.files_dir, os.path.join(os.path.realpath(self.chosen), '.healthdash-store', p.id))
        self.assertEqual(p.uploads.files_dir, p.files_dir)

    def test_upload_lands_in_the_chosen_folder(self):
        self.st.set_folder(self.chosen)
        self.wait_moved()
        r = self.up.add('scan.pdf', b'%PDF-1 new scan')
        self.assertTrue(os.path.isfile(os.path.join(self.up.files_dir, r['id'] + '.pdf')))
        for _ in range(200):                       # let the worker read it before the database closes
            if self.store.document(r['id'])['status'] != 'processing':
                break
            time.sleep(0.02)

    def test_missing_drive_is_said_plainly(self):
        self.st.set_folder(self.chosen)
        self.wait_moved()
        os.rename(self.chosen, self.chosen + '-unplugged')
        self.assertFalse(self.st.info()['available'])
        with self.assertRaisesRegex(Exception, 'drive connected'):
            self.up.add('scan.pdf', b'%PDF-1 another scan')


class ReadableCopyTests(StorageCase):
    def setUp(self):
        super().setUp()
        self.st.set_folder(self.chosen)
        self.wait_moved()
        self.dir = self.people.get('main').files_dir

    def names(self):
        out = []
        for root, dirs, files in os.walk(self.chosen):
            dirs[:] = [d for d in dirs if d != '.healthdash-store']
            out += [os.path.relpath(os.path.join(root, f), self.chosen) for f in files]
        return sorted(out)

    def test_follows_renames_and_deletes_and_leaves_own_files_alone(self):
        sha = self.add_doc(self.store, self.dir, b'%PDF-1 visit', 'Office visit')
        self.add_doc(self.store, self.dir, b'%PDF-1 other', 'Office visit')          # the same name
        own = os.path.join(self.chosen, 'Sam-Sample', 'my notes.txt')
        os.makedirs(os.path.dirname(own), exist_ok=True)
        with open(own, 'w') as f:
            f.write('mine')
        self.st.sync()
        self.assertEqual(self.names(), ['Sam-Sample/02 Office visits and consults/2020-03-04_Riverside-Clinic_Office-visit.pdf',
                                        'Sam-Sample/02 Office visits and consults/2020-03-04_Riverside-Clinic_Office-visit_2.pdf',
                                        'Sam-Sample/my notes.txt'])
        self.assertEqual(self.st.sync()['added'], 0)                                  # nothing to do twice
        self.store.update_document(sha, {'title': 'Knee follow-up', 'doc_date': '2020-03-05'})
        self.st.sync()
        self.assertIn('Sam-Sample/02 Office visits and consults/2020-03-05_Riverside-Clinic_Knee-follow-up.pdf', self.names())
        self.up.delete(sha)
        self.st.sync()
        self.assertEqual(len(self.names()), 2)
        self.assertTrue(os.path.isfile(own))                                          # never touched

    def test_originals_are_read_only_and_redrawn_pages_relinked(self):
        from healthdash.uploads import write_page
        sha = self.add_doc(self.store, self.dir, b'%PDF-1 visit', 'Office visit')
        self.st.sync()
        src = os.path.join(self.dir, sha + '.pdf')
        self.assertFalse(os.stat(src).st_mode & 0o222)                   # nobody saves over the original
        write_page(src, 'drawn again, longer')                           # swapped in although read-only
        self.st.sync()
        (name,) = self.names()
        self.assertTrue(os.path.samefile(os.path.join(self.chosen, name), src))

    def test_an_edited_list_cannot_remove_files_elsewhere(self):
        victim = os.path.join(self.state, 'keep.txt')
        with open(victim, 'w') as f:
            f.write('important')
        listed = os.path.join(self.chosen, '.healthdash-store', 'readable.json')
        os.makedirs(os.path.dirname(listed), exist_ok=True)
        with open(listed, 'w') as f:
            json.dump({'../state/keep.txt': ['/nowhere/x.pdf', 9],
                       'Sam-Sample/x.pdf': [victim, 9]}, f)
        self.st.sync()
        self.assertTrue(os.path.isfile(victim))

    def test_renaming_a_person_renames_their_folder(self):
        self.add_doc(self.store, self.dir, b'%PDF-1 visit', 'Office visit')
        self.st.sync()
        self.people.rename('main', 'Samantha Sample')
        self.st.sync()
        self.assertTrue(all(n.startswith('Samantha-Sample/') for n in self.names()))

    def test_wipe_clears_the_chosen_folder_too(self):
        sha = self.add_doc(self.store, self.dir, b'%PDF-1 visit', 'Office visit')
        self.st.sync()
        self.store.close()
        self.assertEqual(wipe(self.db, self.files, yes=True), 0)
        self.assertEqual(self.names(), [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, sha + '.pdf')))
        self.store = Store(self.db)


class StorageApiTests(StorageCase):
    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.store, self.files, set(), 'T', self.up,
                                                                        people=self.people))
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def post(self, body):
        req = urllib.request.Request(self.url + '/api/storage', data=json.dumps(body).encode(), method='POST',
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())

    def test_show_and_change_the_folder(self):
        with urllib.request.urlopen(self.url + '/api/storage') as r:
            info = json.loads(r.read())
        self.assertIsNone(info['folder'])
        self.assertEqual(info['default'], self.files)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post({'folder': '/etc'})
        self.assertEqual(e.exception.code, 400)
        self.assertIn('system', json.loads(e.exception.read())['error'])
        e.exception.close()
        code, info = self.post({'folder': self.chosen})
        self.assertEqual(code, 202)
        self.assertEqual(self.wait_moved()['state'], 'done')
        self.assertEqual(self.st.folder, os.path.realpath(self.chosen))


if __name__ == '__main__':
    unittest.main()

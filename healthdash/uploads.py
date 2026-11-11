"""Files added from the dashboard: store, read, work out what they are, file them.

An upload is saved and listed at once (status "processing"); a background thread then
extracts the text (OCR for scans), classifies it, adds any lab values and the bill amount,
and either files it or flags it with questions for a person (status "done").
"""

import hashlib
import os
import queue
import re
import shutil
import threading
import time
import traceback

from . import classify, doctor
from .extract import extract_file
from .store import ValidationError, now

TYPES = {'.pdf': (b'%PDF',), '.jpg': (b'\xff\xd8\xff',), '.jpeg': (b'\xff\xd8\xff',),
         '.png': (b'\x89PNG',), '.tif': (b'II*\x00', b'MM\x00*'), '.tiff': (b'II*\x00', b'MM\x00*'),
         '.html': (b'<',)}  # a portal page saved by the browser plugin
MIN_PAGE_TEXT = 300  # a saved page with less text than this is a menu or a loading screen
UPLOAD_DIR = 'Uploaded'
NO_TOOLS = "the dashboard can't read files yet"


def tools_missing(ext):
    need = ['pdftotext', 'pdfinfo', 'pdftoppm', 'tesseract'] if ext == '.pdf' else ['tesseract']
    return [t for t in need if not shutil.which(t)]


def default_extract(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == '.html':
        from .extract import html_text
        with open(path, 'rb') as f:
            return {'pages': [html_text(f.read())], 'ocr_pages': []}
    missing = tools_missing(ext)
    if missing:
        raise RuntimeError(NO_TOOLS + ' (missing: ' + ', '.join(missing) + '). Install them with: '
                           + (doctor.install_command(missing) or 'your package manager'))
    return extract_file(path)


def safe_name(name):
    name = os.path.basename((name or '').replace('\\', '/')).strip() or 'upload'
    name = re.sub(r'[^\w.\- ()&,]+', '_', name)
    return name[-120:]


class Uploads:
    def __init__(self, store, files_dir, extractor=None, recheck=60):
        self.store, self.files_dir = store, files_dir
        self.extract = extractor or default_extract
        self.q = queue.Queue()
        threading.Thread(target=self._work, daemon=True, name='uploads').start()
        for d in store.documents():  # finish anything interrupted by a restart
            if d.get('status') == 'processing':
                self.q.put(d['id'])
        for sha in store.stale_uploads(classify.VERSION):  # sorting rules improved: sort again
            self.q.put(('resort', sha))
        if recheck and not extractor:
            threading.Thread(target=self._recheck, args=(recheck,), daemon=True, name='uploads-recheck').start()

    def _recheck(self, every):
        """Files that couldn't be read only because the tools were missing are read as soon as
        the tools are installed; no need to upload them again."""
        while True:
            if doctor.upload_status()['can_read']:
                for d in self.store.documents():
                    if d.get('status') == 'error' and NO_TOOLS in (d.get('review_note') or ''):
                        self.retry(d['id'])
            time.sleep(every)

    def retry(self, sha):
        d = self.store.document(sha)
        if not d or not d.get('has_file') or d.get('status') == 'processing':
            return False
        self.store.set_upload_result(sha, status='processing', review_note=None, duplicate_of=None)
        self.q.put(sha)
        return True

    def add(self, name, data, source='dashboard'):
        name = safe_name(name)
        ext = os.path.splitext(name)[1].lower()
        if ext not in TYPES or (ext == '.html' and source != 'plugin'):
            raise ValidationError('only PDFs and pictures (JPG, PNG, TIFF) can be added')
        if ext == '.html':
            data = data.lstrip()
        if not data or not any(data.startswith(m) for m in TYPES[ext]):
            raise ValidationError(f"{name} doesn't look like a real {ext[1:].upper()} file")
        sha = hashlib.sha256(data).hexdigest()
        old = self.store.document(sha)
        if old:
            return {'id': sha, 'duplicate': True, 'title': old['title'], 'status': old.get('status') or 'done'}
        tmp = os.path.join(self.files_dir, sha + ext + '.tmp')
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, os.path.join(self.files_dir, sha + ext))
        self.store.upsert_documents([{
            'id': sha, 'path': f'{UPLOAD_DIR}/{name}', 'orig_path': name, 'title': os.path.splitext(name)[0],
            'category': 'other', 'size': len(data), 'ext': ext, 'text': '', 'needs_review': True,
            'status': 'processing', 'summary': (f'Saved by the browser plugin on {now()[:10]}.' if source == 'plugin'
                                                else f'Added from the dashboard on {now()[:10]}.')}])
        self.store.set_has_file(sha)
        self.q.put(sha)
        return {'id': sha, 'duplicate': False, 'title': name, 'status': 'processing'}

    def _work(self):
        while True:
            item = self.q.get()
            resort = isinstance(item, tuple)
            sha = item[1] if resort else item
            try:
                self.process(sha, reuse_text=resort)
            except Exception as e:  # noqa: BLE001 - keep the worker alive, tell the person
                if not isinstance(e, RuntimeError):  # RuntimeError: an expected, explained failure
                    traceback.print_exc()
                self.store.set_upload_result(sha, status='error', needs_review=True,
                                             review_note=f"Couldn't read this file: {e}")

    def process(self, sha, reuse_text=False):
        d = self.store.document(sha)
        if not d:
            return
        if reuse_text and d.get('text'):
            text = d['text']
            ex = {'pages': text.split('\f'), 'ocr_pages': d.get('ocr_pages') or []}
        else:
            path = os.path.join(self.files_dir, sha + (d.get('ext') or ''))
            ex = self.extract(path)
            text = '\f'.join(ex['pages'])
        self.store.clear_upload_entries(sha)
        r = classify.classify(text, d.get('orig_path') or '', self.store.known_facilities(),
                              classify.Filed(self.store.filed_for_learning()))
        dup = classify.near_duplicate(text, self.store.texts_except(sha))
        if (d.get('ext') == '.html' and not reuse_text and
                (len(text.strip()) < MIN_PAGE_TEXT or (dup and dup['how'] == 'copy' and dup['score'] >= 0.95))):
            self.delete(sha)  # a page with nothing new on it: not worth asking about
            return None
        questions = list(r['questions'])
        if dup:
            when = f" from {dup['doc_date']}" if dup.get('doc_date') else ''
            questions.insert(0, f"This looks like a copy of \u201c{dup['title']}\u201d{when}, which is already in the "
                                "records. If it's the same, delete this one." if dup['how'] == 'copy' else
                             f"Most of this text is already in \u201c{dup['title']}\u201d{when}. "
                             "Keep it only if it's a separate document.")
        r['questions'] = questions
        self.store.set_upload_result(
            sha, text=text, pages=len(ex['pages']), ocr_pages=ex.get('ocr_pages', []),
            title=r['title'], doc_date=r['doc_date'], category=r['category'], facility=r['facility'],
            status='done', needs_review=bool(questions), review_note='\n'.join(questions) or None,
            duplicate_of=dup['id'] if dup else None, classified=classify.VERSION)
        if d.get('ext') == '.html' and not reuse_text:
            # a page saved before it finished loading is replaced by the fuller save
            new = classify.shingles(text)
            for oid, otext in self.store.plugin_pages(sha):
                old = classify.shingles(otext)
                if len(old) >= 10 and len(new) > len(old) and len(old & new) / len(old) >= 0.9:
                    self.delete(oid)
        if dup and dup['how'] == 'copy':
            return r  # nothing new to add from a copy
        new_labs = [row for row in r['labs'] if not self.store.lab_exists(row)]
        if new_labs:
            rows = [dict(row, id=f'lab-u{sha[:10]}-{i}', docs=[sha]) for i, row in enumerate(new_labs)]
            self.store.upsert_imported('labs', rows)
        if r['money'] and not self.store.bill_exists(r['doc_date'], r['money']['charged']):
            self.store.upsert_imported('financial', [{
                'id': f'fin-u{sha[:10]}', 'docs': [sha], 'date': r['doc_date'], 'kind': r['money']['kind'],
                'payee': r['facility'], 'description': r['title'], 'charged': r['money']['charged'],
                'needs_review': bool(r['questions']) or not r['money']['sure']}])
        return r

    def delete(self, sha):
        d = self.store.delete_upload(sha)
        if not d:
            return False
        for f in os.listdir(self.files_dir):
            if f.split('.')[0] == sha:
                os.remove(os.path.join(self.files_dir, f))
        return True

    def uploaded(self):
        return [d for d in self.store.documents() if (d.get('path') or '').startswith(UPLOAD_DIR + '/')]

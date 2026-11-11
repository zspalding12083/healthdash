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
import traceback

from . import classify
from .extract import extract_file
from .store import ValidationError, now

TYPES = {'.pdf': (b'%PDF',), '.jpg': (b'\xff\xd8\xff',), '.jpeg': (b'\xff\xd8\xff',),
         '.png': (b'\x89PNG',), '.tif': (b'II*\x00', b'MM\x00*'), '.tiff': (b'II*\x00', b'MM\x00*')}
UPLOAD_DIR = 'Uploaded'


def tools_missing(ext):
    need = ['pdftotext', 'pdfinfo', 'pdftoppm', 'tesseract'] if ext == '.pdf' else ['tesseract']
    return [t for t in need if not shutil.which(t)]


def default_extract(path):
    ext = os.path.splitext(path)[1].lower()
    missing = tools_missing(ext)
    if missing:
        raise RuntimeError("the dashboard can't read files yet (missing: " + ', '.join(missing)
                           + '). Install poppler-utils and tesseract-ocr on the server.')
    return extract_file(path)


def safe_name(name):
    name = os.path.basename((name or '').replace('\\', '/')).strip() or 'upload'
    name = re.sub(r'[^\w.\- ()&,]+', '_', name)
    return name[-120:]


class Uploads:
    def __init__(self, store, files_dir, extractor=None):
        self.store, self.files_dir = store, files_dir
        self.extract = extractor or default_extract
        self.q = queue.Queue()
        threading.Thread(target=self._work, daemon=True, name='uploads').start()
        for d in store.documents():  # finish anything interrupted by a restart
            if d.get('status') == 'processing':
                self.q.put(d['id'])

    def add(self, name, data):
        name = safe_name(name)
        ext = os.path.splitext(name)[1].lower()
        if ext not in TYPES:
            raise ValidationError('only PDFs and pictures (JPG, PNG, TIFF) can be added')
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
            'status': 'processing', 'summary': f'Added from the dashboard on {now()[:10]}.'}])
        self.store.set_has_file(sha)
        self.q.put(sha)
        return {'id': sha, 'duplicate': False, 'title': name, 'status': 'processing'}

    def _work(self):
        while True:
            sha = self.q.get()
            try:
                self.process(sha)
            except Exception as e:  # noqa: BLE001 - keep the worker alive, tell the person
                traceback.print_exc()
                self.store.set_upload_result(sha, status='error', needs_review=True,
                                             review_note=f"Couldn't read this file: {e}")

    def process(self, sha):
        d = self.store.document(sha)
        if not d:
            return
        path = os.path.join(self.files_dir, sha + (d.get('ext') or ''))
        ex = self.extract(path)
        text = '\f'.join(ex['pages'])
        r = classify.classify(text, d.get('orig_path') or '', self.store.known_facilities(),
                              classify.Filed(self.store.filed_for_learning()))
        self.store.set_upload_result(
            sha, text=text, pages=len(ex['pages']), ocr_pages=ex.get('ocr_pages', []),
            title=r['title'], doc_date=r['doc_date'], category=r['category'], facility=r['facility'],
            status='done', needs_review=bool(r['questions']), review_note='\n'.join(r['questions']) or None)
        if r['labs']:
            rows = [dict(row, id=f'lab-u{sha[:10]}-{i}', docs=[sha]) for i, row in enumerate(r['labs'])]
            self.store.upsert_imported('labs', rows)
        if r['money']:
            self.store.upsert_imported('financial', [{
                'id': f'fin-u{sha[:10]}', 'docs': [sha], 'date': r['doc_date'], 'kind': r['money']['kind'],
                'payee': r['facility'], 'description': r['title'], 'charged': r['money']['charged'],
                'needs_review': bool(r['questions']) or not r['money']['sure']}])
        return r

    def uploaded(self):
        return [d for d in self.store.documents() if (d.get('path') or '').startswith(UPLOAD_DIR + '/')]

"""Ingest: extract text from the source folder and send everything to the dashboard.

Runs on the machine that has the source files, pdftotext and tesseract:

  1. extract text (OCR where needed) for any new file, cached in extracted/
  2. describe each file from data/catalog.json (new files get a guess + "needs review")
  3. POST documents and structured records (data/records.json) to the Pi's /api/import
  4. upload any source file the Pi doesn't have yet (checked by sha256)

Records in data/records.json refer to documents by their path in the source folder; that is
translated to the document id (sha256) here.
"""

import json
import os
import urllib.request

from . import catalog, config
from .extract import extract_all
from .organize import organized_paths
from .store import KINDS

NO_UPLOAD = {'.iso'}
MAX_UPLOAD = 150 * 1024 * 1024


def _post(url, payload, ctype='application/json'):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method='POST', headers={'Content-Type': ctype})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read() or b'{}')


def _get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


def build(source, log=print):
    manifest = extract_all(source, config.EXTRACTED, log=log)
    cat = catalog.load()
    sorted_already = os.path.realpath(source) == os.path.realpath(config.ORGANIZED)   # the sorted copy is the source
    organized = {} if sorted_already else organized_paths(source)
    # files that were moved or renamed since the catalog and records.json were written
    aliases = {}
    if os.path.exists(config.ALIASES):
        with open(config.ALIASES, encoding='utf-8') as f:
            aliases = json.load(f)
    old_name = {new: old for old, new in aliases.items()}
    by_path, documents, discs, unknown, seen = {}, [], [], [], {}
    for m in manifest:
        if m['kind'] == 'disc':
            discs.append(m)
            continue
        rel = m['path']
        meta = catalog.describe(cat, rel)
        if meta.get('needs_review') and rel in old_name:
            meta = catalog.describe(cat, old_name[rel])
        if meta.get('needs_review'):
            unknown.append(rel)
        sha = m['sha256']
        by_path[rel] = sha
        if rel in old_name:
            by_path[old_name[rel]] = sha
        if sha in seen:  # the very same file stored twice in the source folder
            seen[sha]['summary'] = ((seen[sha].get('summary') or '') + f' Same file is also at: {rel}').strip()
            continue
        text, pages, ocr = '', None, []
        if not m.get('no_text'):
            with open(os.path.join(config.EXTRACTED, sha + '.json'), encoding='utf-8') as f:
                ex = json.load(f)
            text = '\f'.join(ex['pages'])
            pages, ocr = len(ex['pages']), ex.get('ocr_pages', [])
        documents.append({
            'id': sha, 'path': rel, 'orig_path': rel, 'organized_path': rel if sorted_already else organized.get(rel),
            'title': meta.get('title'), 'doc_date': meta.get('date'), 'category': meta.get('category'),
            'facility': meta.get('facility'), 'summary': meta.get('summary'), 'pages': pages,
            'ocr_pages': ocr, 'size': m.get('size'), 'ext': m.get('ext'), 'text': text,
            'needs_review': bool(meta.get('needs_review')),
        })
        seen[sha] = documents[-1]
    records = {}
    if os.path.exists(config.RECORDS):
        with open(config.RECORDS, encoding='utf-8') as f:
            records = json.load(f)
    missing_refs = set()
    for kind, items in records.items():
        if kind not in KINDS:
            raise SystemExit(f'records.json: unknown kind {kind!r}')
        for r in items:
            ids = []
            for ref in r.get('docs', []):
                if ref in by_path:
                    ids.append(by_path[ref])
                else:
                    missing_refs.add(ref)
            r['docs'] = ids
    return {'documents': documents, 'records': records, 'discs': discs, 'unknown': unknown,
            'missing_refs': sorted(missing_refs), 'by_path': by_path}


def ingest(source, pi, dry_run=False, log=print):
    b = build(source, log=log)
    log(f"{len(b['documents'])} documents, {len(b['discs'])} imaging discs, "
        + ', '.join(f'{len(v)} {k}' for k, v in b['records'].items()))
    if b['missing_refs']:
        log('records.json refers to files that are not in the source folder:')
        for r in b['missing_refs']:
            log(f'  {r}')
    if b['unknown']:
        log(f"{len(b['unknown'])} file(s) are not in the catalog yet (shown as 'needs review'):")
        for r in b['unknown']:
            log(f'  {r}')
    if dry_run:
        return b
    res = _post(pi + '/api/import', {'documents': b['documents']})
    log(f"documents: {res.get('documents')}")
    if b['records'] or config.PLACE_NAMES or config.OTC:
        res = _post(pi + '/api/import', dict(b['records'], place_names=config.PLACE_NAMES, otc=config.OTC))
        for k, v in res.items():
            log(f'{k}: {v}')
    missing = set(_get(pi + '/api/files/missing'))
    sent = 0
    for d in b['documents']:
        if d['id'] not in missing or (d['ext'] or '') in NO_UPLOAD:
            continue
        full = os.path.join(source, d['path'])
        if os.path.getsize(full) > MAX_UPLOAD:
            continue
        with open(full, 'rb') as f:
            _post(f"{pi}/api/files/{d['id']}{d['ext']}", f.read(), 'application/octet-stream')
        sent += 1
    log(f'uploaded {sent} file(s) to the Pi')
    return b

"""Files added from the dashboard: store, read, work out what they are, file them.

An upload is saved and listed at once (status "processing"); a background thread then
extracts the text (OCR for scans), classifies it, adds any lab values and the bill amount,
and either files it or flags it with questions for a person (status "done").
"""

import hashlib
import json
import os
import queue
import re
import shutil
import threading
import time
import traceback

from . import classify, codes, doctor
from .extract import extract_file
from .store import ValidationError, now

TYPES = {'.pdf': (b'%PDF',), '.jpg': (b'\xff\xd8\xff',), '.jpeg': (b'\xff\xd8\xff',),
         '.png': (b'\x89PNG',), '.tif': (b'II*\x00', b'MM\x00*'), '.tiff': (b'II*\x00', b'MM\x00*'),
         '.html': (b'<',),   # a portal page saved by the browser plugin
         '.zip': (b'PK',), '.xml': (b'<',)}  # a health summary export (C-CDA, e.g. MyChart's)
IMPORTED_DIR = 'Imported'
MIN_PAGE_TEXT = 300  # a saved page with less text than this is a menu or a loading screen
UPLOAD_DIR = 'Uploaded'
NO_TOOLS = "the dashboard can't read files yet"


def tools_missing(ext):
    need = ['pdftotext', 'pdfinfo', 'pdftoppm', 'tesseract'] if ext == '.pdf' else ['tesseract']
    return [t for t in need if not shutil.which(t)]


def default_extract(path):
    """Read an uploaded file with the tools on this machine (pdftotext, tesseract, ...)."""
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
    """Files added from the dashboard or the browser plugin: checked, stored, read in a
    background worker, sorted, and turned into entries. Also runs the one-time passes on startup."""
    def __init__(self, store, files_dir, extractor=None, recheck=60):
        """Start the background worker and queue anything unfinished and any one-time passes due."""
        self.store, self.files_dir = store, files_dir
        self.extract = extractor or default_extract
        self.q = queue.Queue()
        threading.Thread(target=self._work, daemon=True, name='uploads').start()
        for d in store.documents():  # finish anything interrupted by a restart
            if d.get('status') == 'processing':
                self.q.put(d['id'])
        for sha in store.stale_uploads(classify.VERSION):  # sorting rules improved: sort again
            self.q.put(('resort', sha))
        for sha in store.placeholder_dates()[1]:          # read with 1/1/1900 taken as a real date
            self.q.put(('resort', sha))
        from . import ccda
        summaries = any(d.get('ext') in ('.xml', '.zip') for d in store.documents())
        for job, key, version in (('meds', 'meds_version', MEDS_VERSION),            # one entry per drug per period
                                  ('conditions', 'conditions_version', CONDITIONS_VERSION),  # one entry per condition
                                  ('labnames', 'labnames_version', LABNAMES_VERSION),  # one name per lab test
                                  ('places', 'places_version', PLACES_VERSION),        # departments saved as places
                                  ('repage', 'page_version', ccda.PAGE_VERSION),       # pages drawn by an older page()
                                  ('vaccines', 'vaccines_version', VACCINES_VERSION),  # summaries read before vaccines
                                  ('codes', 'codes_version', CODES_VERSION)):          # summaries read before codes
            if store.get_setting(key) != version:
                if summaries:
                    self.q.put((job, None))
                else:
                    store.set_setting(key, version)   # nothing saved yet
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
        """Accept one uploaded file: check its type, store it under its sha256 and queue it for reading.
        Returns {id, duplicate, title, status}."""
        name = safe_name(name)
        ext = os.path.splitext(name)[1].lower()
        if ext not in TYPES or (ext == '.html' and source not in ('plugin', 'export')):
            raise ValidationError('only PDFs, pictures (JPG, PNG, TIFF) and health summary exports (.zip, .xml) can be added')
        if ext == '.html':
            data = data.lstrip()
        if not data or not any(data.startswith(m) for m in TYPES[ext]):
            raise ValidationError(f"{name} doesn't look like a real {ext[1:].upper()} file")
        sha = hashlib.sha256(data).hexdigest()
        old = self.store.document(sha)
        if old:
            return {'id': sha, 'duplicate': True, 'title': old['title'], 'status': old.get('status') or 'done'}
        self.store.undelete('documents', sha)
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
        """The background worker: reads queued files one at a time, and runs one-time passes."""
        while True:
            item = self.q.get()
            if isinstance(item, tuple) and item[1] is None:   # a background job, not a file
                try:
                    {'repage': self.repage, 'vaccines': self.backfill_vaccines, 'places': self.fix_places,
                     'labnames': self.tidy_lab_names, 'conditions': self.merge_conditions_once,
                     'meds': self.merge_meds_once, 'codes': self.backfill_codes}[item[0]]()
                except Exception:  # noqa: BLE001 - pages stay as they were; nothing else depends on it
                    traceback.print_exc()
                continue
            resort = isinstance(item, tuple)
            sha = item[1] if resort else item
            try:
                self.process(sha, reuse_text=resort)
            except Exception as e:  # noqa: BLE001 - keep the worker alive, tell the person
                if not isinstance(e, RuntimeError):  # RuntimeError: an expected, explained failure
                    traceback.print_exc()
                self.store.set_upload_result(sha, status='error', needs_review=True,
                                             review_note=f"Couldn't read this file: {e}")

    def repage(self):
        """Draw the readable pages of imported health summaries again when page() has changed.
        Only the saved .html pages are rewritten; no entries are touched."""
        from . import ccda
        if self.store.get_setting('page_version') == ccda.PAGE_VERSION:
            return 0
        n = 0
        for d in self.store.documents():
            if d.get('ext') not in ('.xml', '.zip') or d.get('status') != 'done':
                continue
            try:
                with open(os.path.join(self.files_dir, d['id'] + d['ext']), 'rb') as f:
                    data = f.read()
                docs = ccda.documents_in(data, d.get('orig_path') or '')
            except (OSError, ccda.NotCCDA):
                continue
            for _name, xml in docs:
                path = os.path.join(self.files_dir, hashlib.sha256(xml + b'#page').hexdigest() + '.html')
                if not os.path.exists(path):
                    continue
                try:
                    page = ccda.page(ccda.parse(xml))
                except ccda.NotCCDA:
                    continue
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(page)
                n += 1
        self.store.set_setting('page_version', ccda.PAGE_VERSION)
        return n

    def merge_meds_once(self):
        """Medications saved before more drug names were known are merged once, the same way an
        import merges them."""
        from . import ccda
        if self.store.get_setting('meds_version') == MEDS_VERSION:
            return 0
        self.store.consolidate_meds(ccda.drug_key, ccda.record_ids)
        n = self.store.merge_med_sources()
        self.store.set_setting('meds_version', MEDS_VERSION)
        return n

    def merge_conditions_once(self):
        """Conditions saved before more synonyms were known are merged once."""
        if self.store.get_setting('conditions_version') == CONDITIONS_VERSION:
            return 0
        n = self.store.merge_conditions()
        self.store.set_setting('conditions_version', CONDITIONS_VERSION)
        return n

    def tidy_lab_names(self):
        """Results saved under names the current rules spell differently ("Hgb A1c.") get the standard
        name, so one test is one trend; rows that are not results (an analyzer's serial number) are
        removed, and the same result saved twice under two names is kept once. Hand-edited rows are
        left alone."""
        from . import parsers
        if self.store.get_setting('labnames_version') == LABNAMES_VERSION:
            return 0
        db, n = self.store.db, 0
        with self.store.lock:
            # results read from uploaded PDFs: Labcorp's footnote letter was kept in the name
            for (old,) in db.execute("SELECT DISTINCT analyte FROM labs WHERE edited=0 AND id LIKE 'lab-u%'").fetchall():
                new = parsers.labcorp_name(old or '')
                if old and new != old:
                    n += db.execute("UPDATE labs SET analyte=? WHERE analyte=? AND edited=0 AND id LIKE 'lab-u%'",
                                    (new, old)).rowcount
            for (old,) in db.execute('SELECT DISTINCT analyte FROM labs WHERE edited=0').fetchall():
                if not old:
                    continue
                if parsers.JUNK_ANALYTE.search(old):
                    n += db.execute('DELETE FROM labs WHERE analyte=? AND edited=0', (old,)).rowcount
                    continue
                new = parsers.display_name(parsers.standard_name(old))
                if new != old:
                    n += db.execute('UPDATE labs SET analyte=? WHERE analyte=? AND edited=0', (new, old)).rowcount
            # names that differ only in capitals are one test: use the most common spelling
            spell = {}
            for name, cnt in db.execute('SELECT analyte, COUNT(*) FROM labs GROUP BY analyte').fetchall():
                if name:
                    spell.setdefault(name.lower(), []).append((cnt, name))
            for variants in spell.values():
                if len(variants) > 1:
                    canon = [v for v in variants if parsers.display_name(v[1]) == v[1]]
                    best = max(canon or variants)[1]   # the standard spelling if there is one
                    for _cnt, name in variants:
                        if name != best:
                            n += db.execute('UPDATE labs SET analyte=? WHERE analyte=? AND edited=0', (best, name)).rowcount
            keep = {}
            for rowid, analyte, date, value, docs, edited in db.execute(
                    'SELECT rowid, analyte, date, COALESCE(CAST(num AS TEXT), value), docs, edited FROM labs '
                    'ORDER BY edited DESC, rowid').fetchall():
                k = (analyte, date, value)
                if k not in keep:
                    keep[k] = (rowid, json.loads(docs or '[]'))
                    continue
                if edited:
                    continue
                kid, kdocs = keep[k]
                merged = list(dict.fromkeys(kdocs + json.loads(docs or '[]')))[:40]
                db.execute('UPDATE labs SET docs=? WHERE rowid=?', (json.dumps(merged), kid))
                keep[k] = (kid, merged)
                n += db.execute('DELETE FROM labs WHERE rowid=?', (rowid,)).rowcount
            db.commit()
        self.store.set_setting('labnames_version', LABNAMES_VERSION)
        return n

    def fix_places(self):
        """Summaries read by older versions saved a hospital department as the place ("Respiratory
        Therapy Hospital"), and gave a procedure the place of whichever summary listed it, even one
        about a visit years later. Puts the hospital's name on those visits and documents, and gives
        each imported procedure the place of a visit on its date (or of the summary's own visit if it
        happened then), or none. Entries a person edited are left alone."""
        from . import ccda
        if self.store.get_setting('places_version') == PLACES_VERSION:
            return 0
        n, db, visits = 0, self.store.db, {}
        for d in self.store.documents():
            if d.get('ext') not in ('.xml', '.zip') or d.get('status') != 'done':
                continue
            try:
                with open(os.path.join(self.files_dir, d['id'] + d['ext']), 'rb') as f:
                    found = ccda.documents_in(f.read(), d.get('orig_path') or '')
            except (OSError, ccda.NotCCDA):
                continue
            for _name, xml in found:
                try:
                    doc = ccda.parse(xml)
                except ccda.NotCCDA:
                    continue
                v = doc['visit']
                pid = hashlib.sha256(xml + b'#page').hexdigest()
                if not v:
                    continue
                visits[pid] = v
                if v.get('department'):
                    new = ccda.place(v.get('location'), v.get('facility'))
                    with self.store.lock:
                        n += db.execute("UPDATE encounters SET facility=? WHERE id LIKE 'ccda-%' AND edited=0 AND facility=? "
                                        "AND docs LIKE ?", (new, v['department'], f'%{pid}%')).rowcount
                        for did in (pid, d['id']):   # the readable page, and the uploaded file itself
                            n += db.execute("UPDATE documents SET facility=? WHERE id=? AND person_edited=0 AND facility=?",
                                            (doc['facility'], did, v['department'])).rowcount
                        db.commit()
        saved = [e for e in self.store.list('encounters') if e.get('date') and e.get('facility')]
        for p in self.store.list('procedures'):
            if not (p['id'] or '').startswith('ccda-') or p.get('edited') or len(p.get('date') or '') < 10:
                continue   # undated history items: nothing to check them against
            there = {e['facility'] for e in saved if ccda.during(p['date'], {'date': e['date'], 'end_date': e.get('end_date')})
                     and p['date'][:10] >= e['date'][:10]}
            cur = p.get('facility')
            if not there or cur in there:
                continue   # no visit then to check against, or it matches one
            within = [t for t in there if cur and cur.lower() in t.lower()]
            if not cur:
                if len(there) > 1:
                    continue
                fix = there.pop()                     # no place yet: the one visit's place
            elif len(within) == 1:
                fix = within[0]                       # "Example Health Network" -> "Riverside Clinic / Example Health Network"
            elif ccda.HOSPITAL.search(cur):
                fix = there.pop() if len(there) == 1 else None   # a hospital no visit that day was at
            else:
                continue                              # can't tell: leave it
            if fix != cur:
                with self.store.lock:
                    n += db.execute('UPDATE procedures SET facility=? WHERE id=? AND edited=0', (fix, p['id'])).rowcount
                    db.commit()
        self.store.set_setting('places_version', PLACES_VERSION)
        return n

    def backfill_vaccines(self):
        """Read the vaccines out of health summaries that were loaded before vaccines were kept.
        Only vaccine entries are added; nothing else is touched."""
        from . import ccda, fhir
        if self.store.get_setting('vaccines_version') == VACCINES_VERSION:
            return 0
        imms = {}
        for d in self.store.documents():
            if d.get('ext') not in ('.xml', '.zip') or d.get('status') != 'done':
                continue
            try:
                with open(os.path.join(self.files_dir, d['id'] + d['ext']), 'rb') as f:
                    data = f.read()
                found = ccda.documents_in(data, d.get('orig_path') or '')
            except (OSError, ccda.NotCCDA):
                continue
            for _name, xml in found:
                try:
                    doc = ccda.parse(xml)
                except ccda.NotCCDA:
                    continue
                pid = hashlib.sha256(xml + b'#page').hexdigest()
                for i in doc['immunizations']:
                    collect_vaccine(imms, i, pid if self.store.document(pid) else d['id'])
            for _name, js in ccda.zip_extras(data)['json']:
                try:
                    f = fhir.parse(js)
                except fhir.NotFHIR:
                    continue
                pid = hashlib.sha256(js + b'#page').hexdigest()
                for i in f.get('immunizations') or []:
                    collect_vaccine(imms, i, pid if self.store.document(pid) else d['id'])
        links = []
        rows = vaccine_rows(self.store, imms, links)
        for kind, rid, did in links:
            self.store.link_doc(kind, rid, did)
        n = self.store.upsert_imported('immunizations', rows)['added'] if rows else 0
        self.store.set_setting('vaccines_version', VACCINES_VERSION)
        return n

    def backfill_codes(self):
        """Add the medical codes (ICD-10, SNOMED CT, CPT, ...) of problems and procedures in health
        summaries loaded before codes were kept. Only the codes field of entries already on the
        dashboard changes. A condition gets the codes of every problem the dashboard counts as the
        same condition (store.condition_key: "HTN" and "Essential hypertension" are one); a procedure
        gets the codes of a summary procedure with its name on its date, or else with a related name
        (store.procedures_related) on the same day, give or take one. Returns how many entries got codes."""
        from . import ccda, fhir
        from .store import procedures_related
        if self.store.get_setting('codes_version') == CODES_VERSION:
            return 0
        parsed = []
        for d in self.store.documents():
            if d.get('ext') not in ('.xml', '.zip') or d.get('status') != 'done':
                continue
            try:
                with open(os.path.join(self.files_dir, d['id'] + d['ext']), 'rb') as f:
                    data = f.read()
                found = ccda.documents_in(data, d.get('orig_path') or '')
            except (OSError, ccda.NotCCDA):
                continue
            for _name, xml in found:
                try:
                    parsed.append(ccda.parse(xml))
                except ccda.NotCCDA:
                    continue
            for _name, js in ccda.zip_extras(data)['json']:
                try:
                    parsed.append(fhir.parse(js))
                except fhir.NotFHIR:
                    continue
        key = self.store.condition_key()
        conds, procs = {}, {}   # condition key -> codes; (name, date) -> codes
        for doc in parsed:
            for p in doc['problems']:
                if p.get('codes'):
                    conds[key(p['name'])] = codes.merge(conds.get(key(p['name'])), p['codes'])
            for p in doc['procedures']:
                if p.get('codes'):
                    k = ((p['name'] or '').lower(), p.get('date'))
                    procs[k] = codes.merge(procs.get(k), p['codes'])
        n = 0
        for c in self.store.list('conditions'):
            n += self.store.add_codes('conditions', c['id'], conds.get(key(c['name'])))
        for p in self.store.list('procedures'):
            text = procs.get(((p['name'] or '').lower(), p.get('date')))
            if not text and p.get('date'):
                text = codes.merge(*[t for (name, date), t in procs.items()
                                     if date and _days_apart(date, p['date']) <= 1 and procedures_related(name, p['name'])])
            n += self.store.add_codes('procedures', p['id'], text)
        self.store.set_setting('codes_version', CODES_VERSION)
        return n

    def process(self, sha, reuse_text=False):
        """Read one stored file: a health summary export is imported, anything else is read for text,
        sorted and dated, with lab results taken from it."""
        d = self.store.document(sha)
        if not d:
            return
        if d.get('ext') == '.zip' and not is_export(os.path.join(self.files_dir, sha + '.zip')):
            return self.unpack_bundle(sha, d)
        if d.get('ext') in ('.zip', '.xml'):
            return self.import_summary(sha, d)
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
            status='processing', needs_review=bool(questions), review_note='\n'.join(questions) or None,
            duplicate_of=dup['id'] if dup else None, classified=classify.VERSION)
        if d.get('ext') == '.html' and not reuse_text:
            # a page saved before it finished loading is replaced by the fuller save
            new = classify.shingles(text)
            for oid, otext in self.store.plugin_pages(sha):
                old = classify.shingles(otext)
                if len(old) >= 10 and len(new) > len(old) and len(old & new) / len(old) >= 0.9:
                    self.delete(oid)
        if dup and dup['how'] == 'copy':
            self.store.set_upload_result(sha, status='done')
            return r  # nothing new to add from a copy
        new_labs = [row for row in r['labs'] if not self.store.lab_exists(row)]
        if new_labs:
            rows = [dict(row, id=f'lab-u{sha[:10]}-{i}', docs=[sha]) for i, row in enumerate(new_labs)]
            self.store.upsert_imported('labs', rows)
        self.store.set_upload_result(sha, status='done')   # last, once its entries are in
        return r

    def unpack_bundle(self, sha, d):
        """A zip of loose records (PDFs, scans, health summaries), not a portal export: every file in
        it, in any folder, is added as if uploaded on its own, and read and sorted the same way. The
        zip stays listed as one document that says what was in it."""
        import zipfile
        added = dup = 0
        skipped, total = [], 0
        with zipfile.ZipFile(os.path.join(self.files_dir, sha + '.zip')) as z:
            members = z.infolist()
            if len(members) > BUNDLE_MAX_FILES:
                raise RuntimeError(f'this zip holds {len(members)} files; split it into zips of up to {BUNDLE_MAX_FILES}')
            for i in members:
                base = i.filename.rsplit('/', 1)[-1]
                if i.is_dir() or not base or base.startswith('.') or '__MACOSX/' in i.filename:
                    continue   # folders, and the hidden files Macs and Windows put in zips
                ext = os.path.splitext(base)[1].lower()
                if ext not in TYPES or ext == '.html' or i.file_size > BUNDLE_MAX_FILE:
                    skipped.append(i.filename)
                    continue
                total += i.file_size   # what the zip says; reading stops at that size, so it can't lie bigger
                if total > BUNDLE_MAX_TOTAL:
                    skipped.append(i.filename + ' (the zip unpacks too big; add the rest separately)')
                    continue
                try:
                    r = self.add(base, z.read(i), source='bundle')
                except ValidationError:
                    skipped.append(i.filename)
                    continue
                dup += r['duplicate']
                added += not r['duplicate']
        summary = (f"A zip of {added + dup + len(skipped)} files: {added} added to be read and sorted one by one"
                   + (f", {dup} already in the records" if dup else '')
                   + (f", {len(skipped)} left out (not a PDF, picture or health summary): "
                      + ', '.join(os.path.basename(x) for x in skipped[:12])
                      + (f' and {len(skipped) - 12} more' if len(skipped) > 12 else '') if skipped else '') + '.')
        self.store.set_upload_result(sha, title=f"Zip of records ({d.get('orig_path') or 'upload'})", category='other',
                                     status='done', needs_review=False, review_note=None, text=summary,
                                     summary=summary, classified=classify.VERSION)
        return {'added': added, 'duplicates': dup, 'skipped': skipped}

    def import_summary(self, sha, d):
        """A C-CDA health summary export: a readable page per document, plus its visits, results,
        vital signs, medications, problems and allergies. Re-importing the same or a newer export
        updates instead of doubling; results already recorded from other documents are skipped."""
        from . import ccda, fhir, parsers
        from .extract import html_text
        with open(os.path.join(self.files_dir, sha + d['ext']), 'rb') as f:
            data = f.read()
        found, bad = [], 0
        for name, xml in ccda.documents_in(data, d.get('orig_path') or ''):
            try:
                # the readable page gets its own id (an uploaded .xml file already uses the file's)
                found.append((hashlib.sha256(xml + b'#page').hexdigest(), name, ccda.parse(xml)))
            except ccda.NotCCDA:
                bad += 1
        extras = ccda.zip_extras(data)
        bundles = []
        for name, js in extras['json']:
            try:
                bundles.append((name, js, fhir.parse(js)))
            except fhir.NotFHIR:
                bad += 1
        if not found and not bundles:
            raise RuntimeError("no health summary documents could be read in this file")
        when = lambda x: ((x[2]['visit'] or {}).get('date') or x[2]['doc_date'] or '')
        found.sort(key=when)
        label = os.path.splitext(d.get('orig_path') or 'export')[0]
        invoices, queued = [], 0
        docs, enc, labs, meds, probs, alls, procs, links = [], [], [], {}, {}, {}, {}, []
        imms = {}
        HOSPITAL = re.compile(r'inpatient|emergency|observation|admission|hospital', re.I)
        for xsha, name, doc in found:
            v = doc['visit']
            page = ccda.page(doc)
            with open(os.path.join(self.files_dir, xsha + '.html'), 'w', encoding='utf-8') as f:
                f.write(page)
            stay = bool(v) and (HOSPITAL.search(v.get('kind') or '') or
                                (v.get('end_date') and v.get('date') and v['end_date'][:10] > v['date'][:10]))
            title = ('Hospital stay' if stay else f"{v['kind'] or 'Office'} visit".replace('Office Visit visit', 'Office visit')) \
                if v else doc['title']
            docs.append({'id': xsha, 'path': f"{IMPORTED_DIR}/{label}/{name.rsplit('/', 1)[-1]}", 'orig_path': name,
                         'title': title, 'doc_date': when((xsha, name, doc)) or None,
                         'category': ('hospital' if stay else 'office') if v else 'other', 'facility': doc['facility'], 'ext': '.html',
                         'size': len(page), 'text': html_text(page), 'needs_review': False, 'status': 'done',
                         'summary': f"From the health summary export {d.get('orig_path')}."})
            same = self.store.same_day('encounters', v['date']) if v and v.get('date') else []
            place = ccda.place(v.get('location'), v.get('facility')) if v else ''
            twin = None
            if v and v.get('date'):
                for e in enc + [x for x in self.store.list('encounters') if (x['id'] or '').startswith('ccda-')]:
                    if (e.get('date') or '')[:10] == v['date'][:10] and (e.get('type') or '').lower() == (v['kind'] or 'office visit').lower() \
                            and (e.get('facility') or '') == place:
                        twin = e
                        break
            if same:   # already on the dashboard from other records: link this document to it
                links.append(('encounters', same[0]['id'], xsha))
            elif twin:  # the same visit in another file of this or an earlier export
                if twin in enc:
                    twin['docs'] = twin['docs'] + [xsha]
                    twin['provider'] = twin.get('provider') or v.get('provider') or None
                else:
                    links.append(('encounters', twin['id'], xsha))
            elif v and v.get('date'):
                enc.append({'id': ccda.record_ids('visit', v.get('id') or xsha), 'docs': [xsha], 'date': v['date'],
                            'end_date': v.get('end_date') if v.get('end_date') != v['date'] else None,
                            'type': v['kind'] or 'Office visit', 'facility': place,
                            'provider': v.get('provider') or None, 'reason': v.get('reason'),
                            'summary': ('Diagnoses: ' + v['diagnoses']) if v.get('diagnoses') else None})
            for r in doc['labs']:
                labs.append(dict(r, docs=[xsha], facility=doc['facility']))
            listed, given = set(), []
            for m in doc['medications']:
                if stay and m.get('start_date') and v.get('date') and \
                        v['date'][:10] <= m['start_date'][:10] <= (v.get('end_date') or v['date'])[:10]:
                    given.append(m['name'])   # given in the hospital during this stay
                    continue
                k = m['name'].lower()
                listed.add(k)
                prev = meds.get(k)
                meds[k] = dict(m, docs=[xsha], first=(prev or {}).get('first') or when((xsha, name, doc)),
                               last_seen=when((xsha, name, doc)), gone=None)
            if v and listed:  # a medication that drops off a visit's (non-empty) list was stopped by then
                for k, m in meds.items():
                    if k not in listed and not m.get('gone') and not m.get('end_date') and m.get('status') == 'active':
                        m['gone'] = when((xsha, name, doc))
            if given:
                names = list(dict.fromkeys(ccda.drug_key(g).title() for g in given))
                note = 'Medicines given during the stay: ' + ', '.join(names[:25]) + ('…' if len(names) > 25 else '') + '.'
                docs[-1]['summary'] += ' ' + note
                if enc and enc[-1]['docs'] == [xsha]:
                    enc[-1]['summary'] = ((enc[-1].get('summary') or '') + ' ' + note).strip()
            for p in doc['problems']:
                probs[p['name'].lower()] = dict(p, docs=[xsha])
            for a in doc['allergies']:
                alls.setdefault(a['substance'].lower(), dict(a, docs=[xsha]))
            for i in doc['immunizations']:
                collect_vaccine(imms, i, xsha)
            for p in doc['procedures']:
                # a summary lists past procedures too: its place only fits ones done during its visit
                fac = doc['facility'] if ccda.during(p.get('date'), v) else None
                k = (p['name'].lower(), p.get('date'))
                if k not in procs:
                    procs[k] = dict(p, docs=[xsha], facility=fac)
                elif fac and not procs[k].get('facility'):
                    procs[k]['facility'] = fac
        # a FHIR bundle (FollowMyHealth's AllPatientData.json): a summary page and a messages page
        for name, js, f in bundles:
            def page_doc(pid, title, page, category='other', date=None):
                with open(os.path.join(self.files_dir, pid + '.html'), 'w', encoding='utf-8') as fh:
                    fh.write(page)
                docs.append({'id': pid, 'path': f"{IMPORTED_DIR}/{label}/{title}", 'orig_path': name, 'title': title,
                             'doc_date': date, 'category': category, 'ext': '.html', 'size': len(page),
                             'text': html_text(page), 'needs_review': False, 'status': 'done',
                             'summary': f"From the export {d.get('orig_path')}."})
                return pid
            last = max([v['date'] for v in f['visits']] + [m['date'] or '' for m in f['messages']] or [''])
            spid = page_doc(hashlib.sha256(js + b'#page').hexdigest(), 'Patient portal record (all data)',
                            fhir.summary_page(f, 'Patient portal record (all data)'), date=last or None)
            if f['messages']:
                page_doc(hashlib.sha256(js + b'#messages').hexdigest(), 'Portal messages',
                         fhir.messages_page(f['messages']), date=max(m['date'] or '' for m in f['messages']) or None)
            for v in f['visits']:
                same = self.store.same_day('encounters', v['date'], days=0)
                if same:
                    links.append(('encounters', same[0]['id'], spid))
                else:
                    enc.append({'id': ccda.record_ids('fvisit', v['date'], v['kind']), 'docs': [spid], 'date': v['date'],
                                'type': v['kind'], 'provider': v['provider'], 'reason': v['reason']})
            for r in f['labs']:
                labs.append(dict(r, docs=[spid]))
            for m in f['medications']:
                k = (m['name'].lower(), m['start_date'])
                if k not in {(x['name'].lower(), x.get('start_date')) for x in meds.values()}:
                    meds[f"fhir|{m['name'].lower()}|{m['start_date']}"] = dict(m, docs=[spid], first=m['start_date'],
                                                                               last_seen=m['start_date'], gone=None, fixed=True)
            for p in f['problems']:
                probs.setdefault(p['name'].lower(), dict(p, docs=[spid]))
            for a in f['allergies']:
                alls.setdefault(a['substance'].lower(), dict(a, docs=[spid]))
            for i in f.get('immunizations') or []:
                collect_vaccine(imms, i, spid)
            for p in f['procedures']:
                procs.setdefault((p['name'].lower(), p.get('date')), dict(p, docs=[spid], facility=None))
            # scans: named by what the export says they are, with their date, then read like any upload
            kinds = {'slab': 'Lab report scan', 'sekg': 'EKG', 'srad': 'Imaging report scan'}
            for sname, blob in extras['scans']:
                meta = f['files'].get(sname.rsplit('/', 1)[-1], {})
                nice = ' '.join(x for x in (kinds.get((meta.get('type') or '').lower(), meta.get('type') or 'Scan'),
                                            meta.get('date')) if x)
                ext = os.path.splitext(sname)[1].lower()
                try:
                    self.add(f'{nice}{ext}', blob, source='export')
                    queued += 1
                except ValidationError:
                    extras['skipped'].append(sname)
            for hname, blob in extras['html']:
                try:
                    self.add(os.path.basename(hname), blob, source='export')
                    queued += 1
                except ValidationError:
                    extras['skipped'].append(hname)
        if not found and not bundles:
            raise RuntimeError("no health summary documents could be read in this file")
        self.store.upsert_documents(docs)
        for x in docs:
            self.store.set_has_file(x['id'])
        # results: one name per test; skip values already recorded from other documents
        lab_rows = []
        known = {a.lower(): a for a in self.store.lab_names()}
        from .checks import learned_lab_names
        learned = learned_lab_names(self.store)   # "same" answers on the Review tab
        for r in parsers.normalize(labs):
            r['analyte'] = parsers.display_name(r['analyte'])
            r['analyte'] = learned.get(r['analyte'].lower(), r['analyte'])
            r['analyte'] = known.get(r['analyte'].lower(), r['analyte'])
            r['id'] = ccda.record_ids('lab', r['analyte'], r['date'], r['value'])
            if self.store.get('labs', r['id']) or not self.store.lab_exists(r):
                lab_rows.append(r)
        med_rows = []
        for m in meds.values():
            end, status, notes = m.get('end_date'), 'active', None
            if end or m.get('status') in ('aborted', 'completed', 'cancelled', 'stopped'):
                status = 'stopped'
            if m.get('gone') and not end:
                status, end = 'stopped', m['gone']
                notes = f"Not on the medication list at the {m['gone']} visit (last listed {m['last_seen']})."
            name = m['name']
            if re.match(r'\s*unable to find\b', name, re.I):   # the clinic's system had no name for it
                name = 'Unnamed medicine (not in the clinic\'s drug list)'
            med_rows.append({'id': ccda.record_ids('med', m['name'], m.get('start_date')), 'docs': m['docs'],
                             'name': name, 'dose': m.get('dose'), 'schedule': m.get('schedule'),
                             'start_date': m.get('start_date') or m['first'], 'end_date': end, 'status': status,
                             'notes': notes or ('Start date is when it first appears in this export.' if not m.get('start_date') else None)})
        # problems and allergies: an earlier onset date or a known reaction is never lost to a later file
        existing_c = {c['id']: c for c in self.store.list('conditions')}
        have_cond = {c['name'].lower() for c in existing_c.values() if not (c['id'] or '').startswith('ccda-')}
        cond_rows = []
        code_links = []   # (kind, name or id, codes) for entries already on the dashboard
        for p in probs.values():
            if p['name'].lower() in have_cond:
                code_links.append(('conditions', p['name'].lower(), p.get('codes')))
                continue
            rid = ccda.record_ids('cond', p['name'])
            old = existing_c.get(rid) or {}
            dates = [x for x in (p.get('first_noted'), old.get('first_noted')) if x]
            cond_rows.append({'id': rid, 'docs': list(dict.fromkeys((old.get('docs') or []) + p['docs']))[:40],
                              'name': p['name'], 'status': p['status'], 'first_noted': min(dates) if dates else None,
                              'codes': codes.merge(old.get('codes'), p.get('codes'))})
        existing_a = {a['id']: a for a in self.store.list('allergies')}
        vague = re.compile(r'^\s*(unknown|unk|not known|none|n/?a|other|see (note|comment)s?|pt cannot remember|patient (can ?not|does not) (remember|recall)|severe|moderate|mild)\s*\.?\s*$', re.I)

        def reaction(new, old):
            if not new or vague.match(new):
                return old or new
            if not old or vague.match(old) or new.lower() in old.lower():
                return new if not old or vague.match(old) else old
            return f'{old}; {new}' if old.lower() not in new.lower() else new

        def first_word(x):
            w = re.findall(r'[a-z]{5,}', (x or '').lower())
            return w[0] if w else (x or '').lower()
        all_rows = []
        for a in alls.values():
            rid = ccda.record_ids('allergy', a['substance'])
            twin = next((x for x in existing_a.values() if x['id'] != rid and first_word(x['substance']) == first_word(a['substance'])), None)
            if twin:   # the same allergy under another wording: link the document
                links.extend(('allergies', twin['id'], d) for d in a['docs'])
                continue
            old = existing_a.get(rid) or {}
            all_rows.append({'id': rid, 'docs': list(dict.fromkeys((old.get('docs') or []) + a['docs']))[:40],
                             'substance': a['substance'], 'reaction': reaction(a.get('reaction'), old.get('reaction'))})
            existing_a[rid] = all_rows[-1]
        proc_rows = []
        old_procs = self.store.list('procedures')
        have_proc = {(x['name'] or '').lower() for x in old_procs}
        old_place = {x['id']: x['facility'] for x in old_procs if x.get('facility')}
        for p in procs.values():
            if not p.get('date') and any(h.startswith(p['name'].lower()[:10]) for h in have_proc):
                continue   # an undated history item already listed
            same = self.store.same_day('procedures', p['date'], days=3) if p.get('date') else []
            if same:
                links.append(('procedures', same[0]['id'], p['docs'][0]))
                code_links.append(('procedures', same[0]['id'], p.get('codes')))
                continue
            pid = ccda.record_ids('proc', p['name'], p.get('date'))
            proc_rows.append({'id': pid, 'docs': p['docs'],
                              'date': p.get('date'), 'name': p['name'],
                              'facility': p.get('facility') or visit_place(p.get('date'), enc, self.store) or old_place.get(pid),
                              'provider': p.get('provider'), 'codes': p.get('codes')})
        for kind, rid, did in links:
            self.store.link_doc(kind, rid, did)
        by_name = {(c['name'] or '').lower(): c['id'] for c in existing_c.values()}
        for kind, key, text in code_links:
            self.store.add_codes(kind, by_name.get(key, key) if kind == 'conditions' else key, text)
        counts = {}
        vax_rows = vaccine_rows(self.store, imms, links)
        for kind, rows in (('encounters', enc), ('labs', lab_rows), ('medications', med_rows),
                           ('conditions', cond_rows), ('allergies', all_rows), ('procedures', proc_rows),
                           ('immunizations', vax_rows)):
            if rows:
                counts[kind] = self.store.upsert_imported(kind, rows)
        merged = self.store.consolidate_meds(ccda.drug_key, ccda.record_ids)
        self.store.tidy_imported_places(ccda.place)
        self.store.tidy_places()
        self.store.merge_imported_procedures()
        imaging_from_summaries(self.store)
        self.store.merge_imported_procedures('tests')
        self.store.merge_med_sources()
        self.store.merge_conditions()
        counts['providers'] = refresh_doctors(self.store)
        added = {k: v['added'] + v['updated'] for k, v in counts.items()}
        added['medications'] = merged['entries']
        span = f"{when(found[0])} to {when(found[-1])}" if found else 'patient portal data'
        summary = (f"Imported {len(docs)} documents ({span}): {added.get('encounters', 0)} visits, "
                   f"{added.get('labs', 0)} results and vital signs, medications now {added.get('medications', 0)} entries (one per drug per period of use), "
                   f"{added.get('conditions', 0)} problems, {added.get('allergies', 0)} allergies, "
                   f"{added.get('procedures', 0)} procedures, {added.get('immunizations', 0)} vaccines."
                   + (f" Linked to {len(links)} entr{'y' if len(links) == 1 else 'ies'} already on the dashboard." if links else '')
                   + (f" {queued} scans and other documents added to be read separately." if queued else '')
                   + (f" {bad} file(s) could not be read." if bad else '')
                   + (f" Left out: {', '.join(os.path.basename(x) for x in extras['skipped'][:12])}"
                      + (f" and {len(extras['skipped']) - 12} more" if len(extras['skipped']) > 12 else '') + '.'
                      if extras['skipped'] else ''))
        self.store.set_upload_result(sha, title=f"Health summary export ({label})", category='other',
                                     doc_date=(when(found[-1]) if found else None) or None,
                                     facility=found[-1][2]['facility'] if found else None,
                                     status='done', needs_review=False, review_note=None, text=summary,
                                     summary=summary, classified=classify.VERSION)
        return {'questions': [], 'labs': [], 'money': None}

    def doctors(self):
        return refresh_doctors(self.store, write=False)

    def delete(self, sha):
        d = self.store.delete_document(sha)
        if not d:
            return False
        for f in os.listdir(self.files_dir):
            if f.split('.')[0] == sha:
                os.remove(os.path.join(self.files_dir, f))
        return True

    def uploaded(self):
        return [d for d in self.store.documents() if (d.get('path') or '').startswith(UPLOAD_DIR + '/')]


MEDS_VERSION = '1'        # bump when drug names are matched differently
CONDITIONS_VERSION = '2'  # bump when condition synonyms change
LABNAMES_VERSION = '4'  # bump when lab names are standardized differently
PLACES_VERSION = '5'     # bump when places are read differently, so saved ones are fixed again


BUNDLE_MAX_FILE = 200 * 1024 * 1024   # the same limit as one upload
BUNDLE_MAX_FILES = 5000                # files in one zip of records
BUNDLE_MAX_TOTAL = 4 * 1024 ** 3       # unpacked size of one zip of records (guards against zip bombs)


def _days_apart(a, b):
    """Days between two dates; a year or month alone counts from its first day. 999 if unreadable."""
    from datetime import date
    def d(v):
        parts = (v or '').split('-') + ['01', '01']
        try:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            return None
    x, y = d(a), d(b)
    return abs((x - y).days) if x and y else 999


def is_export(path):
    """Is this zip a patient portal export (health summaries: .xml documents or a FHIR .json
    bundle)? Otherwise it is a zip of loose records, read file by file."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            names = [i.filename.lower() for i in z.infolist() if not i.is_dir()]
    except (OSError, zipfile.BadZipFile):
        return True   # let the export reader explain what's wrong with it
    names = [n for n in names if '__macosx/' not in n]
    if any(n.endswith('.json') or 'ihe_xdm/' in n or n.endswith('metadata.xml') for n in names):
        return True    # a FHIR bundle, or the standard (IHE XDM) layout portals export in
    loose = any(n.endswith(('.pdf', '.jpg', '.jpeg', '.png', '.tif', '.tiff')) for n in names)
    return any(n.endswith('.xml') for n in names) and not loose


def visit_place(date, enc, store, saved=None):
    """The place of a visit on that day (new visits of this import first, then saved ones)."""
    if not date or len(date) < 10:
        return None
    for e in list(enc) + (saved if saved is not None else store.list('encounters')):
        if (e.get('date') or '')[:10] == date[:10] and e.get('facility'):
            return e['facility']
    return None


CODES_VERSION = '1'      # bump when medical codes are read differently, so summaries are read again
VACCINES_VERSION = '1'   # bump when vaccines are read differently, so summaries are read again


def collect_vaccine(imms, i, did):
    """One shot per vaccine per day, however many documents list it; details are filled in from
    whichever document has them."""
    key = (i['vaccine'].lower(), i['date'][:10])
    cur = imms.get(key)
    if not cur:
        imms[key] = dict(i, date=i['date'][:10], docs=[did])
        return
    if did not in cur['docs']:
        cur['docs'].append(did)
    for f in ('lot', 'manufacturer', 'provider', 'facility'):
        cur[f] = cur.get(f) or i.get(f)
    if len(i.get('product') or '') > len(cur.get('product') or ''):
        cur['product'] = i['product']


def vaccine_rows(store, imms, links):
    """Rows to import; a shot already entered by hand on the same day only gets the documents linked."""
    from . import ccda
    old = {r['id']: r for r in store.list('immunizations')}
    by_hand = {(r['date'] or '')[:10] + '|' + (r['name'] or '').lower(): r['id'] for r in old.values()
               if not (r['id'] or '').startswith('ccda-')}
    # the same shot recorded by two systems a day apart, one without a lot number: keep one
    import datetime
    for key in sorted(imms, key=lambda k: k[1]):
        i = imms.get(key)
        if not i or i.get('lot'):
            continue
        day = datetime.date.fromisoformat(key[1])
        for k2 in [(key[0], str(day + datetime.timedelta(days=n))) for n in (-1, 1)]:
            j = imms.get(k2)
            if j and j.get('lot'):
                j['docs'] = list(dict.fromkeys(j['docs'] + i['docs']))
                del imms[key]
                break
    rows = []
    for (group, day), i in sorted(imms.items(), key=lambda x: x[0][1]):
        name = f"{i['vaccine']} vaccine"
        twin = by_hand.get(day + '|' + name.lower())
        if twin:
            links.extend(('immunizations', twin, d) for d in i['docs'])
            continue
        rid = ccda.record_ids('imm', group, day)
        prev = old.get(rid) or {}
        rows.append({'id': rid, 'docs': list(dict.fromkeys((prev.get('docs') or []) + i['docs']))[:40],
                     'date': day, 'name': name, 'product': i.get('product'), 'lot': i.get('lot') or prev.get('lot'),
                     'manufacturer': i.get('manufacturer') or prev.get('manufacturer'),
                     'facility': i.get('facility') or prev.get('facility'),
                     'provider': i.get('provider') or prev.get('provider')})
    return rows


def refresh_doctors(store, write=True):
    """One Doctors entry per person named on visits and procedures (credentials, middle initials
    and notes like "(cardiology)" don't make a second one). A person already entered by hand keeps
    that entry, which gets the documents that name them. Entries made this way for a name no longer
    on any visit are removed, unless someone edited them. With write=False, only returns the rows."""
    from . import ccda
    from collections import Counter
    mine = {}
    for p in store.list('providers'):
        if not (p['id'] or '').startswith('ccda-') or p.get('edited'):
            k = ccda.doctor_key(p['name'])
            if k:
                mine.setdefault(k, p)
    seen, theirs = {}, {}
    for kind in ('encounters', 'procedures'):
        for r in store.list(kind):
            for person in ccda.people(r.get('provider')):
                key = ccda.doctor_key(person)
                if not key:
                    continue
                if key in mine:
                    theirs.setdefault(key, [])
                    theirs[key] += [x for x in r.get('docs') or [] if x not in theirs[key]]
                    continue
                d = seen.setdefault(key, {'names': Counter(), 'places': Counter(), 'dates': [], 'docs': [], 'visits': 0})
                d['names'][person] += 1
                if r.get('facility'):
                    d['places'][ccda.place(*r['facility'].split(' / '))] += 1
                if r.get('date'):
                    d['dates'].append(r['date'][:10])
                d['visits'] += kind == 'encounters'
                d['docs'] += [x for x in r.get('docs') or [] if x not in d['docs']]
    rows = []
    for key, d in seen.items():
        # the fullest spelling, e.g. with the middle initial and "MD"
        name = max(d['names'], key=lambda n: (len(n.split()), d['names'][n], len(n)))
        span = (f"{min(d['dates'])} to {max(d['dates'])}" if len(set(d['dates'])) > 1 else d['dates'][0]) if d['dates'] else ''
        seen_at = f"{d['visits']} visit{'s' if d['visits'] != 1 else ''}" if d['visits'] else 'procedures'
        rows.append({'id': ccda.record_ids('doctor', key), 'name': name,
                     'facility': d['places'].most_common(1)[0][0] if d['places'] else None,
                     'notes': f"From the health summaries: {seen_at}{', ' + span if span else ''}."
                              + (f" Also written as {', '.join(n for n in d['names'] if n != name)}." if len(d['names']) > 1 else ''),
                     'docs': d['docs'][:40]})
    if not write:
        return rows
    keep = {r['id'] for r in rows}
    for p in store.list('providers'):
        if (p['id'] or '').startswith('ccda-doctor-') and not p.get('edited') and p['id'] not in keep:
            with store.lock:   # a name that is now someone's hand-made entry, or no longer on any visit
                store.db.execute('DELETE FROM providers WHERE id=?', (p['id'],))
                store.db.commit()
    for key, docs in theirs.items():
        have = len(mine[key]['docs'])
        for x in docs[:max(0, 40 - have)]:
            store.link_doc('providers', mine[key]['id'], x)
    return store.upsert_imported('providers', rows)


def imaging_from_summaries(store):
    """Imaging reports written out in the "Radiology Results" section of imported health
    summaries become Tests & imaging entries (one per study and date, linked to every summary
    that has it), unless the same study is already entered in detail that day."""
    from . import ccda
    rads = {}
    found = store.db.execute("SELECT id, facility, text FROM documents WHERE text LIKE '%Radiology Results%' "
                             "ORDER BY doc_date DESC").fetchall()
    for did, facility, text in found:
        d = {'id': did, 'facility': facility}
        for rp in ccda.radiology_reports(text):
            k = (rp['name'].lower(), rp['date'])
            if k in rads:
                if d['id'] not in rads[k]['docs']:
                    rads[k]['docs'].append(d['id'])
            else:
                rads[k] = dict(rp, docs=[d['id']], facility=d.get('facility'))
    rows = [{'id': ccda.record_ids('rad', r['name'], r['date']), 'docs': r['docs'][:40], 'date': r['date'],
             'category': 'imaging', 'name': r['name'], 'result': r['result'], 'provider': r['provider'],
             'facility': r['facility']} for r in rads.values()]
    out = store.upsert_imported('tests', rows) if rows else {'added': 0, 'updated': 0, 'skipped_edited': 0}
    store.merge_imported_procedures('tests')
    return out

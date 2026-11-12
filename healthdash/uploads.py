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
        for sha in store.placeholder_dates()[1]:          # read with 1/1/1900 taken as a real date
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
            for p in doc['procedures']:
                procs.setdefault((p['name'].lower(), p.get('date')), dict(p, docs=[xsha], facility=doc['facility']))
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
        for r in parsers.normalize(labs):
            if r['analyte'].isupper():
                r['analyte'] = r['analyte'].title()
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
        for p in probs.values():
            if p['name'].lower() in have_cond:
                continue
            rid = ccda.record_ids('cond', p['name'])
            old = existing_c.get(rid) or {}
            dates = [x for x in (p.get('first_noted'), old.get('first_noted')) if x]
            cond_rows.append({'id': rid, 'docs': list(dict.fromkeys((old.get('docs') or []) + p['docs']))[:40],
                              'name': p['name'], 'status': p['status'], 'first_noted': min(dates) if dates else None})
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
                continue
            pid = ccda.record_ids('proc', p['name'], p.get('date'))
            proc_rows.append({'id': pid, 'docs': p['docs'],
                              'date': p.get('date'), 'name': p['name'], 'facility': old_place.get(pid) or p.get('facility'),
                              'provider': p.get('provider')})
        for kind, rid, did in links:
            self.store.link_doc(kind, rid, did)
        counts = {}
        for kind, rows in (('encounters', enc), ('labs', lab_rows), ('medications', med_rows),
                           ('conditions', cond_rows), ('allergies', all_rows), ('procedures', proc_rows)):
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
                   f"{added.get('procedures', 0)} procedures."
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

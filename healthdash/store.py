"""SQLite storage for the health dashboard.

Two halves:
  documents  - every source file (PDF, scan, letter) with its extracted text, searchable (FTS5)
  records    - structured history, one table per kind (visits, tests, labs, meds, ...)

Every record lists the documents it came from (`docs`, a JSON list of document ids), so the
dashboard can open the original. Records loaded by the importer have stable string ids, so
re-importing updates them in place; a record edited on the dashboard is marked `edited` and
the importer leaves it alone from then on.
"""

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone

# kind -> (table, fields). Every table also gets: id, docs, needs_review, edited, origin, updated
KINDS = {
    'encounters': ['date', 'end_date', 'type', 'facility', 'provider', 'reason', 'summary'],
    'tests': ['date', 'category', 'name', 'body_part', 'facility', 'provider', 'result', 'abnormal',
              'encounter_id'],
    'labs': ['date', 'panel', 'analyte', 'value', 'num', 'unit', 'ref_range', 'flag', 'facility'],
    'procedures': ['date', 'name', 'facility', 'provider', 'notes', 'encounter_id'],
    'medications': ['name', 'dose', 'schedule', 'purpose', 'start_date', 'end_date', 'status',
                    'prescriber', 'notes'],
    'conditions': ['name', 'status', 'first_noted', 'notes'],
    'allergies': ['substance', 'reaction', 'severity'],
    'imaging': ['date', 'modality', 'description', 'facility', 'location', 'notes'],
    'providers': ['name', 'specialty', 'facility', 'phone', 'notes'],
}
NUMERIC = {'num'}
DELETED = '(deleted)'   # in the moved table: deleted on the dashboard, not moved to a person
DROPPED = {}  # removed columns, erased on open
DROPPED_TABLES = ['financial']  # bills and estimates are no longer kept
INTEGER = {'abnormal'}
DATE_FIELDS = {'date', 'end_date', 'start_date', 'first_noted'}
DOC_FIELDS = ['path', 'orig_path', 'title', 'doc_date', 'category', 'facility', 'pages', 'ocr_pages',
              'size', 'ext', 'summary', 'organized_path', 'status', 'review_note']
ADDED = {'documents': ['status TEXT', 'review_note TEXT', 'duplicate_of TEXT', 'classified TEXT',
                       'person_edited INTEGER DEFAULT 0']}  # columns added after the first release
DATE_RE = re.compile(r'^\d{4}(-\d{2}(-\d{2})?)?$')  # full date, or just year / year-month


def _squash(name):
    """A place name for comparing: lower case, single spaces."""
    return re.sub(r'\s+', ' ', str(name or '')).strip().lower()


class ValidationError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _schema():
    sql = ["""
    CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,            -- sha256 of the file
        path TEXT, orig_path TEXT, title TEXT, doc_date TEXT, category TEXT, facility TEXT,
        pages INTEGER, ocr_pages TEXT, size INTEGER, ext TEXT, summary TEXT, organized_path TEXT,
        text TEXT, has_file INTEGER DEFAULT 0, needs_review INTEGER DEFAULT 1,
        edited INTEGER DEFAULT 0, added TEXT, updated TEXT,
        status TEXT,                    -- 'processing' while an upload is being read, else done/error
        review_note TEXT,               -- questions for a person about this document
        duplicate_of TEXT,              -- an upload that looks like a copy of this document
        classified TEXT,                -- version of the sorting rules that last sorted this upload
        person_edited INTEGER DEFAULT 0 -- a person saved it; never re-sorted automatically
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
        title, facility, text, content='documents', content_rowid='rowid',
        tokenize='porter unicode61'
    );
    CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
        INSERT INTO documents_fts(rowid, title, facility, text)
        VALUES (new.rowid, new.title, new.facility, new.text);
    END;
    CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
        INSERT INTO documents_fts(documents_fts, rowid, title, facility, text)
        VALUES ('delete', old.rowid, old.title, old.facility, old.text);
    END;
    CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
        INSERT INTO documents_fts(documents_fts, rowid, title, facility, text)
        VALUES ('delete', old.rowid, old.title, old.facility, old.text);
        INSERT INTO documents_fts(rowid, title, facility, text)
        VALUES (new.rowid, new.title, new.facility, new.text);
    END;
    """]
    for kind, fields in KINDS.items():
        cols = []
        for f in fields:
            t = 'REAL' if f in NUMERIC else 'INTEGER' if f in INTEGER else 'TEXT'
            cols.append(f'{f} {t}')
        sql.append(f"""
        CREATE TABLE IF NOT EXISTS {kind} (
            id TEXT PRIMARY KEY, {', '.join(cols)},
            docs TEXT DEFAULT '[]', needs_review INTEGER DEFAULT 0, edited INTEGER DEFAULT 0,
            origin TEXT DEFAULT 'manual', updated TEXT
        );""")
    sql.append('CREATE INDEX IF NOT EXISTS labs_analyte ON labs(analyte, date);')
    return '\n'.join(sql)


class _Result:
    """A finished query: rows are fetched while the lock is held."""

    def __init__(self, cur):
        self.rows = cur.fetchall()
        self.rowcount, self.lastrowid = cur.rowcount, cur.lastrowid

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class _LockedDB:
    """One sqlite connection shared by the web threads and the upload worker. Every call holds
    the store's lock, so two threads never use the connection at the same time."""

    def __init__(self, conn, lock):
        object.__setattr__(self, '_conn', conn)
        object.__setattr__(self, '_lock', lock)

    def execute(self, sql, args=()):
        with self._lock:
            return _Result(self._conn.execute(sql, args))

    def executescript(self, sql):
        with self._lock:
            self._conn.executescript(sql)

    def commit(self):
        with self._lock:
            self._conn.commit()

    def __getattr__(self, name):  # backup, close
        return getattr(self._conn, name)

    def __setattr__(self, name, value):  # row_factory
        setattr(self._conn, name, value)


# words a medication line can start with that aren't a drug
NOT_DRUGS = {'misc', 'miscellaneous', 'med', 'meds', 'medication', 'patient', 'pill', 'unable', 'unlisted',
             'historical', 'other', 'anes', 'rdna', 'apap', 'otc', 'generic', 'unknown'}

# over-the-counter medicines and supplies: listed, but their start and stop dates aren't tracked
OTC_RE = re.compile(
    r'\b(acetaminophen|tylenol|advil|motrin|naproxen|aleve|loratadine|claritin|cetirizine|'
    r'zyrtec|fexofenadine|allegra|diphenhydramine|benadryl|docusate|colace|senna|sennosides|miralax|'
    r'polyethylene glycol|famotidine|pepcid|calcium carbonate|tums|simethicone|loperamide|imodium|bismuth|'
    r'dextromethorphan|delsym|guaifenesin|mucinex|melatonin|(multi-?|multi-day )?vitamins?|multivitamin|'
    r'cholecalciferol|cyanocobalamin|biotin|fish oil|omega-3|magnesium|ferrous|lactobacillus|acidophilus|'
    r'saccharomyces|florastor|probiotics?|lancets?|test strips?|in vitro strip|pill splitter|'
    r'misc(ellaneous)? equipment)\b', re.I)
# a prescription combination or strength, or given by injection or IV in the hospital: still tracked
NOT_OTC_RE = re.compile(r'butalbital|codone|codeine|tramadol|\bapap\b|\binj\b|injection|\biv\b|\banes\b|'
                        r'motrin\s*(400|600|800)\b|naproxen\s*(sodium\s*)?(250|275|375|500|550)\b', re.I)


# common short names for procedures, so "I and D" is known to be an incision and drainage
PROCEDURE_SHORT = [(r'\bi\s*(and|&)\s*d\b', 'incision drainage'), (r'\begd\b', 'esophagogastroduodenoscopy'),
                   (r'\btah\b', 'total abdominal hysterectomy'), (r'\brfa\b', 'radiofrequency ablation'),
                   (r'\bd\s*(and|&)\s*c\b', 'dilation curettage'), (r'\blap chole\b', 'laparoscopic cholecystectomy')]


def is_otc(name, extra=None):
    """True for an over-the-counter medicine or supply ("Tylenol CAPS", "Loratadine 10 MG"), not for a
    prescription combination or strength that contains one ("Oxycodone-Acetaminophen 5-325").
    extra: (more OTC words, more prescription patterns) as compiled patterns, from set_otc."""
    n = name or ''
    yes, no = extra or (None, None)
    if NOT_OTC_RE.search(n) or (no and no.search(n)):
        return False
    return bool(OTC_RE.search(n) or (yes and yes.search(n)))


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.db = _LockedDB(sqlite3.connect(path, check_same_thread=False), self.lock)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(_schema())
        self.db.commit()
        self._drop_old_columns()

    def _drop_old_columns(self):
        for table, cols in ADDED.items():
            have = {r[1] for r in self.db.execute(f'PRAGMA table_info({table})')}
            for c in cols:
                if c.split()[0] not in have:
                    self.db.execute(f'ALTER TABLE {table} ADD COLUMN {c}')
        self.db.commit()
        dropped = False
        for table in DROPPED_TABLES:
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                self.db.execute(f'DROP TABLE {table}')
                dropped = True
        if dropped:
            self.db.commit()
            self.db.execute('VACUUM')  # so the old values are really gone from the file
            dropped = False
        if sqlite3.sqlite_version_info < (3, 35):  # too old to drop columns; they're just ignored
            return
        for table, cols in DROPPED.items():
            have = {r[1] for r in self.db.execute(f'PRAGMA table_info({table})')}
            for c in cols:
                if c in have:
                    self.db.execute(f'ALTER TABLE {table} DROP COLUMN {c}')
                    dropped = True
        if dropped:
            self.db.commit()
            self.db.execute('VACUUM')  # so the old values are really gone from the file

    # ---- helpers ----------------------------------------------------------------------

    def _rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    @staticmethod
    def _clean_record(kind, data, partial=False):
        if kind not in KINDS:
            raise ValidationError(f'unknown kind: {kind}')
        out = {}
        for f in KINDS[kind]:
            if f not in data:
                if not partial:
                    out[f] = None
                continue
            v = data[f]
            if v == '' or v is None:
                out[f] = None
            elif f in NUMERIC:
                try:
                    out[f] = float(str(v).replace('$', '').replace(',', ''))
                except ValueError:
                    raise ValidationError(f'{f} must be a number')
            elif f in INTEGER:
                out[f] = 1 if v in (1, True, '1', 'true', 'yes') else 0
            else:
                v = str(v).strip()
                if f in DATE_FIELDS and v and not DATE_RE.match(v):
                    raise ValidationError(f'{f} must look like YYYY-MM-DD (or YYYY-MM, YYYY)')
                out[f] = v
        if 'docs' in data:
            docs = data['docs'] or []
            if isinstance(docs, str):
                docs = [docs]
            out['docs'] = json.dumps([str(d) for d in docs])
        if 'needs_review' in data:
            out['needs_review'] = 1 if data['needs_review'] else 0
        return out

    @staticmethod
    def _decode(row):
        if row and 'docs' in row and isinstance(row['docs'], str):
            row['docs'] = json.loads(row['docs'] or '[]')
        return row

    # ---- records ------------------------------------------------------------------------

    def list(self, kind):
        if kind not in KINDS:
            raise ValidationError(f'unknown kind: {kind}')
        order = {'medications': 'COALESCE(end_date, "9999") DESC, start_date DESC, name',
                 'conditions': 'status, name', 'allergies': 'substance',
                 'providers': 'name'}.get(kind, 'date DESC, id')
        rows = [self._decode(r) for r in self._rows(f'SELECT * FROM {kind} ORDER BY {order}')]
        if kind == 'medications':
            for r in rows:
                r['otc'] = self.is_otc(r['name'])
        return rows

    def get(self, kind, rid):
        if kind not in KINDS:
            raise ValidationError(f'unknown kind: {kind}')
        r = self.db.execute(f'SELECT * FROM {kind} WHERE id=?', (rid,)).fetchone()
        return self._decode(dict(r)) if r else None

    def create(self, kind, data):
        rec = self._clean_record(kind, data)
        rid = str(data.get('id') or f'{kind[:3]}-{datetime.now().strftime("%Y%m%d%H%M%S%f")}')
        rec.update(id=rid, origin='manual', edited=1, updated=now())
        rec.setdefault('docs', '[]')
        cols = ', '.join(rec)
        with self.lock:
            self.db.execute(f'INSERT INTO {kind} ({cols}) VALUES ({", ".join("?" * len(rec))})',
                            list(rec.values()))
            self.db.commit()
        return self.get(kind, rid)

    def update(self, kind, rid, data):
        if not self.get(kind, rid):
            return None
        rec = self._clean_record(kind, data, partial=True)
        rec.update(edited=1, updated=now())
        sets = ', '.join(f'{k}=?' for k in rec)
        with self.lock:
            self.db.execute(f'UPDATE {kind} SET {sets} WHERE id=?', [*rec.values(), rid])
            self.db.commit()
        return self.get(kind, rid)

    def delete(self, kind, rid):
        """Delete an entry. It is remembered, so a later import doesn't put it back."""
        if kind not in KINDS:
            raise ValidationError(f'unknown kind: {kind}')
        self._moved_table()
        with self.lock:
            n = self.db.execute(f'DELETE FROM {kind} WHERE id=?', (rid,)).rowcount
            if n:
                self.db.execute('INSERT OR REPLACE INTO moved VALUES (?, ?, ?, ?)', (kind, rid, DELETED, now()))
            self.db.commit()
        return n > 0

    def upsert_imported(self, kind, items):
        """Import records with stable ids; skips rows edited on the dashboard and rows moved to
        another person."""
        moved = self.moved_ids(kind)
        items = [i for i in items if str(i.get('id')) not in moved]
        added = updated = skipped = 0
        with self.lock:
            for item in items:
                if not item.get('id'):
                    raise ValidationError(f'{kind}: imported records need an id')
                rec = self._clean_record(kind, item)
                rec.setdefault('docs', '[]')
                rec.setdefault('needs_review', 0)
                rec.update(id=str(item['id']), origin='import', updated=now())
                old = self.db.execute(f'SELECT edited FROM {kind} WHERE id=?', (rec['id'],)).fetchone()
                if old and old['edited']:
                    skipped += 1
                    continue
                cols = ', '.join(rec)
                self.db.execute(f'INSERT OR REPLACE INTO {kind} ({cols}) VALUES '
                                f'({", ".join("?" * len(rec))})', list(rec.values()))
                if old:
                    updated += 1
                else:
                    added += 1
            self.db.commit()
        return {'added': added, 'updated': updated, 'skipped_edited': skipped}

    # ---- documents ----------------------------------------------------------------------

    def upsert_documents(self, docs):
        moved = self.moved_ids('documents')
        docs = [d for d in docs if str(d.get('id')) not in moved]
        added = updated = 0
        with self.lock:
            for d in docs:
                did = str(d['id'])
                old = self.db.execute('SELECT edited, needs_review FROM documents WHERE id=?',
                                      (did,)).fetchone()
                vals = {f: d.get(f) for f in DOC_FIELDS}
                if isinstance(vals.get('ocr_pages'), list):
                    vals['ocr_pages'] = json.dumps(vals['ocr_pages'])
                vals['text'] = d.get('text', '')
                if old:
                    if old['edited']:  # keep hand-edited metadata, refresh text and paths only
                        vals = {k: vals[k] for k in ('path', 'orig_path', 'organized_path', 'text',
                                                     'pages', 'ocr_pages', 'size', 'ext')}
                    else:
                        vals['needs_review'] = 1 if d.get('needs_review', old['needs_review']) else 0
                    vals['updated'] = now()
                    sets = ', '.join(f'{k}=?' for k in vals)
                    self.db.execute(f'UPDATE documents SET {sets} WHERE id=?', [*vals.values(), did])
                    updated += 1
                else:
                    vals.update(id=did, added=now(), updated=now(),
                                needs_review=1 if d.get('needs_review', True) else 0)
                    cols = ', '.join(vals)
                    self.db.execute(f'INSERT INTO documents ({cols}) VALUES '
                                    f'({", ".join("?" * len(vals))})', list(vals.values()))
                    added += 1
            self.db.commit()
        return {'added': added, 'updated': updated}

    def documents(self):
        rows = self._rows('SELECT id, path, orig_path, organized_path, title, doc_date, category, '
                          'facility, pages, ocr_pages, size, ext, summary, has_file, needs_review, '
                          'edited, added, status, review_note FROM documents ORDER BY doc_date DESC, title')
        for r in rows:
            r['ocr_pages'] = json.loads(r['ocr_pages'] or '[]')
        return rows

    def document(self, did):
        r = self.db.execute('SELECT * FROM documents WHERE id=?', (did,)).fetchone()
        if not r:
            return None
        r = dict(r)
        r['ocr_pages'] = json.loads(r['ocr_pages'] or '[]')
        r['linked'] = self.records_for_doc(did)
        return r

    def update_document(self, did, data):
        allowed = {'title', 'doc_date', 'category', 'facility', 'summary', 'needs_review', 'review_note'}
        vals = {k: v for k, v in data.items() if k in allowed}
        if 'doc_date' in vals and vals['doc_date'] and not DATE_RE.match(vals['doc_date']):
            raise ValidationError('doc_date must look like YYYY-MM-DD')
        if 'needs_review' in vals:
            vals['needs_review'] = 1 if vals['needs_review'] else 0
        if not vals:
            raise ValidationError('nothing to update')
        vals.update(edited=1, person_edited=1, updated=now())
        with self.lock:
            n = self.db.execute(f'UPDATE documents SET {", ".join(f"{k}=?" for k in vals)} WHERE id=?',
                                [*vals.values(), did]).rowcount
            self.db.commit()
        return self.document(did) if n else None

    def set_upload_result(self, did, **vals):
        """Results of reading an uploaded file. Marked edited so a later ingest from the records
        folder doesn't replace them with a guess from the file name."""
        if 'ocr_pages' in vals:
            vals['ocr_pages'] = json.dumps(vals['ocr_pages'])
        if 'needs_review' in vals:
            vals['needs_review'] = 1 if vals['needs_review'] else 0
        vals.update(edited=1, updated=now())
        with self.lock:
            self.db.execute(f'UPDATE documents SET {", ".join(f"{k}=?" for k in vals)} WHERE id=?',
                            [*vals.values(), did])
            self.db.commit()

    def texts_except(self, did):
        return [(r[0], r[1], r[2], r[3]) for r in self.db.execute(
            "SELECT id, title, doc_date, text FROM documents WHERE id<>? AND coalesce(status,'done')='done'",
            (did,))]

    def lab_exists(self, row):
        """Same test, same day, same result already recorded (from any document)?"""
        if row.get('num') is not None:
            q = self.db.execute('SELECT 1 FROM labs WHERE date=? AND lower(analyte)=lower(?) AND num=? LIMIT 1',
                                (row['date'], row['analyte'], float(row['num'])))
        else:
            q = self.db.execute('SELECT 1 FROM labs WHERE date=? AND lower(analyte)=lower(?) AND value=? LIMIT 1',
                                (row['date'], row['analyte'], row.get('value')))
        return q.fetchone() is not None

    def plugin_pages(self, except_id):
        """Saved portal pages nobody has saved by hand: (id, text)."""
        return [(r[0], r[1]) for r in self.db.execute(
            "SELECT id, text FROM documents WHERE ext='.html' AND path LIKE 'Uploaded/%' AND person_edited=0 "
            "AND coalesce(status,'done')='done' AND id<>?", (except_id,))]

    def link_doc(self, kind, rid, did):
        """Add a source document to an existing entry (doesn't count as a person's edit)."""
        r = self.get(kind, rid)
        if not r or did in r['docs']:
            return
        with self.lock:
            self.db.execute(f'UPDATE {kind} SET docs=? WHERE id=?', (json.dumps(r['docs'] + [did]), rid))
            self.db.commit()

    def tidy_imported_places(self, place):
        """Imported visits saved as 'Clinic / Clinic' by older versions -> 'Clinic' (edited ones are left alone)."""
        with self.lock:
            for kind in ('encounters', 'procedures'):
                for rid, fac in self.db.execute(f"SELECT id, facility FROM {kind} WHERE id LIKE 'ccda-%' AND edited=0 "
                                                "AND facility LIKE '% / %'").fetchall():
                    new = place(*fac.split(' / '))
                    if new != fac:
                        self.db.execute(f'UPDATE {kind} SET facility=? WHERE id=?', (new, rid))
            self.db.commit()

    # ---- one name per place, one entry per procedure -----------------------------------

    def set_place_names(self, names):
        """{"Example General Hospital": ["EXAMPLE GEN HOSP", "EGH"], ...}: every other way a place
        is written, and the name to show for it. Replaces the earlier list."""
        if not isinstance(names, dict):
            raise ValidationError('place names must be {name: [other spellings]}')
        with self.lock:
            self.db.execute('CREATE TABLE IF NOT EXISTS place_alias (alias TEXT PRIMARY KEY, name TEXT)')
            self.db.execute('DELETE FROM place_alias')
            for name, others in names.items():
                for o in others if isinstance(others, list) else [others]:
                    self.db.execute('INSERT OR REPLACE INTO place_alias VALUES (?, ?)', (_squash(o), str(name).strip()))
            self.db.commit()

    def tidy_places(self):
        """Every facility written one way: the listed name for a known other spelling, and one
        spelling for names that differ only in capitals and spaces (the most used one, preferring
        one that isn't all capitals). Returns how many entries changed."""
        self.db.execute('CREATE TABLE IF NOT EXISTS place_alias (alias TEXT PRIMARY KEY, name TEXT)')
        alias = dict(self.db.execute('SELECT alias, name FROM place_alias').fetchall())
        tables = [k for k in KINDS if 'facility' in KINDS[k]] + ['documents']
        from collections import Counter
        count = Counter()
        for t in tables:
            for (f,) in self.db.execute(f'SELECT facility FROM {t} WHERE facility IS NOT NULL').fetchall():
                parts = []
                for x in f.split(' / '):
                    if _squash(x) not in map(_squash, parts):
                        parts.append(x.strip())
                f = ' / '.join(parts)
                count[alias.get(_squash(f), f)] += 1
        best = {}
        for name, n in count.items():
            k, cur = _squash(name), best.get(_squash(name))
            if cur is None or (name.isupper(), -n) < (cur.isupper(), -count[cur]):
                best[k] = name
        for name in alias.values():   # a listed name wins over any spelling of it
            best[_squash(name)] = name
        changed = 0
        with self.lock:
            for t in tables:
                for rid, f in self.db.execute(f'SELECT id, facility FROM {t} WHERE facility IS NOT NULL').fetchall():
                    a = alias.get(_squash(f), f)
                    new = best.get(_squash(a), a)
                    parts = []
                    for x in new.split(' / '):   # "Clinic / Clinic" -> "Clinic"
                        if _squash(x) not in map(_squash, parts):
                            parts.append(x.strip())
                    new = ' / '.join(parts)
                    new = alias.get(_squash(new), new)
                    new = best.get(_squash(new), new)
                    if new != f:
                        self.db.execute(f'UPDATE {t} SET facility=? WHERE id=?', (new, rid))
                        changed += 1
            self.db.commit()
        return changed

    def places(self):
        """Every place named in the records, most used first (for suggestions when typing one)."""
        from collections import Counter
        n = Counter()
        for t in [k for k in KINDS if 'facility' in KINDS[k]] + ['documents']:
            for f, c in self.db.execute(f'SELECT facility, COUNT(*) FROM {t} WHERE facility IS NOT NULL '
                                        f"AND facility != '' GROUP BY facility").fetchall():
                n[f] += c
        return [f for f, _ in n.most_common()]

    def merge_imported_procedures(self, kind='procedures'):
        """A procedure from a health summary that is also entered in detail (same date, give or take
        a day, or the same month when only the month is known, and a word of the name in common)
        is folded into the detailed entry: its documents are linked there and it is removed, so a
        later import doesn't add it again. Returns how many were merged."""
        def words(n):
            n = (n or '').lower()
            for short, full in PROCEDURE_SHORT:
                n = re.sub(short, ' ' + full + ' ', n)
            return {w for w in re.findall(r'[a-z]{4,}', n)
                    if w not in ('left', 'right', 'with', 'lumbar', 'none', 'history', 'procedure', 'implants',
                                 'without', 'contrast', 'view', 'views', 'portable', 'bilateral', 'unilateral')}
        def related(a, b):
            return any(x in y or y in x for x in words(a) for y in words(b))
        merged = 0
        for r in self.list(kind):
            if not (r['id'] or '').startswith('ccda-') or r.get('edited') or not r.get('date'):
                continue
            match = [x for x in self.same_day(kind, r['date'], days=1 if kind == 'procedures' else 0) if related(x['name'], r['name'])]
            if not match:
                continue
            for d in r['docs']:
                self.link_doc(kind, match[0]['id'], d)
            self.delete(kind, r['id'])
            merged += 1
        return merged

    def same_day(self, kind, date, days=1):
        """Entries not from an import whose date is within `days` of `date`."""
        from datetime import date as D
        if not date:
            return []
        out = []
        for r in self.list(kind):
            other = r.get('date') or ''
            if (r['id'] or '').startswith('ccda-') or not other:
                continue
            if len(date) < 10 or len(other) < 10:   # a month or a year: the same if one contains the other
                if date.startswith(other) or other.startswith(date):
                    out.append(r)
                continue
            try:
                if abs((D.fromisoformat(other[:10]) - D.fromisoformat(date[:10])).days) <= days:
                    out.append(r)
            except ValueError:
                continue
        return out

    def consolidate_meds(self, drug_key, record_id, gap_days=60):
        """Imported medication lines (not edited by a person) merged into one entry per drug per
        period of use: lines of the same drug less than gap_days apart become one entry. An
        over-the-counter drug becomes a single entry, since its start and stop aren't tracked."""
        from datetime import date as D, timedelta
        rows = [r for r in self.list('medications') if (r['id'] or '').startswith('ccda-') and not r.get('edited')]
        # "generic (Brand 10 mg)" teaches that Brand is that generic; learned names are kept,
        # because once merged the "generic (Brand)" line may be gone
        def base_key(name):   # "Misc Med (EXAMPLAMATE 100MG)" is examplamate
            k = drug_key(name)
            if k in NOT_DRUGS:
                inner = re.search(r'\(([^()]+)', name or '')
                k2 = drug_key(inner.group(1)) if inner else ''
                return k2 if k2 and k2 not in NOT_DRUGS else k
            return k
        generic_words = {base_key(m.group(1)) for r in rows
                         for m in [re.match(r'\s*([^()]+?)\s*\(', r['name'] or '')] if m}
        ok = lambda b, g: (b != g and len(g) >= 4 and len(b) >= 4 and b not in NOT_DRUGS and g not in NOT_DRUGS
                           and b not in generic_words and drug_key(b) == b)
        self.db.execute('CREATE TABLE IF NOT EXISTS drug_alias (brand TEXT PRIMARY KEY, generic TEXT)')
        alias = {b: g for b, g in self.db.execute('SELECT brand, generic FROM drug_alias')
                 if b != g and b not in NOT_DRUGS and g not in NOT_DRUGS}
        pairs = set()
        for r in rows:
            m = re.match(r'\s*([^()]+?)\s*\(\s*([A-Za-z][A-Za-z-]{2,})', r['name'] or '')
            if m:
                pairs.add((m.group(2).lower(), base_key(m.group(1))))
        for b, g in sorted(pairs):
            if (g, b) in pairs:   # written both ways round ("A (B)" and "B (A)"): one drug, joined one way
                b, g = max(b, g), min(b, g)
                fits = b != g and b not in NOT_DRUGS and g not in NOT_DRUGS
            else:
                fits = ok(b, g)
            if fits and b not in alias:
                alias[b] = g
                self.db.execute('INSERT OR IGNORE INTO drug_alias VALUES (?, ?)', (b, g))

        def key_of(name):
            k = base_key(name)
            for _ in range(3):   # follow brand -> generic, never round in circles
                if k not in alias or alias[k] == k:
                    break
                k = alias[k]
            return k
        groups = {}
        for r in rows:
            groups.setdefault(key_of(r['name']), []).append(r)
        # when each line was last seen: an active line is known to go on at least that long
        doc_dates = {}
        ids = list({x for r in rows for x in r['docs']})
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for did, dd in self.db.execute(f"SELECT id, doc_date FROM documents WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                if dd and dd[:4] >= '1950':
                    doc_dates[did] = dd[:10]

        def d(v):
            try:
                return D.fromisoformat((v or '')[:10]) if len(v or '') >= 10 else (
                    D.fromisoformat(v + '-01') if len(v or '') == 7 else D.fromisoformat(v + '-01-01') if len(v or '') == 4 else None)
            except ValueError:
                return None
        def seen(r):
            ds = [d(doc_dates[x]) for x in r['docs'] if x in doc_dates]
            return max([x for x in ds if x] or [None], key=lambda x: x or D.min)

        def until(r):   # the latest date this line tells us about
            e = d(r['end_date'])
            if e:
                return e
            s, last = d(r['start_date']), seen(r)
            if r['status'] == 'active' and last and (not s or last > s):
                return last
            return s
        merged = []
        for key, items in groups.items():
            items.sort(key=lambda r: (d(r['start_date']) or D.max, r['updated'] or ''))
            episodes = []
            otc = all(self.is_otc(r['name']) for r in items)
            for r in items:
                s, e = d(r['start_date']), until(r)
                if episodes and (otc or s is None or episodes[-1]['end'] is None or s <= episodes[-1]['end'] + timedelta(days=gap_days)):
                    ep = episodes[-1]
                    ep['rows'].append(r)
                    if e and (ep['end'] is None or e > ep['end']):
                        ep['end'] = e
                else:
                    episodes.append({'start': s, 'end': e, 'rows': [r]})
            for ep in episodes:
                # the newest word on it decides: a stop on 3/24 beats a list from 3/16 that shows it
                last = max(ep['rows'], key=lambda r: (until(r) or D.min, r['status'] == 'stopped',
                                                      d(r['start_date']) or D.min, r['id']))
                active = last['status'] == 'active' and not last['end_date']
                known = lambda f: last[f] or next((r[f] for r in sorted(ep['rows'], key=lambda r: until(r) or D.min, reverse=True)
                                                   if r[f]), None)
                docs = []
                for r in ep['rows']:
                    docs += [x for x in r['docs'] if x not in docs]
                starts = [r['start_date'] for r in ep['rows'] if r['start_date']]
                first = min(starts) if starts else None
                ends = [r['end_date'] for r in ep['rows'] if r['end_date']]
                n = max(len(docs), len(ep['rows']))   # documents that list it (a merged entry keeps them all)
                merged.append({'id': record_id('med', key, first), 'docs': docs[:40], 'name': last['name'],
                               'dose': known('dose'), 'schedule': known('schedule'), 'purpose': known('purpose'),
                               'start_date': first, 'end_date': None if active else (max(ends) if ends else last['start_date']),
                               'status': 'active' if active else 'stopped', 'prescriber': known('prescriber'),
                               'notes': (f'Listed {n} times in the records' + (f' ({first} to {max(ends + starts)})' if starts else '') + '. '
                                         if n > 1 else '') + re.sub(r'^(Listed \d+ times in the records[^.]*\.\s*)+', '',
                                                                    last['notes'] or '')})
        keep = {m['id'] for m in merged}
        with self.lock:
            for r in rows:
                if r['id'] not in keep:
                    self.db.execute('DELETE FROM medications WHERE id=?', (r['id'],))
            self.db.commit()
        self.upsert_imported('medications', merged)
        return {'lines': len(rows), 'entries': len(merged)}

    def set_otc(self, cfg):
        """{"words": [more over-the-counter drug names], "not": [patterns that make one prescription,
        e.g. a strength]}: added to the built-in lists. Replaces the earlier setting."""
        if not isinstance(cfg, dict):
            raise ValidationError('otc must be {"words": [...], "not": [...]}')
        words = [str(w) for w in cfg.get('words') or []]
        nots = [str(x) for x in cfg.get('not') or []]
        for x in nots:
            try:
                re.compile(x)
            except re.error as e:
                raise ValidationError(f'otc "not" pattern {x!r}: {e}')
        with self.lock:
            self.db.execute('CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT)')
            self.db.execute('INSERT OR REPLACE INTO setting VALUES (?, ?)', ('otc', json.dumps({'words': words, 'not': nots})))
            self.db.commit()
        object.__setattr__(self, '_otc', None)

    def is_otc(self, name):
        if getattr(self, '_otc', None) is None:
            self.db.execute('CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT)')
            row = self.db.execute("SELECT value FROM setting WHERE key='otc'").fetchone()
            cfg = json.loads(row[0]) if row else {}
            words = [re.escape(w) for w in cfg.get('words') or []]
            yes = re.compile(r'\b(' + '|'.join(words) + r')\b', re.I) if words else None
            no = re.compile('|'.join(cfg.get('not')), re.I) if cfg.get('not') else None
            self._otc = (yes, no)
        return is_otc(name, self._otc)

    def _drug_keys(self):
        """(key(name), root(word), parent): one key per drug, brand and generic names joined
        through what the records taught (drug_alias)."""
        from .ccda import drug_key
        self.db.execute('CREATE TABLE IF NOT EXISTS drug_alias (brand TEXT PRIMARY KEY, generic TEXT)')
        parent = {}

        def root(k):
            while parent.get(k, k) != k:
                k = parent[k]
            return k
        pairs = list(self.db.execute('SELECT brand, generic FROM drug_alias'))
        pairs += [(b.split('-')[0], g) for b, g in pairs if '-' in b and len(b.split('-')[0]) >= 4]   # "Toprol XL" is toprol
        for b, g in pairs:
            rb, rg = root(b), root(g)
            if rb != rg:
                parent[max(rb, rg)] = min(rb, rg)   # brand and generic, either way round: one drug

        def key(name):
            k = drug_key(name)
            if k in NOT_DRUGS:
                inner = re.search(r'\(([^()]+)', name or '')
                k = drug_key(inner.group(1)) if inner else k
            return root(k)
        return key, root, parent

    def merge_med_sources(self, slack_days=60):
        """A medication line from a health summary for a drug that is also entered by hand (or
        edited) over the same time is folded into that entry: its documents are linked there and
        it is removed, so the drug isn't listed twice. Returns how many were merged."""
        from datetime import date as D, timedelta
        key, _, _ = self._drug_keys()
        today = D.today()

        def span(r):
            def d(v, end=False):
                v = v or ''
                try:
                    return D.fromisoformat(v[:10] if len(v) >= 10 else (v + ('-28' if end else '-01'))[:10] if len(v) == 7
                                           else (v + ('-12-31' if end else '-01-01')))
                except ValueError:
                    return None
            s0 = d(r['start_date'])
            e0 = d(r['end_date'], True) or (today if r['status'] != 'stopped' else s0)
            return s0, e0
        meds = self.list('medications')
        mine = [m for m in meds if not (m['id'] or '').startswith('ccda-') or m.get('edited')]
        by_key = {}
        for m in mine:
            by_key.setdefault(key(m['name']), []).append(m)
        merged = 0
        for r in meds:
            if r in mine:
                continue
            rs, re_ = span(r)
            for m in by_key.get(key(r['name']), []):
                ms, me = span(m)
                if not (rs and ms and re_ and me):
                    continue
                if rs <= me + timedelta(days=slack_days) and ms <= re_ + timedelta(days=slack_days):
                    for x in r['docs']:
                        self.link_doc('medications', m['id'], x)
                    self.delete('medications', r['id'])
                    merged += 1
                    break
        return merged

    def merge_conditions(self):
        """One entry per condition: names that differ only by labels like "(Confirmed)" or
        "(Problem)", a trailing number, capitals or "Hx of" are one. The entry kept is a hand-made
        one if there is one; it is active if any copy is, noted from the earliest real date, and
        linked to every copy's documents. Returns how many were merged."""
        def clean(n):
            n = re.sub(r'\((confirmed|provisional|problem|disorder|finding|diagnosis)\)', ' ', n or '', flags=re.I)
            n = re.sub(r'\s+\d+\s*$', '', n)
            n = re.sub(r'^\s*hx\.? of\b', 'History of', n, flags=re.I)
            return re.sub(r'\s+', ' ', n).strip(' ,.-')
        groups = {}
        for c in self.list('conditions'):
            groups.setdefault(clean(c['name']).lower(), []).append(c)
        merged = 0
        for k, rows in groups.items():
            if not k:
                continue
            mine = [r for r in rows if not (r['id'] or '').startswith('ccda-') or r.get('edited')]
            keep = mine[0] if mine else min(rows, key=lambda r: (len(clean(r['name'])) != len(r['name'].strip()), r['first_noted'] is None, r['id']))
            dates = [r['first_noted'] for r in rows if r['first_noted'] and r['first_noted'][:4] >= '1910']
            vals = {}
            if not mine:
                vals['name'] = clean(keep['name'])
                vals['status'] = 'active' if any(r['status'] == 'active' for r in rows) else keep['status']
            if dates and min(dates) != keep['first_noted'] and not keep.get('edited'):
                vals['first_noted'] = min(dates)
            if vals and len(rows) > 1 or vals.get('name', keep['name']) != keep['name']:
                with self.lock:   # not a person's edit: a later import may still update it
                    self.db.execute(f"UPDATE conditions SET {', '.join(f + '=?' for f in vals)} WHERE id=?", [*vals.values(), keep['id']])
                    self.db.commit()
            for r in rows:
                if r['id'] == keep['id'] or r in mine:
                    continue
                for x in r['docs']:
                    self.link_doc('conditions', keep['id'], x)
                self.delete('conditions', r['id'])
                merged += 1
        return merged

    def apply_med_list(self, text, as_of, apply=False):
        """A current medication list ("Topiramate 100mg", one per line) set against the records:
        listed drugs stay or become active with the listed dose, other active prescriptions are
        stopped as of that date, and a drug listed under two names keeps one entry. Over-the-counter
        entries that aren't listed are left alone. With apply=False, only says what would change."""
        import difflib
        from .ccda import drug_key
        if not DATE_RE.match(as_of or '') or len(as_of) != 10:
            raise ValidationError('the list date must look like YYYY-MM-DD')
        key, root, parent = self._drug_keys()
        meds = self.list('medications')
        current = [m for m in meds if m['status'] == 'active' or (not m['end_date'] and not m['status'])]
        known = sorted({key(m['name']) for m in meds})
        # every name a known drug goes by, generic or brand, to catch a misspelling of either
        names = {n: root(n) for n in set(parent) | {drug_key(m['name']) for m in meds} if root(n) in known}
        listed = []
        for line in (text or '').splitlines():
            line = re.sub(r'^\s*[-*•\d.)]*\s+', '', line).strip() if re.match(r'^\s*([-*•]|\d+[.)])\s', line) else line.strip()
            if not line:
                continue
            m = re.match(r'(.*?[A-Za-z][^\d]*?)\s*(\d.*)?$', line)
            name, dose = (m.group(1).strip(), (m.group(2) or '').strip()) if m else (line, '')
            dose = re.sub(r'(?i)(\d)\s*(mg|mcg|ml|g|units?)\b', lambda x: f'{x.group(1)} {x.group(2).lower()}', dose) or None
            k = key(name)
            if k not in known:   # a typo ("Trazdone") finds the drug it is close to
                close = difflib.get_close_matches(k, list(names), n=1, cutoff=0.8)
                if close:
                    k, name = names[close[0]], close[0].title()   # a new entry gets the right spelling
            listed.append({'line': line, 'name': name, 'dose': dose, 'key': k})
        if not listed:
            raise ValidationError('paste the list, one medication per line')
        keep, stop, add, merge = [], [], [], []
        by_key = {}
        for m in current:
            by_key.setdefault(key(m['name']), []).append(m)
        for x in listed:
            rows = by_key.pop(x['key'], [])
            if not rows:
                add.append(x)
                continue
            rows.sort(key=lambda r: (-len(r['docs']), r['start_date'] or '9999'))   # the best-documented one stays
            keep.append({'id': rows[0]['id'], 'name': rows[0]['name'], 'line': x['line'],
                         'dose_from': rows[0]['dose'], 'dose': x['dose'] or rows[0]['dose']})
            merge += [{'id': r['id'], 'name': r['name'], 'into': rows[0]['id'], 'into_name': rows[0]['name']} for r in rows[1:]]
        for rows in by_key.values():
            stop += [{'id': r['id'], 'name': r['name'], 'start_date': r['start_date']} for r in rows if not r['otc']]
        plan = {'as_of': as_of, 'keep': keep, 'stop': stop, 'add': [{'name': x['name'], 'dose': x['dose'], 'line': x['line']} for x in add],
                'merge': merge}
        if not apply:
            return plan
        note = f'On the current medication list of {as_of}.'
        for k in keep:
            r = self.get('medications', k['id'])
            notes = r['notes'] or ''
            same = lambda a, b: re.sub(r'\.0\b|\s', '', (a or '').lower()) == re.sub(r'\.0\b|\s', '', (b or '').lower())
            if k['dose_from'] and not same(k['dose'], k['dose_from']):
                notes = f"{notes} Earlier records gave the dose as {k['dose_from']}.".strip()
            self.update('medications', k['id'], {'status': 'active', 'end_date': None, 'dose': k['dose'],
                                                  'notes': f'{notes} {note}'.strip(), 'needs_review': 0})
        for g in merge:
            r = self.get('medications', g['id'])
            for d in r['docs']:
                self.link_doc('medications', g['into'], d)
            self.delete('medications', g['id'])
        for x in stop:
            r = self.get('medications', x['id'])
            self.update('medications', x['id'], {
                'status': 'stopped', 'end_date': as_of, 'needs_review': 0,
                'notes': f"{r['notes'] or ''} Not on the current medication list of {as_of}; stopped by then.".strip()})
        for x in add:
            self.create('medications', {'name': x['name'], 'dose': x['dose'], 'status': 'active', 'notes': note})
        return plan

    def _moved_table(self):
        self.db.executescript('CREATE TABLE IF NOT EXISTS moved (kind TEXT, id TEXT, to_person TEXT, at TEXT, '
                              'PRIMARY KEY (kind, id));')

    def undelete(self, kind, rid):
        """Someone added it again on purpose: imports may bring it back."""
        self._moved_table()
        with self.lock:
            self.db.execute('DELETE FROM moved WHERE kind=? AND id=? AND to_person=?', (kind, rid, DELETED))
            self.db.commit()

    def moved_ids(self, kind):
        self._moved_table()
        return {r[0] for r in self.db.execute('SELECT id FROM moved WHERE kind=?', (kind,))}

    def take_document(self, did, to_person):
        """Remove a document and the entries made only from it, for moving to someone else.
        Returns (document row, {kind: [records]}). Entries that also cite other documents stay,
        without this one. The ids are remembered so a later import doesn't put them back."""
        self._moved_table()
        doc = self.db.execute('SELECT * FROM documents WHERE id=?', (did,)).fetchone()
        if not doc:
            raise ValidationError('no such document')
        doc = dict(doc)
        taken = {}
        with self.lock:
            for kind in KINDS:
                for r in self.list(kind):
                    if did not in r['docs']:
                        continue
                    if r['docs'] == [did]:
                        taken.setdefault(kind, []).append(r)
                        self.db.execute(f'DELETE FROM {kind} WHERE id=?', (r['id'],))
                        self.db.execute('INSERT OR REPLACE INTO moved VALUES (?, ?, ?, ?)', (kind, r['id'], to_person, now()))
                    else:
                        self.db.execute(f'UPDATE {kind} SET docs=? WHERE id=?',
                                        (json.dumps([x for x in r['docs'] if x != did]), r['id']))
            self.db.execute('DELETE FROM documents WHERE id=?', (did,))
            self.db.execute('INSERT OR REPLACE INTO moved VALUES (?, ?, ?, ?)', ('documents', did, to_person, now()))
            self.db.commit()
        return doc, taken

    def give_document(self, doc, records):
        """Add a document and its entries moved from someone else (kept as a person's edits)."""
        self._moved_table()
        with self.lock:   # if it had been moved away from here before, it's back now
            self.db.execute("DELETE FROM moved WHERE kind='documents' AND id=?", (doc['id'],))
            for kind, rows in records.items():
                for r in rows:
                    self.db.execute('DELETE FROM moved WHERE kind=? AND id=?', (kind, r['id']))
            self.db.commit()
        cols = [c for c in doc if c != 'rowid']
        with self.lock:
            self.db.execute(f'INSERT OR REPLACE INTO documents ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                            [doc[c] for c in cols])
            self.db.execute('UPDATE documents SET edited=1, person_edited=1 WHERE id=?', (doc['id'],))
            self.db.commit()
        for kind, rows in records.items():
            for r in rows:
                rec = {f: r.get(f) for f in KINDS[kind]}
                rec.update(id=r['id'], docs=r['docs'], needs_review=r.get('needs_review', 0))
                self.upsert_imported(kind, [rec])
                with self.lock:
                    self.db.execute(f'UPDATE {kind} SET edited=1, origin=? WHERE id=?', ('moved', r['id']))
                    self.db.commit()

    def clear_upload_entries(self, did):
        """Entries read from an upload (not edited by a person), before reading it again."""
        with self.lock:
            self.db.execute("DELETE FROM labs WHERE id LIKE ? AND edited=0", (f'lab-u{did[:10]}-%',))
            self.db.commit()

    def delete_upload(self, did):
        """Remove a document added from the dashboard and the entries read from it."""
        d = self.document(did)
        if not d or not (d.get('path') or '').startswith('Uploaded/'):
            return None
        with self.lock:
            self.db.execute('DELETE FROM documents WHERE id=?', (did,))
            self.db.execute("DELETE FROM labs WHERE id LIKE ? AND edited=0", (f'lab-u{did[:10]}-%',))
            self.db.commit()
        return d

    def delete_document(self, did):
        """Take a document off the dashboard (the file in the records folder is not touched).
        Entries keep their other sources; it is remembered so a later import doesn't add it back."""
        d = self.document(did)
        if not d:
            return None
        if (d.get('path') or '').startswith('Uploaded/'):
            self.delete_upload(did)
        self._moved_table()
        with self.lock:
            self.db.execute('DELETE FROM documents WHERE id=?', (did,))
            for kind in KINDS:
                for rid, docs in self.db.execute(f"SELECT id, docs FROM {kind} WHERE docs LIKE ?", (f'%{did}%',)).fetchall():
                    self.db.execute(f'UPDATE {kind} SET docs=? WHERE id=?',
                                    (json.dumps([x for x in json.loads(docs or '[]') if x != did]), rid))
            self.db.execute('INSERT OR REPLACE INTO moved VALUES (?, ?, ?, ?)', ('documents', did, DELETED, now()))
            self.db.commit()
        return d

    def placeholder_dates(self):
        """Older imports took 1/1/1900 ("no date" in some portals) as a real date. Removes the
        visits made from it (unless a person edited them) and returns the export uploads to read
        again."""
        with self.lock:
            gone = self.db.execute("DELETE FROM encounters WHERE id LIKE 'ccda-%' AND edited=0 "
                                   "AND date IS NOT NULL AND date <= '1900-12-31'").rowcount
            docs = [r[0] for r in self.db.execute(
                "SELECT id FROM documents WHERE doc_date IS NOT NULL AND doc_date <= '1900-12-31'")]
            if docs:
                self.db.execute(f"UPDATE documents SET doc_date=NULL WHERE id IN ({','.join('?' * len(docs))}) "
                                "AND edited=0", docs)
            again = [r[0] for r in self.db.execute(
                f"SELECT id FROM documents WHERE id IN ({','.join('?' * len(docs))}) AND path LIKE 'Uploaded/%' "
                "AND person_edited=0 AND ext IN ('.zip', '.xml')", docs)] if docs else []
            self.db.commit()
        return gone, again

    def stale_uploads(self, version):
        """Uploads sorted by older rules that no person has touched yet."""
        return [r[0] for r in self.db.execute(
            "SELECT id FROM documents WHERE path LIKE 'Uploaded/%' AND status='done' AND person_edited=0 "
            "AND coalesce(classified,'')<>?", (version,))]

    def known_facilities(self):
        names = set()
        for table, col in (('documents', 'facility'), ('encounters', 'facility'), ('tests', 'facility'),
                           ('procedures', 'facility'), ('providers', 'facility'), ('imaging', 'facility')):
            names.update(r[0] for r in self.db.execute(f'SELECT DISTINCT {col} FROM {table}') if r[0])
        return sorted(names)

    def filed_for_learning(self):
        """(facility, category, text) of documents a person has confirmed or that were catalogued."""
        return [(r[0], r[1], r[2]) for r in self.db.execute(
            "SELECT facility, category, text FROM documents WHERE needs_review=0 AND facility IS NOT NULL "
            "AND coalesce(status, 'done')='done' AND (path NOT LIKE 'Uploaded/%' OR person_edited=1)")]

    def set_has_file(self, did, has=True):
        with self.lock:
            self.db.execute('UPDATE documents SET has_file=? WHERE id=?', (1 if has else 0, did))
            self.db.commit()

    def records_for_doc(self, did):
        out = []
        for kind in KINDS:
            for r in self._rows(f"SELECT * FROM {kind} WHERE docs LIKE ?", (f'%"{did}"%',)):
                r = self._decode(r)
                r['kind'] = kind
                out.append(r)
        return out

    # ---- search & views -------------------------------------------------------------------

    def search(self, q, limit=60):
        """Full-text search over documents plus a LIKE search over structured records."""
        q = (q or '').strip()
        if not q:
            return {'documents': [], 'records': []}
        terms = [t for t in re.findall(r'[\w.]+', q) if t]
        fts = ' '.join('"%s"*' % t.replace('"', '') for t in terms)
        docs = []
        if fts:
            docs = self._rows(
                "SELECT d.id, d.title, d.doc_date, d.category, d.facility, "
                "snippet(documents_fts, 2, '[[', ']]', ' … ', 18) AS snippet "
                "FROM documents_fts JOIN documents d ON d.rowid = documents_fts.rowid "
                "WHERE documents_fts MATCH ? ORDER BY rank LIMIT ?", (fts, limit))
        recs = []
        for kind, fields in KINDS.items():
            text_fields = [f for f in fields if f not in NUMERIC and f not in INTEGER]
            where = ' AND '.join(
                '(' + ' OR '.join(f'{f} LIKE ?' for f in text_fields) + ')' for _ in terms)
            args = [f'%{t}%' for t in terms for _ in text_fields]
            for r in self._rows(f'SELECT * FROM {kind} WHERE {where} LIMIT ?', args + [limit]):
                r = self._decode(r)
                r['kind'] = kind
                recs.append(r)
        return {'documents': docs, 'records': recs}

    def timeline(self):
        """Everything with a date, newest first, as uniform events."""
        ev = []

        def add(kind, rid, date, title, detail, facility=None, extra=None, docs=None):
            if date:
                e = {'kind': kind, 'id': rid, 'date': date, 'title': title, 'detail': detail,
                     'facility': facility, 'docs': docs or []}
                if extra:
                    e.update(extra)
                ev.append(e)

        for r in self.list('encounters'):
            add('encounters', r['id'], r['date'], f"{r['type'] or 'Visit'}: {r['reason'] or ''}".strip(': '),
                r['summary'], r['facility'], {'end_date': r['end_date'], 'type': r['type']}, r['docs'])
        for r in self.list('tests'):
            add('tests', r['id'], r['date'], r['name'], r['result'], r['facility'],
                {'category': r['category'], 'abnormal': r['abnormal']}, r['docs'])
        for r in self.list('procedures'):
            add('procedures', r['id'], r['date'], r['name'], r['notes'], r['facility'], None, r['docs'])
        for r in self.list('imaging'):
            add('imaging', r['id'], r['date'], f"{r['modality'] or ''} {r['description'] or ''}".strip(),
                r['notes'], r['facility'], None, r['docs'])
        for r in self.list('medications'):
            if r['otc']:
                continue
            label = f"{r['name']} {r['dose'] or ''}".strip()
            add('medications', r['id'], r['start_date'], f'Started {label}', r['purpose'], None,
                {'change': 'start'}, r['docs'])
            add('medications', r['id'], r['end_date'], f'Stopped {label}', r['notes'], None,
                {'change': 'stop'}, r['docs'])
        ev.sort(key=lambda e: (e['date'], e['kind']), reverse=True)
        return ev

    def lab_series(self):
        """{analyte: [{date, num, value, unit, flag, ref_range}]} for analytes with 2+ numeric values."""
        out = {}
        for r in self._rows('SELECT date, analyte, value, num, unit, flag, ref_range, facility, id, docs '
                            'FROM labs WHERE num IS NOT NULL ORDER BY analyte, date'):
            out.setdefault(r['analyte'], []).append(r)
        return {k: v for k, v in out.items() if len(v) >= 2}

    def summary(self):
        c = {k: self.db.execute(f'SELECT COUNT(*) FROM {k}').fetchone()[0] for k in KINDS}
        c['documents'] = self.db.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
        review = self.db.execute('SELECT COUNT(*) FROM documents WHERE needs_review=1').fetchone()[0]
        for k in KINDS:
            if k != 'medications':
                review += self.db.execute(f'SELECT COUNT(*) FROM {k} WHERE needs_review=1').fetchone()[0]
        review += sum(1 for r in self._rows('SELECT name FROM medications WHERE needs_review=1') if not self.is_otc(r['name']))
        current = [r for r in self._rows("SELECT * FROM medications WHERE (status='active' OR "
                                         "(end_date IS NULL AND status IS NULL)) ORDER BY name") if not self.is_otc(r['name'])]
        last = self._rows('SELECT * FROM encounters ORDER BY date DESC LIMIT 1')
        span = self.db.execute("SELECT MIN(date), MAX(date) FROM encounters").fetchone()
        return {'counts': c, 'needs_review': review,
                'current_meds': [self._decode(r) for r in current],
                'last_visit': self._decode(last[0]) if last else None,
                'first_date': span[0], 'last_date': span[1]}

    def review_queue(self):
        docs = self._rows('SELECT id, title, doc_date, category, path, status, review_note FROM documents '
                          'WHERE needs_review=1 ORDER BY added DESC')
        recs = []
        for kind in KINDS:
            for r in self._rows(f'SELECT * FROM {kind} WHERE needs_review=1'):
                if kind == 'medications' and self.is_otc(r['name']):
                    continue   # what to check on a medicine is its dates, which aren't tracked for these
                r = self._decode(r)
                r['kind'] = kind
                recs.append(r)
        return {'documents': docs, 'records': recs}

    def backup_bytes(self):
        """A consistent copy of the whole database (sqlite online backup), as bytes."""
        import os
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            dst = sqlite3.connect(tmp)
            with self.lock:
                self.db.backup(dst)
            dst.close()
            with open(tmp, 'rb') as f:
                return f.read()
        finally:
            os.remove(tmp)

    def close(self):
        self.db.close()

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
    # what was billed or quoted, not whether it was paid (payment status is deliberately not kept)
    'financial': ['date', 'kind', 'payee', 'account', 'description', 'charged', 'notes', 'encounter_id'],
}
NUMERIC = {'num', 'charged'}
DROPPED = {'financial': ['paid', 'adjusted', 'owed']}  # removed columns, erased on open
INTEGER = {'abnormal'}
DATE_FIELDS = {'date', 'end_date', 'start_date', 'first_noted'}
DOC_FIELDS = ['path', 'orig_path', 'title', 'doc_date', 'category', 'facility', 'pages', 'ocr_pages',
              'size', 'ext', 'summary', 'organized_path', 'status', 'review_note']
ADDED = {'documents': ['status TEXT', 'review_note TEXT', 'duplicate_of TEXT', 'classified TEXT',
                       'person_edited INTEGER DEFAULT 0']}  # columns added after the first release
# A financial row whose kind mentions an estimate or quote is not a charge: no work was billed.
ESTIMATE_SQL = "(lower(coalesce(kind,'')) LIKE '%estimate%' OR lower(coalesce(kind,'')) LIKE '%quote%')"


def is_estimate(kind):
    k = (kind or '').lower()
    return 'estimate' in k or 'quote' in k


DATE_RE = re.compile(r'^\d{4}(-\d{2}(-\d{2})?)?$')  # full date, or just year / year-month


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
        return [self._decode(r) for r in self._rows(f'SELECT * FROM {kind} ORDER BY {order}')]

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
        if kind not in KINDS:
            raise ValidationError(f'unknown kind: {kind}')
        with self.lock:
            n = self.db.execute(f'DELETE FROM {kind} WHERE id=?', (rid,)).rowcount
            self.db.commit()
        return n > 0

    def upsert_imported(self, kind, items):
        """Import records with stable ids; skips rows edited on the dashboard."""
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

    def bill_exists(self, date, charged):
        if charged is None:
            return False
        return self.db.execute('SELECT 1 FROM financial WHERE date IS ? AND charged=? LIMIT 1',
                               (date, float(charged))).fetchone() is not None

    def clear_upload_entries(self, did):
        """Entries read from an upload (not edited by a person), before reading it again."""
        with self.lock:
            self.db.execute("DELETE FROM labs WHERE id LIKE ? AND edited=0", (f'lab-u{did[:10]}-%',))
            self.db.execute("DELETE FROM financial WHERE id=? AND edited=0", (f'fin-u{did[:10]}',))
            self.db.commit()

    def delete_upload(self, did):
        """Remove a document added from the dashboard and the entries read from it."""
        d = self.document(did)
        if not d or not (d.get('path') or '').startswith('Uploaded/'):
            return None
        with self.lock:
            self.db.execute('DELETE FROM documents WHERE id=?', (did,))
            self.db.execute("DELETE FROM labs WHERE id LIKE ? AND edited=0", (f'lab-u{did[:10]}-%',))
            self.db.execute("DELETE FROM financial WHERE id=? AND edited=0", (f'fin-u{did[:10]}',))
            self.db.commit()
        return d

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

    def timeline(self, include_financial=False):
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
            label = f"{r['name']} {r['dose'] or ''}".strip()
            add('medications', r['id'], r['start_date'], f'Started {label}', r['purpose'], None,
                {'change': 'start'}, r['docs'])
            add('medications', r['id'], r['end_date'], f'Stopped {label}', r['notes'], None,
                {'change': 'stop'}, r['docs'])
        if include_financial:
            for r in self.list('financial'):
                add('financial', r['id'], r['date'], f"{r['kind'] or 'Bill'}: {r['payee'] or ''}",
                    r['description'], r['payee'], {'charged': r['charged']}, r['docs'])
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
            review += self.db.execute(f'SELECT COUNT(*) FROM {k} WHERE needs_review=1').fetchone()[0]
        current = self._rows("SELECT * FROM medications WHERE (status='active' OR "
                             "(end_date IS NULL AND status IS NULL)) ORDER BY name")
        last = self._rows('SELECT * FROM encounters ORDER BY date DESC LIMIT 1')
        span = self.db.execute("SELECT MIN(date), MAX(date) FROM encounters").fetchone()
        fin = self.db.execute(f'SELECT COUNT(*), SUM(charged) FROM financial WHERE NOT {ESTIMATE_SQL}').fetchone()
        est = self.db.execute(f'SELECT COUNT(*), SUM(charged) FROM financial WHERE {ESTIMATE_SQL}').fetchone()
        return {'counts': c, 'needs_review': review,
                'current_meds': [self._decode(r) for r in current],
                'last_visit': self._decode(last[0]) if last else None,
                'first_date': span[0], 'last_date': span[1],
                'financial': {'bills': fin[0] or 0, 'charged': fin[1] or 0,
                              'estimates': est[0] or 0, 'estimated': est[1] or 0}}

    def review_queue(self):
        docs = self._rows('SELECT id, title, doc_date, category, path, status, review_note FROM documents '
                          'WHERE needs_review=1 ORDER BY added DESC')
        recs = []
        for kind in KINDS:
            for r in self._rows(f'SELECT * FROM {kind} WHERE needs_review=1'):
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

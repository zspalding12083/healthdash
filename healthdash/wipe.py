"""Empty a dashboard to start over: every record, document and stored file, for the main person
and everyone on the people menu. Sign-in accounts, plugin keys and the people menu itself stay.

Run it with the dashboard stopped, as the user that owns the data:
  sudo systemctl stop healthdash
  sudo -u healthdash PYTHONPATH=/opt/healthdash python3 -m healthdash wipe --db /var/lib/healthdash/health.db --files /var/lib/healthdash/files
  sudo systemctl start healthdash
"""

import os
import sqlite3

from .store import KINDS, Store

# record tables plus bookkeeping that belongs to the records; never users, sessions, api_keys, people
EXTRA = ['drug_alias', 'moved']


def _empty_db(path):
    s = Store(path)   # brings an older database up to date first, so every table exists
    s.close()
    db = sqlite3.connect(path)
    counts = {}
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in list(KINDS) + ['documents'] + EXTRA:
            if t in tables:
                counts[t] = db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                db.execute(f'DELETE FROM {t}')
        db.execute("INSERT INTO documents_fts(documents_fts) VALUES ('rebuild')")
        db.commit()
        db.execute('VACUUM')   # so the old values are really gone from the file
    finally:
        db.close()
    return counts


def _empty_files(d):
    n = 0
    if os.path.isdir(d):
        for root, dirs, files in os.walk(d, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f)); n += 1
            for x in dirs:
                os.rmdir(os.path.join(root, x))
    return n


def targets(db, files):
    """(label, database, files folder) for the main person and each person under <state>/people/."""
    out = [('main', db, files)]
    people = os.path.join(os.path.dirname(os.path.abspath(db)), 'people')
    if os.path.isdir(people):
        for pid in sorted(os.listdir(people)):
            p = os.path.join(people, pid)
            if os.path.isfile(os.path.join(p, 'health.db')):
                out.append((pid, os.path.join(p, 'health.db'), os.path.join(p, 'files')))
    return out


def wipe(db, files, yes=False, ask=input):
    if not os.path.exists(db):
        raise SystemExit(f'no database at {db}')
    todo = targets(db, files)
    print('This empties every record, document and stored file for:', ', '.join(t[0] for t in todo))
    print('Sign-in accounts, plugin keys and the people menu are kept. This cannot be undone.')
    if not yes and ask('Type WIPE to go ahead: ').strip() != 'WIPE':
        print('nothing changed')
        return 1
    for label, d, f in todo:
        counts = _empty_db(d)
        n = _empty_files(f)
        shown = ', '.join(f'{v} {k}' for k, v in counts.items() if v)
        print(f'{label}: removed {shown or "nothing"}; {n} stored files')
    return 0

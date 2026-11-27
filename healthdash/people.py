"""Several people on one dashboard: each with their own database and files, one set of accounts.

The first person ("main") is the dashboard's original database and files folder, so nothing moves
when people are added. Everyone else lives under <state>/people/<id>/ (health.db, files/). The list
of people is kept in the main database. Pages pick a person with the X-Person header (file links
use ?p=<id>); without one, requests go to the main person.
"""

import os
import re
import threading

from .store import Store, ValidationError, now

SCHEMA = 'CREATE TABLE IF NOT EXISTS people (id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT);'
MAIN = 'main'


class Person:
    def __init__(self, pid, name, store, files_dir, uploads):
        """One person: their id, shown name, store, files folder and upload worker."""
        self.id, self.name, self.store, self.files_dir, self.uploads = pid, name, store, files_dir, uploads


class People:
    """Everyone on one dashboard: each person's store, files and upload worker, by id."""
    def __init__(self, store, files_dir, uploads, state_dir=None, make_uploads=None, main_name='Main'):
        """Open the main person (the original database) and everyone listed in its people table."""
        self.home = store
        self.state = state_dir
        self.make_uploads = make_uploads
        self.storage = None       # storage.Storage, when files may be kept in a chosen folder
        self._lock = threading.Lock()
        store.db.executescript(SCHEMA)
        store.db.commit()
        row = store.db.execute('SELECT name FROM people WHERE id=?', (MAIN,)).fetchone()
        if not row:
            with store.lock:
                store.db.execute('INSERT INTO people VALUES (?, ?, ?)', (MAIN, main_name, now()))
                store.db.commit()
        self.by_id = {MAIN: Person(MAIN, row[0] if row else main_name, store, files_dir, uploads)}
        self.by_id[MAIN].files_dir_default = files_dir
        for pid, name in store.db.execute('SELECT id, name FROM people WHERE id<>? ORDER BY created', (MAIN,)):
            self.by_id[pid] = self._open(pid, name)

    def _open(self, pid, name):
        if not self.state:
            raise ValidationError('this dashboard was started without a data folder for more people')
        d = os.path.join(self.state, 'people', pid)
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = Store(os.path.join(d, 'health.db'))
        files = self.storage.dir_for(pid) if self.storage else self.default_files(pid)
        if not self.storage or self.storage.available():
            os.makedirs(files, mode=0o700, exist_ok=True)
        return Person(pid, name, st, files, self.make_uploads(st, files) if self.make_uploads else None)

    def default_files(self, pid):
        """A person's files folder when no files folder has been chosen (see storage.py)."""
        return self.by_id[MAIN].files_dir_default if pid == MAIN else os.path.join(self.state or '', 'people', pid, 'files')

    def get(self, pid):
        p = self.by_id.get(pid or MAIN)
        if not p:
            raise ValidationError('no such person on this dashboard')
        return p

    def list(self):
        return [{'id': p.id, 'name': p.name} for p in self.by_id.values()]

    def add(self, name):
        """Add a person with their own database and files folder; returns {id, name}."""
        name = (name or '').strip()
        if not name or len(name) > 60:
            raise ValidationError('give the person a name (up to 60 characters)')
        base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:24] or 'person'
        with self._lock:
            if any(p.name.lower() == name.lower() for p in self.by_id.values()):
                raise ValidationError(f'{name} is already on this dashboard')
            pid, n = base, 2
            while pid in self.by_id or pid == MAIN:
                pid, n = f'{base}-{n}', n + 1
            person = self._open(pid, name)
            with self.home.lock:
                self.home.db.execute('INSERT INTO people VALUES (?, ?, ?)', (pid, name, now()))
                self.home.db.commit()
            self.by_id[pid] = person
        if self.storage:
            self.storage.person_added(person)
        return {'id': pid, 'name': name}

    def rename(self, pid, name):
        """Change a person's shown name."""
        name = (name or '').strip()
        if not name or len(name) > 60:
            raise ValidationError('give the person a name (up to 60 characters)')
        p = self.get(pid)
        with self.home.lock:
            self.home.db.execute('UPDATE people SET name=? WHERE id=?', (name, pid))
            self.home.db.commit()
        p.name = name
        if self.storage:
            self.storage.poke()    # their folder in the readable copy follows the name
        return {'id': pid, 'name': name}

    def close(self):
        if self.storage:
            self.storage.close()
        for p in self.by_id.values():
            if p.store is not self.home:
                p.store.close()

"""Where the original records are kept, and a readable, sorted copy of them.

By default each person's files sit in the dashboard's data folder, named by their sha256
(`--files` for the main person, <state>/people/<id>/files for others). People can choose another
folder on the server from the Accounts dialog (setting `files_folder`, kept in the main database),
for example a bigger disk or a shared folder. Then:

    <folder>/.healthdash-store/<person id>/<sha256>.<ext>     the dashboard's own copy (as before)
    <folder>/<Person name>/<category folder>/<date>_<place>_<title>.<ext>    the readable copy

The readable copy is made of hard links to the stored files (no extra space); where the disk can't
link (some USB and network drives) they are copies. It is kept up to date in the background as
files are added, renamed, moved or deleted. A list of what was made (`.healthdash-store/readable.json`)
means only those files are ever removed: anything else a person puts in the folder is left alone.
Changing the folder moves the stored files there (in the background) and makes the readable copy.
"""

import contextlib
import hashlib
import json
import os
import shutil
import threading
import time

from . import config
from .organize import slug, target_name

SETTING = 'files_folder'
STORE = '.healthdash-store'
MANIFEST = 'readable.json'
# never a place for records (the program's own folder is refused too)
SYSTEM = ('/bin', '/boot', '/dev', '/etc', '/lib', '/lib64', '/proc', '/root', '/run', '/sbin', '/sys',
          '/usr', '/System', '/Library', '/Applications', '/private/etc', '/private/var/db')


class StorageError(ValueError):
    pass


def check_folder(path):
    """The chosen folder, made absolute and real, or StorageError saying what's wrong. It must
    already exist, be writable by the dashboard and not be a system folder."""
    path = (path or '').strip()
    if not path:
        raise StorageError('type the folder on the server where the records should be kept')
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        raise StorageError('give the whole path, starting with / (for example /mnt/records/health)')
    real = os.path.realpath(path)
    here = os.path.realpath(os.path.dirname(os.path.dirname(__file__)))
    if real == '/' or any(real == s or real.startswith(s + '/') for s in SYSTEM) or \
            real == here or real.startswith(here + '/'):
        raise StorageError(f'{path} is a system or program folder; choose a folder for records')
    if not os.path.isdir(real):
        raise StorageError(f'there is no folder {path} on the server; make it first')
    probe = os.path.join(real, f'.healthdash-write-test-{os.getpid()}')
    try:
        with open(probe, 'w') as f:
            f.write('ok')
        os.remove(probe)
    except OSError as e:
        raise StorageError(f"the dashboard can't write to {path} ({e.strerror}). " + _write_help(real))
    return real


def _write_help(path):
    if os.path.exists('/etc/systemd/system/healthdash.service') or os.environ.get('INVOCATION_ID'):
        return (f'On a Linux server, let the service write there: run "sudo systemctl edit healthdash", add '
                f'the two lines "[Service]" and "ReadWritePaths={path}", save, then '
                f'"sudo chown healthdash {path}" and "sudo systemctl restart healthdash".')
    return 'Check that the folder belongs to the account the dashboard runs as.'


def store_dir(folder, pid):
    return os.path.join(folder, STORE, pid)


def person_folder(name):
    return slug(name or 'Person', 40) or 'Person'


def readable_name(doc):
    """<category folder>/<file name> for a document: the name it has in the records folder when it
    came from there, otherwise dated and named like the sorted records folder (organize.py)."""
    ext = (doc.get('ext') or '').lower()
    if doc.get('organized_path') and '/' in doc['organized_path']:
        folder, name = doc['organized_path'].split('/', 1)
        return folder, name.replace('/', '-')
    folder = config.CATEGORY_FOLDERS.get(doc.get('category') or 'other', config.CATEGORY_FOLDERS['other'])
    return folder, target_name({'date': doc.get('doc_date'), 'facility': doc.get('facility'),
                                'title': doc.get('title')}, ext)


def _same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _put(src, dst):
    """Hard-link src at dst, or copy it where the disk can't link. Returns 'link' or 'copy'.
    The stored file is made read-only first: a link shares it, so a program saving over the readable
    copy would otherwise change the original."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    mode = os.stat(src).st_mode
    if mode & 0o222:
        os.chmod(src, mode & ~0o222)
    try:
        os.link(src, dst)
        return 'link'
    except OSError:
        tmp = dst + '.tmp'
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        return 'copy'


def _ours(folder, rel, src, size):
    """May the readable file at `rel` be removed? Only a path inside the folder (not in the store),
    that this made from a stored file, and that is still that file (a link to it, or a copy of its
    size): the list could have been edited by anyone who can write to the folder."""
    p = os.path.normpath(os.path.join(folder, rel))
    store = os.path.join(folder, STORE)
    if os.path.isabs(rel) or not p.startswith(folder + os.sep) or p.startswith(store + os.sep) or os.path.islink(p):
        return None
    if not (isinstance(src, str) and os.path.normpath(src).startswith(store + os.sep)) or not os.path.isfile(p):
        return None
    return p if _same(p, src) or os.path.getsize(p) == size else None


def mirror(folder, people):
    """Bring the readable copy in `folder` up to date with every person's stored files.
    `people` is [(person name, store, files_dir)]. Returns {'files', 'added', 'removed', 'mode'}."""
    mpath = os.path.join(folder, STORE, MANIFEST)
    try:
        with open(mpath, encoding='utf-8') as f:
            made = json.load(f)        # {relative path: [stored file path, size]}
    except (OSError, ValueError):
        made = {}
    want, seen = {}, set()
    for name, store, files_dir in people:
        top = person_folder(name)
        for d in store.documents():
            if not d.get('has_file') or not d.get('ext'):
                continue
            src = os.path.join(files_dir, d['id'] + d['ext'])
            if not os.path.isfile(src):
                continue
            sub, fname = readable_name(d)
            stem, ext = os.path.splitext(fname)
            rel, n = f'{top}/{sub}/{fname}', 2
            while rel.lower() in seen:      # two documents with the same name
                rel, n = f'{top}/{sub}/{stem}_{n}{ext}', n + 1
            seen.add(rel.lower())
            want[rel] = src
    added = removed = 0
    mode = None
    for rel, (old_src, size) in list(made.items()):
        p = os.path.join(folder, rel)
        if want.get(rel) == old_src and os.path.exists(p) and (
                _same(p, old_src) or os.path.getsize(p) == os.path.getsize(old_src)):
            continue    # still the stored file (a page drawn again is a new file: linked again below)
        # only remove what this made and nobody has replaced since
        p = _ours(folder, rel, old_src, size)
        if p:
            os.remove(p)
            removed += 1
            _prune(folder, os.path.dirname(p))
        del made[rel]
    for rel, src in want.items():
        p = os.path.join(folder, rel)
        if rel in made and os.path.exists(p):
            continue
        if os.path.exists(p):
            continue            # something of the person's own is there: leave it
        mode = _put(src, p)
        made[rel] = [src, os.path.getsize(src)]
        added += 1
    os.makedirs(os.path.dirname(mpath), exist_ok=True)
    with open(mpath + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(made, f, indent=0, sort_keys=True)
    os.replace(mpath + '.tmp', mpath)
    return {'files': len(made), 'added': added, 'removed': removed, 'mode': mode}


def _prune(folder, d):
    """Remove folders left empty by the readable copy, up to (not including) `folder`."""
    folder = os.path.normpath(folder)
    d = os.path.normpath(d)
    while d.startswith(folder + os.sep) and d != folder:
        try:
            os.rmdir(d)
        except OSError:
            return
        d = os.path.dirname(d)


def unmirror(folder):
    """Remove the readable copy this made in `folder` (used when moving away from it)."""
    mpath = os.path.join(folder, STORE, MANIFEST)
    try:
        with open(mpath, encoding='utf-8') as f:
            made = json.load(f)
    except (OSError, ValueError):
        return 0
    n = 0
    for rel, (src, size) in made.items():
        p = _ours(folder, rel, src, size)
        if p:
            os.remove(p)
            n += 1
            _prune(folder, os.path.dirname(p))
    os.remove(mpath)
    return n


def move_files(src, dst, progress=None):
    """Move every stored file from src to dst: a rename on the same disk, otherwise a copy that is
    checked before the original is removed. Returns the number moved."""
    os.makedirs(dst, mode=0o700, exist_ok=True)
    if not os.path.isdir(src) or _same(src, dst):
        return 0
    names = [f for f in os.listdir(src) if not f.endswith('.tmp') and os.path.isfile(os.path.join(src, f))]
    for i, f in enumerate(names):
        a, b = os.path.join(src, f), os.path.join(dst, f)
        try:
            os.rename(a, b)
        except OSError:         # another disk
            shutil.copy2(a, b + '.tmp')
            if _sha(a) != _sha(b + '.tmp'):
                os.remove(b + '.tmp')
                raise StorageError(f'copying {f} to {dst} went wrong; nothing more was moved')
            os.replace(b + '.tmp', b)
            os.remove(a)
        if progress:
            progress(i + 1, len(names))
    return len(names)


def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


class Storage:
    """The files-folder setting for one dashboard: which folder each person's files are in, moving
    them when the setting changes, and keeping the readable copy current (in a background thread).
    A move takes all three locks: `lock` (shared with every upload worker: writing a file),
    `work_lock` (their reading of a file, which can be a long OCR run) and `mirror_lock`."""

    def __init__(self, people):
        self.people = people
        self.home = people.home
        self.lock = threading.RLock()
        self.work_lock = threading.RLock()
        self.mirror_lock = threading.Lock()
        self.status = {'state': 'idle'}
        self._poke = threading.Event()
        self._closed = False
        self.folder = self.home.get_setting(SETTING) or None
        people.storage = self
        for p in people.by_id.values():
            self._attach(p)
            if self.folder:
                self._use(p, store_dir(self.folder, p.id))
        threading.Thread(target=self._loop, daemon=True, name='readable-copy').start()
        self.poke()

    def _attach(self, person):
        if person.uploads:
            person.uploads.lock, person.uploads.work_lock = self.lock, self.work_lock
            person.uploads.changed = self.poke

    def default_for(self, pid):
        return self.people.default_files(pid)

    def dir_for(self, pid):
        return store_dir(self.folder, pid) if self.folder else self.default_for(pid)

    def _use(self, person, d):
        if self.folder and os.path.isdir(self.folder):
            os.makedirs(d, mode=0o700, exist_ok=True)
        person.files_dir = d
        if person.uploads:
            person.uploads.files_dir = d
            self._attach(person)

    def available(self):
        return not self.folder or os.path.isdir(self.folder)

    def info(self):
        return {'folder': self.folder, 'default': self.default_for('main'), 'available': self.available(),
                'status': dict(self.status)}

    def set_folder(self, path):
        """Start moving everyone's files to `path` ('' or None: back to the data folder). Checks the
        folder first (StorageError); the move runs in the background (see info()['status'])."""
        new = check_folder(path) if (path or '').strip() else None
        if self.status.get('state') == 'moving':
            raise StorageError('files are being moved already; wait for that to finish')
        if new == self.folder:
            return self.info()
        if new:
            for p in self.people.by_id.values():
                cur = os.path.realpath(p.files_dir)
                if new == cur or new.startswith(cur + os.sep):
                    raise StorageError("choose a folder outside the dashboard's own files folder")
        self.status = {'state': 'moving', 'done': 0, 'total': 0, 'to': new or self.default_for('main')}
        threading.Thread(target=self._move, args=(new,), daemon=True, name='move-files').start()
        return self.info()

    def _move(self, new):
        old, moved = self.folder, []
        with self.work_lock, self.mirror_lock, self.lock:
            people = list(self.people.by_id.values())
            try:
                total = sum(len(os.listdir(p.files_dir)) for p in people if os.path.isdir(p.files_dir))
                self.status.update(total=total)
                done = [0]

                def step(_i, _n):
                    done[0] += 1
                    self.status['done'] = done[0]
                for p in people:
                    dst = store_dir(new, p.id) if new else self.default_for(p.id)
                    moved.append((p.files_dir, dst))
                    move_files(p.files_dir, dst, step)
                if old and os.path.isdir(old):
                    unmirror(old)
                    for src, _dst in moved:     # the empty store folders left behind
                        with contextlib.suppress(OSError):
                            os.rmdir(src)
                    with contextlib.suppress(OSError):
                        os.rmdir(os.path.join(old, STORE))
                self.folder = new
                self.home.set_setting(SETTING, new or '')
                for p in people:
                    self._use(p, self.dir_for(p.id))
                self.status = {'state': 'done', 'moved': done[0], 'at': time.time()}
            except Exception as e:  # noqa: BLE001 - put back what moved, and say what happened
                back = ''
                for src, dst in reversed(moved):
                    try:
                        move_files(dst, src)
                    except Exception:  # noqa: BLE001
                        back = f' Some files are still in {dst}; the dashboard looks in {src}.'
                self.status = {'state': 'error', 'error': f"{e}. Nothing was changed.{back}" if not back else f'{e}.{back}'}
        self.poke()

    def person_added(self, person):
        """A new person: their files go in the chosen folder too."""
        self._attach(person)
        if self.folder:
            self._use(person, store_dir(self.folder, person.id))
        self.poke()

    def poke(self):
        """Something changed: bring the readable copy up to date soon."""
        self._poke.set()

    def close(self):
        """Stop the background thread (before the databases close)."""
        self._closed = True
        self._poke.set()
        with self.mirror_lock:      # let a pass that is running finish first
            pass

    def _loop(self):
        while not self._closed:
            self._poke.wait()
            time.sleep(2)           # gather a burst of changes (a zip of records) into one pass
            self._poke.clear()
            if not self._closed:
                self.sync()

    def sync(self):
        """Bring the readable copy up to date now (the background thread does this after changes)."""
        if not self.folder or not os.path.isdir(self.folder) or self.status.get('state') == 'moving':
            return None
        try:
            with self.mirror_lock:
                if self._closed:
                    return None
                r = mirror(self.folder, [(p.name, p.store, p.files_dir) for p in self.people.by_id.values()])
            self.status = {**{k: v for k, v in self.status.items() if k in ('state', 'moved', 'at')},
                           'readable': r['files'], 'mode': r['mode'] or self.status.get('mode')}
            return r
        except Exception as e:  # noqa: BLE001 - the records are safe in the store; say what failed
            self.status = {'state': 'error', 'error': f"couldn't update the readable copy: {e}"}
            return None

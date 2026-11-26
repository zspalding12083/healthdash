"""Simple sign-in: a few accounts, all with the same access. Optional.

Sign-in is off on a new dashboard: anyone who can reach it on the home network can use it, as
before accounts existed. It is turned on from the Accounts dialog (which makes the first account at
the same time) or with `python3 -m healthdash login on --db DB`, and off again the same ways. The
choice is kept in the database (setting `login_required`); a dashboard that has accounts but no
saved choice (one set up before sign-in was optional) keeps requiring sign-in.

Passwords are stored as salted PBKDF2-SHA256 hashes. A sign-in gives a random session token
(kept in an HttpOnly, SameSite=Strict cookie); only its SHA-256 is stored. Repeated wrong
passwords from one address are slowed down.

This keeps casual visitors on a home network out. It is not built for the open internet: the
dashboard speaks plain HTTP, so passwords cross the local network unencrypted.
"""

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time

from .store import ValidationError, now

ITERATIONS = 310_000
SESSION_DAYS = 30
COOKIE = 'hd_session'
MAX_FAILS, LOCK_SECONDS = 5, 60
NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,31}$')
MIN_PASSWORD = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT NOT NULL, created TEXT);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
                                     created REAL NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys (id TEXT PRIMARY KEY, key_hash TEXT UNIQUE NOT NULL, username TEXT NOT NULL,
                                     label TEXT, created TEXT, last_used TEXT);
"""
KEY_PREFIX = 'hdk_'
# owner of plugin keys made while sign-in is off; not a valid user name, so no account can claim them
NO_LOGIN_OWNER = '(no sign-in)'


def hash_password(password, salt=None, iterations=ITERATIONS):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations)
    return f'pbkdf2_sha256${iterations}${salt.hex()}${h.hex()}'


def check_password(password, stored):
    try:
        alg, it, salt, h = stored.split('$')
    except (AttributeError, ValueError):
        return False
    calc = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(it))
    return hmac.compare_digest(calc.hex(), h)


_DUMMY = hash_password('not a real password', iterations=ITERATIONS)


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    """Sign-in accounts, browser sessions and plugin keys, stored in the dashboard database.
    Passwords and keys are kept only as salted hashes."""
    def __init__(self, store):
        self.store = store
        self.db = store.db
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._fails = {}
        self._lock = threading.Lock()

    # ---- is sign-in on? -------------------------------------------------------------------

    def required(self):
        """Must people sign in? The saved choice; without one, yes if any account exists."""
        v = self.store.get_setting('login_required')
        return v == '1' if v in ('0', '1') else self.count() > 0

    def set_required(self, on):
        """Turn sign-in on or off. On needs an account to sign in with; off signs everyone out."""
        if on and not self.count():
            raise ValidationError('make an account first, to sign in with')
        with self.store.lock:
            if not on:
                self.db.execute('DELETE FROM sessions')
            self.db.commit()
        self.store.set_setting('login_required', '1' if on else '0')

    # ---- accounts -----------------------------------------------------------------------

    def users(self):
        return [{'username': r[0], 'created': r[1]}
                for r in self.db.execute('SELECT username, created FROM users ORDER BY username')]

    def count(self):
        return self.db.execute('SELECT COUNT(*) FROM users').fetchone()[0]

    @staticmethod
    def _check(username, password):
        username = (username or '').strip().lower()
        if not NAME_RE.match(username):
            raise ValidationError('user name: letters, numbers, . _ - (up to 32)')
        if len(password or '') < MIN_PASSWORD:
            raise ValidationError(f'password must be at least {MIN_PASSWORD} characters')
        return username

    def add_user(self, username, password):
        """Create an account; the name is lower-cased. Raises ValidationError if it exists or is too weak."""
        username = self._check(username, password)
        try:
            with self.store.lock:
                self.db.execute('INSERT INTO users (username, password, created) VALUES (?, ?, ?)',
                                (username, hash_password(password), now()))
                self.db.commit()
        except sqlite3.IntegrityError:
            raise ValidationError(f'{username} already exists')
        return username

    def set_password(self, username, password):
        """Change a password and sign that account out everywhere."""
        username = self._check(username, password)
        with self.store.lock:
            n = self.db.execute('UPDATE users SET password=? WHERE username=?',
                                (hash_password(password), username)).rowcount
            self.db.execute('DELETE FROM sessions WHERE username=?', (username,))  # sign out everywhere
            self.db.commit()
        if not n:
            raise ValidationError(f'no user {username}')

    def remove_user(self, username):
        """Delete an account and its sessions and keys; the last account cannot be removed."""
        username = (username or '').lower()
        with self.store.lock:
            if self.count() <= 1:
                raise ValidationError("can't remove the last account")
            n = self.db.execute('DELETE FROM users WHERE username=?', (username,)).rowcount
            self.db.execute('DELETE FROM sessions WHERE username=?', (username,))
            self.db.execute('DELETE FROM api_keys WHERE username=?', (username,))
            self.db.commit()
        if not n:
            raise ValidationError(f'no user {username}')

    # ---- sessions -----------------------------------------------------------------------

    def locked_for(self, ip):
        with self._lock:
            fails, until = self._fails.get(ip, (0, 0))
        return max(0, int(until - time.time()))

    def _failed(self, ip):
        with self._lock:
            fails, _ = self._fails.get(ip, (0, 0))
            fails += 1
            self._fails[ip] = (fails, time.time() + LOCK_SECONDS if fails >= MAX_FAILS else 0)

    def login(self, username, password, ip='?'):
        """A new session token, or ValidationError."""
        wait = self.locked_for(ip)
        if wait:
            raise ValidationError(f'too many wrong passwords; try again in {wait} seconds')
        username = (username or '').strip().lower()
        row = self.db.execute('SELECT password FROM users WHERE username=?', (username,)).fetchone()
        ok = check_password(password or '', row[0] if row else _DUMMY) and row is not None
        if not ok:
            self._failed(ip)
            raise ValidationError('wrong user name or password')
        with self._lock:
            self._fails.pop(ip, None)
        token = secrets.token_urlsafe(32)
        t = time.time()
        with self.store.lock:
            self.db.execute('DELETE FROM sessions WHERE expires < ?', (t,))
            self.db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?)',
                            (_token_hash(token), username, t, t + SESSION_DAYS * 86400))
            self.db.commit()
        return token

    def session_user(self, token):
        if not token:
            return None
        row = self.db.execute('SELECT s.username FROM sessions s JOIN users u ON u.username=s.username '
                              'WHERE s.token_hash=? AND s.expires>?', (_token_hash(token), time.time())).fetchone()
        return row[0] if row else None

    def logout(self, token):
        if token:
            with self.store.lock:
                self.db.execute('DELETE FROM sessions WHERE token_hash=?', (_token_hash(token),))
                self.db.commit()

    # ---- plugin keys: only allowed to add files and check on them (see server.KEY_ROUTES) ----

    def create_key(self, username, label=''):
        kid, key = secrets.token_hex(6), KEY_PREFIX + secrets.token_urlsafe(32)
        with self.store.lock:
            self.db.execute('INSERT INTO api_keys VALUES (?, ?, ?, ?, ?, NULL)',
                            (kid, _token_hash(key), username, (label or 'Browser plugin')[:60], now()))
            self.db.commit()
        return {'id': kid, 'key': key}

    def keys(self):
        return [{'id': r[0], 'username': r[1], 'label': r[2], 'created': r[3], 'last_used': r[4]}
                for r in self.db.execute('SELECT id, username, label, created, last_used FROM api_keys ORDER BY created')]

    def revoke_key(self, kid):
        with self.store.lock:
            n = self.db.execute('DELETE FROM api_keys WHERE id=?', (kid,)).rowcount
            self.db.commit()
        return n > 0

    def key_user(self, key):
        """The account a plugin key belongs to (and note when it was last used), or None. A key made
        while sign-in was off (or whose account was removed) only works while sign-in is off."""
        if not key or not key.startswith(KEY_PREFIX):
            return None
        row = self.db.execute('SELECT k.id, k.username, u.username FROM api_keys k LEFT JOIN users u '
                              'ON u.username=k.username WHERE k.key_hash=?', (_token_hash(key),)).fetchone()
        if not row or (row[2] is None and self.required()):
            return None
        with self.store.lock:
            self.db.execute('UPDATE api_keys SET last_used=? WHERE id=?', (now(), row[0]))
            self.db.commit()
        return row[1]

    @staticmethod
    def cookie(token, secure=False):
        if token is None:
            return f'{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'
        return (f'{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}'
                + ('; Secure' if secure else ''))

"""Check that this machine has what healthdash needs, and say how to fix what's missing.

  python3 -m healthdash check            the dashboard server (default)
  python3 -m healthdash check --ingest   the machine that reads the records folder

Exit code 0 when everything required is present, 1 otherwise.
"""

import shutil
import sqlite3
import subprocess
import sys

APT = {'pdftotext': 'poppler-utils', 'pdfinfo': 'poppler-utils', 'pdftoppm': 'poppler-utils',
       'tesseract': 'tesseract-ocr', 'soffice': 'libreoffice-writer'}
UPLOAD_TOOLS = ['pdftotext', 'pdfinfo', 'pdftoppm', 'tesseract']


def _fts5():
    try:
        sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)')
        return True
    except sqlite3.Error:
        return False


def _tesseract_english():
    if not shutil.which('tesseract'):
        return False
    try:
        out = subprocess.run(['tesseract', '--list-langs'], capture_output=True, timeout=20)
        return b'eng' in out.stdout + out.stderr
    except (OSError, subprocess.SubprocessError):
        return False


def missing_packages(tools):
    return sorted({APT[t] for t in tools if not shutil.which(t)})


def checks(role='server'):
    """[(name, ok, required, detail)]"""
    out = []
    v = sys.version_info
    out.append(('Python 3.8 or newer', v >= (3, 8), True, f'found {v.major}.{v.minor}'))
    out.append(('SQLite full-text search (FTS5)', _fts5(), True, f'SQLite {sqlite3.sqlite_version}'))
    for t in UPLOAD_TOOLS:
        out.append((f'{t} (reading {"scans" if t == "tesseract" else "PDFs"})', bool(shutil.which(t)), True,
                    f'package {APT[t]}'))
    if shutil.which('tesseract'):
        out.append(('tesseract English language data', _tesseract_english(), True, 'package tesseract-ocr-eng'))
    if role == 'ingest':
        out.append(('soffice (old Word .doc files)', bool(shutil.which('soffice')), False,
                    f'package {APT["soffice"]}; optional'))
    return out


def upload_status():
    """What the dashboard page needs to know before someone uploads."""
    missing = [t for t in UPLOAD_TOOLS if not shutil.which(t)]
    if shutil.which('tesseract') and not _tesseract_english():
        missing.append('tesseract English data')
    pkgs = missing_packages([t for t in missing if t in APT])
    return {'can_read': not missing, 'missing': missing,
            'fix': f"sudo apt-get install -y {' '.join(pkgs or ['tesseract-ocr-eng'])}" if missing else None}


def main(role='server'):
    rows = checks(role)
    bad = False
    for name, ok, required, detail in rows:
        mark = 'ok  ' if ok else ('MISSING' if required else 'optional')
        print(f'  {mark:8} {name}  ({detail})')
        bad |= required and not ok
    pkgs = missing_packages([t for t in APT if t in UPLOAD_TOOLS or role == 'ingest'])
    if role != 'ingest':
        pkgs = [p for p in pkgs if p != 'libreoffice-writer']
    if pkgs:
        print(f"\nTo fix (Debian/Ubuntu/Raspberry Pi OS):\n  sudo apt-get install -y {' '.join(pkgs)}")
    if not _fts5():
        print('\nThis Python was built without SQLite FTS5; use the distribution python3 package.')
    print('\nEverything required is present.' if not bad else '\nSome required pieces are missing.')
    return 1 if bad else 0

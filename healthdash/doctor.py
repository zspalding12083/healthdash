"""Check that this machine has what healthdash needs, and say how to fix what's missing.

  python3 -m healthdash check            the dashboard (default)
  python3 -m healthdash check --ingest   the machine that reads the records folder

Exit code 0 when everything required is present, 1 otherwise.
"""

import os
import shutil
import sqlite3
import subprocess
import sys

UPLOAD_TOOLS = ['pdftotext', 'pdfinfo', 'pdftoppm', 'tesseract']
# tool -> package name for each package manager
PACKAGES = {
    'apt': {'pdftotext': 'poppler-utils', 'pdfinfo': 'poppler-utils', 'pdftoppm': 'poppler-utils',
            'tesseract': 'tesseract-ocr', 'eng': 'tesseract-ocr-eng', 'soffice': 'libreoffice-writer'},
    'brew': {'pdftotext': 'poppler', 'pdfinfo': 'poppler', 'pdftoppm': 'poppler',
             'tesseract': 'tesseract', 'eng': 'tesseract', 'soffice': '--cask libreoffice'},
    'dnf': {'pdftotext': 'poppler-utils', 'pdfinfo': 'poppler-utils', 'pdftoppm': 'poppler-utils',
            'tesseract': 'tesseract', 'eng': 'tesseract-langpack-eng', 'soffice': 'libreoffice-writer'},
}
APT = PACKAGES['apt']  # kept for callers that only know Debian names
MAC_SOFFICE = '/Applications/LibreOffice.app/Contents/MacOS/soffice'


def find_soffice():
    """LibreOffice's command; on a Mac it lives inside the app bundle, not on PATH."""
    return shutil.which('soffice') or (MAC_SOFFICE if os.path.exists(MAC_SOFFICE) else None)


def _have(tool):
    return bool(find_soffice() if tool == 'soffice' else shutil.which(tool))


def package_manager():
    if sys.platform == 'darwin':
        return 'brew'
    for pm in ('apt', 'dnf'):
        if shutil.which('apt-get' if pm == 'apt' else pm):
            return pm
    return None


def install_command(tools, pm=None):
    """The command that installs these tools ('eng' = tesseract English data)."""
    pm = pm or package_manager()
    if not tools:
        return None
    if pm is None:
        return 'install poppler (pdftotext, pdfinfo, pdftoppm) and tesseract with English data'
    pkgs = []
    for t in tools:
        p = PACKAGES[pm][t]
        if p not in pkgs:
            pkgs.append(p)
    casks = [p for p in pkgs if p.startswith('--cask')]
    plain = [p for p in pkgs if not p.startswith('--cask')]
    if pm == 'brew':
        cmds = (['brew install ' + ' '.join(plain)] if plain else []) + ['brew install ' + c for c in casks]
        return ' && '.join(cmds)
    prefix = 'sudo apt-get install -y ' if pm == 'apt' else 'sudo dnf install -y '
    return prefix + ' '.join(plain)


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


def missing_tools(role='server'):
    missing = [t for t in UPLOAD_TOOLS if not _have(t)]
    if shutil.which('tesseract') and not _tesseract_english():
        missing.append('eng')
    if role == 'ingest' and not _have('soffice'):
        missing.append('soffice')
    return missing


def checks(role='server'):
    """[(name, ok, required, detail)]"""
    out = []
    v = sys.version_info
    out.append(('Python 3.8 or newer', v >= (3, 8), True, f'found {v.major}.{v.minor}'))
    out.append(('SQLite full-text search (FTS5)', _fts5(), True, f'SQLite {sqlite3.sqlite_version}'))
    pm = package_manager() or 'apt'
    for t in UPLOAD_TOOLS:
        out.append((f'{t} (reading {"scans" if t == "tesseract" else "PDFs"})', _have(t), True,
                    f'package {PACKAGES[pm][t]}'))
    if shutil.which('tesseract'):
        out.append(('tesseract English language data', _tesseract_english(), True,
                    f'package {PACKAGES[pm]["eng"]}'))
    if role == 'ingest':
        out.append(('LibreOffice (old Word .doc files)', _have('soffice'), False,
                    f'{PACKAGES[pm]["soffice"]}; optional'))
    return out


def upload_status():
    """What the dashboard page needs to know before someone uploads."""
    missing = [t for t in missing_tools() if t != 'soffice']
    names = [('tesseract English data' if t == 'eng' else t) for t in missing]
    return {'can_read': not missing, 'missing': names, 'fix': install_command(missing)}


def main(role='server'):
    rows = checks(role)
    bad = False
    for name, ok, required, detail in rows:
        mark = 'ok' if ok else ('MISSING' if required else 'optional')
        print(f'  {mark:8} {name}  ({detail})')
        bad |= required and not ok
    missing = missing_tools(role)
    required = [t for t in missing if t != 'soffice']
    if required:
        print(f'\nTo install what is missing:\n  {install_command(required)}')
    if 'soffice' in missing:
        print(f'\nOptional, for old Word .doc files:\n  {install_command(["soffice"])}')
    if sys.platform == 'darwin' and required and not shutil.which('brew'):
        print('  (needs Homebrew: https://brew.sh)')
    if not _fts5():
        print('\nThis Python was built without SQLite FTS5; install Python from python.org, Homebrew or your'
              ' distribution.')
    print('\nEverything required is present.' if not bad else '\nSome required pieces are missing.')
    return 1 if bad else 0

"""The catalog: what each source file is (title, date, category, facility).

data/catalog.json maps a file's path relative to the source folder to its metadata:

  {"files": {"CT Head 01-15-2024.pdf": {"title": "CT head", "date": "2024-01-15",
             "category": "imaging", "facility": "Sample General Hospital", "summary": "..."}},
   "discs": {"MRI discs/SPINE_MRI_2017": {...same fields...}}}

Files not in the catalog get a best guess from the file name and are flagged for review.
"""

import json
import os
import re

from . import config

_DATE_PATTERNS = [
    (re.compile(r'(?<!\d)(20\d{2}|19\d{2})[_-](\d{2})[_-](\d{2})(?!\d)'), ('y', 'm', 'd')),
    (re.compile(r'(?<!\d)(\d{2})[_-](\d{2})[_-](20\d{2}|19\d{2})(?!\d)'), ('m', 'd', 'y')),
    (re.compile(r'(?<!\d)(\d{1,2})_(\d{2})_(20\d{2})(?!\d)'), ('m', 'd', 'y')),
    (re.compile(r'(?<!\d)(\d{2})(\d{2})(20\d{2}|19\d{2})(?!\d)'), ('m', 'd', 'y')),
    (re.compile(r'(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)'), ('y', 'm', 'd')),
    (re.compile(r'(?<!\d)(20\d{2})[_-](\d{2})(?!\d)'), ('y', 'm')),
    (re.compile(r'(?<!\d)(20\d{2})(?!\d)'), ('y',)),
]


def guess_date(name):
    for rx, order in _DATE_PATTERNS:
        m = rx.search(name)
        if not m:
            continue
        parts = dict(zip(order, m.groups()))
        y, mo, d = parts.get('y'), parts.get('m'), parts.get('d')
        if mo and not 1 <= int(mo) <= 12:
            continue
        if d and not 1 <= int(d) <= 31:
            continue
        return '-'.join(p.zfill(2) for p in (y, mo, d) if p)
    return None


def guess_title(path):
    base = os.path.splitext(os.path.basename(path))[0]
    base = re.sub(r'[_\-]+', ' ', base)
    base = re.sub(r'\b\d{1,8}\b', ' ', base)
    if config.NAME_WORDS:
        words = '|'.join(re.escape(w) for w in config.NAME_WORDS)
        base = re.sub(rf'\b({words})\b', ' ', base, flags=re.I)
    return re.sub(r'\s+', ' ', base).strip().capitalize() or os.path.basename(path)


def load(path=None):
    path = path or config.CATALOG
    if not os.path.exists(path):
        return {'files': {}, 'discs': {}}
    with open(path, encoding='utf-8') as f:
        cat = json.load(f)
    cat.setdefault('files', {})
    cat.setdefault('discs', {})
    return cat


def describe(cat, relpath, disc=False):
    """Metadata for a file or disc: from the catalog, else guessed and marked needs_review."""
    entry = cat['discs' if disc else 'files'].get(relpath)
    if entry:
        out = dict(entry)
        out.setdefault('needs_review', False)
        return out
    return {'title': guess_title(relpath), 'date': guess_date(relpath),
            'category': 'disc' if disc else 'other', 'facility': None, 'needs_review': True}

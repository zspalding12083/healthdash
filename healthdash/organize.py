"""Build a sorted, consistently named copy of the source records.

The originals are never touched. The copy is made of hard links (same disk, no extra space):

  <dest>/<category folder>/<YYYY-MM-DD>_<Facility>_<Title>.<ext>
  <dest>/12 Imaging discs/<YYYY-MM-DD>_<Facility>_<Title>/...   (whole disc folder)
  <dest>/_rename_map.csv   new path -> original path

Re-running is safe: it only adds links for files that are new or whose name changed, and
removes links in <dest> that no longer correspond to a source file.
"""

import csv
import os
import re

from . import catalog, config
from .extract import walk

UNSAFE = re.compile(r'[^A-Za-z0-9.&+\-]+')


def slug(text, limit=60):
    text = (text or '').replace('/', '-').strip()
    text = UNSAFE.sub('-', text).strip('-')
    text = re.sub(r'-{2,}', '-', text)
    return text[:limit].rstrip('-')




def short_facility(name):
    for needle, short in config.FACILITY_SHORT:
        if needle.lower() in name.lower():
            return short
    return slug(name.split('(')[0], 24)


def target_name(meta, ext):
    parts = [meta.get('date') or 'undated']
    if meta.get('facility'):
        parts.append(short_facility(meta['facility']))
    parts.append(slug(meta.get('title') or 'Document'))
    return '_'.join(p for p in parts if p) + ext.lower()


def plan(source, cat=None):
    """Return [(category_folder, new_name, original_relpath, is_disc)] with unique names."""
    cat = cat or catalog.load()
    seen, out = set(), []
    for kind, rel in walk(source):
        disc = kind == 'disc'
        meta = catalog.describe(cat, rel, disc=disc)
        folder = config.CATEGORY_FOLDERS.get('disc' if disc else meta.get('category') or 'other',
                                             config.CATEGORY_FOLDERS['other'])
        ext = '' if disc else os.path.splitext(rel)[1]
        name = target_name(meta, ext)
        stem, n = name[:len(name) - len(ext)] if ext else name, 2
        while (folder, name.lower()) in seen:
            name = f'{stem}_{n}{ext.lower()}'
            n += 1
        seen.add((folder, name.lower()))
        out.append((folder, name, rel, disc))
    return out


def _link_tree(src, dst):
    for dirpath, _, files in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for f in files:
            d = os.path.join(dst, rel, f)
            if not os.path.exists(d):
                os.link(os.path.join(dirpath, f), d)


def organize(source, dest, apply=False, log=print):
    if os.path.realpath(source) == os.path.realpath(dest):
        log('The records folder is already the sorted copy; nothing to organize.')
        return []
    rows = plan(source)
    if not apply:
        for folder, name, rel, disc in rows:
            log(f'{folder}/{name}  <-  {rel}')
        log(f'\n{len(rows)} items. Preview only; add --apply to create the links in {dest}')
        return rows
    os.makedirs(dest, exist_ok=True)
    wanted = set()
    for folder, name, rel, disc in rows:
        src = os.path.join(source, rel)
        dst = os.path.join(dest, folder, name)
        wanted.add(os.path.normpath(dst))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if disc:
            _link_tree(src, dst)
        elif not os.path.exists(dst):
            os.link(src, dst)
        elif not os.path.samefile(src, dst):
            os.remove(dst)
            os.link(src, dst)
    # drop links that no longer match the plan (only hard links of source files, nothing else)
    for folder in set(r[0] for r in rows) | set(config.CATEGORY_FOLDERS.values()):
        top = os.path.join(dest, folder)
        if not os.path.isdir(top):
            continue
        for entry in os.listdir(top):
            p = os.path.normpath(os.path.join(top, entry))
            if p not in wanted and os.path.isfile(p) and os.stat(p).st_nlink > 1:
                os.remove(p)
    with open(os.path.join(dest, '_rename_map.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['organized path', 'original path (in the source folder)'])
        for folder, name, rel, disc in sorted(rows):
            w.writerow([f'{folder}/{name}' + ('/' if disc else ''), rel + ('/' if disc else '')])
    log(f'{len(rows)} items linked into {dest}')
    return rows


def organized_paths(source):
    """{original relpath: organized relpath} for the ingest step."""
    return {rel: f'{folder}/{name}' for folder, name, rel, disc in plan(source)}

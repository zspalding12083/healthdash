"""Paths, hosts and personal settings.

Anything specific to one person (their name, their doctors' offices, where their files live)
belongs in data/local.json, which is git-ignored. See sample/local.example.json. Environment
variables override both.
"""

import json
import os

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Private, never committed (see .gitignore):
DATA = os.environ.get('HEALTHDASH_DATA', os.path.join(PROJECT, 'data'))
EXTRACTED = os.path.join(PROJECT, 'extracted')        # text cache, one JSON per file (by sha256)
CATALOG = os.path.join(DATA, 'catalog.json')          # per-file title / date / category / facility
RECORDS = os.path.join(DATA, 'records.json')          # structured history, {kind: [records]}
ALIASES = os.path.join(DATA, 'path_aliases.json')     # {old path: new path} after files were moved or renamed
LOCAL = os.path.join(DATA, 'local.json')              # personal settings below


def _local():
    try:
        with open(LOCAL, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


_L = _local()

SOURCE = os.environ.get('HEALTHDASH_SOURCE', os.path.expanduser(_L.get('source', '~/health-records/source')))
ORGANIZED = os.environ.get('HEALTHDASH_ORGANIZED',
                           os.path.expanduser(_L.get('organized', '~/health-records/organized')))
PI_URL = os.environ.get('HEALTHDASH_PI', _L.get('dashboard_url', 'http://127.0.0.1:9380'))
TITLE = _L.get('title', 'Health Records')

# Words dropped when guessing a title from a file name (the patient's own name, usually)
NAME_WORDS = [w.lower() for w in _L.get('name_words', [])]
# Lines in lab printouts that start with these words are page furniture, not results
NOISE_LINES = _L.get('noise_lines', [])
# (text found in a facility name, short name used in organized file names)
FACILITY_SHORT = [tuple(p) for p in _L.get('facility_short', [])]
# {name to show: [other ways the records write that place]}
PLACE_NAMES = _L.get('place_names', {})
# more over-the-counter drug names, and patterns (like a strength) that make one a prescription
OTC = _L.get('otc', {})

# Folder for each document category in the organized copy
CATEGORY_FOLDERS = {
    'hospital': '01 Hospital and ER',
    'office': '02 Office visits and consults',
    'imaging': '03 Imaging reports',
    'labs': '04 Labs',
    'procedures': '05 Surgery and procedures',
    'tests': '06 Other tests',
    'eye': '07 Eye',
    'dental': '08 Dental',
    'medications': '09 Medications',
    'financial': '10 Financial',
    'legal': '11 Legal and admin',
    'disc': '12 Imaging discs',
    'other': '13 Other',
}

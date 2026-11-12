#!/usr/bin/env bash
# Copy the shareable part of this project into DEST (its own git repo) for publishing.
#
#   tools/export_public.sh DEST
#
# Only an allowlist of paths is copied. Then every copied file is searched for private terms
# (names, facilities, doctors, medications, dates of care) taken from data/ and from
# data/private_terms.txt (one term per line, both git-ignored). Any hit stops the export.
set -euo pipefail

DEST=${1:?usage: tools/export_public.sh DEST}
HERE=$(cd "$(dirname "$0")/.." && pwd)
cd "$HERE"

ALLOW=(README.md LICENSE.md .gitignore browser-extension healthdash tests sample docs/screenshot.jpg tools/dump_text.py tools/export_public.sh docs/medications.png docs/lab-trends.png docs/needs-review.png docs/logo.svg docs/social-preview.png
       deploy/install.sh deploy/sync.sh deploy/healthdash.service deploy/local.env.example deploy/add-person.sh deploy/unit.py)

mkdir -p "$DEST"
rsync -a --delete --relative --exclude __pycache__ --exclude '*.pyc' --exclude .git \
      --filter='P .git' "${ALLOW[@]}" "$DEST/"

TERMS=$(mktemp)
trap 'rm -f "$TERMS"' EXIT
python3 - "$TERMS" <<'EOF'
import json, os, re, sys
terms = set()
def add(s, min_len=4):
    s = (s or '').strip()
    if len(s) >= min_len:
        terms.add(s.lower())
# common words that occur in place/doctor names and in ordinary code or docs
COMMON = set('''hospital medical center centre health family medicine attending general clinic
practice associates group radiology imaging street services urgent care emergency department
doctor nurse office surgery surgical regional memorial community physicians specialists
the and of for with dental eye vision lab labs laboratory network system plan insurance
headless town illness breast critical ambulance institute quest''' .split())   # quest: Quest Diagnostics, a national lab
def add_name(s):
    add(s)
    for w in re.findall(r"[A-Za-z][A-Za-z'\-]{3,}", s or ''):
        if w.lower() not in COMMON:
            add(w)
L = {}
if os.path.exists('data/local.json'):
    L = json.load(open('data/local.json'))
    add(L.get('title')); [add(w) for w in L.get('name_words', [])]; [add(w) for w in L.get('noise_lines', [])]
    for needle, short in L.get('facility_short', []):
        add_name(needle); add(short)
    for k in ('source', 'organized'):
        if L.get(k):
            add(os.path.basename(L[k].rstrip('/')))
if os.path.exists('data/catalog.json'):
    cat = json.load(open('data/catalog.json'))
    for section in cat.values():
        for e in section.values():
            add_name(e.get('facility'))
if os.path.exists('data/records.json'):
    R = json.load(open('data/records.json'))
    for r in R.get('providers', []):
        add_name(r.get('name')); add_name(r.get('facility'))
    for kind in ('encounters', 'tests', 'procedures', 'labs', 'imaging', 'financial'):
        for r in R.get(kind, []):
            add_name(r.get('facility')); add_name(r.get('payee'))
            for f in ('provider',):
                for name in re.findall(r'Dr\.? ([A-Z][a-z]+(?: [A-Z][a-z]+)+)', r.get(f) or ''):
                    add_name(name)
            d = r.get('date') or ''
            if kind != 'labs' and re.fullmatch(r'\d{4}-\d{2}-\d{2}', d):
                y, m, dd = d.split('-')
                for fmt in (d, f'{y}_{m}_{dd}', f'{m}_{dd}_{y}', f'{m}-{dd}-{y}', f'{m}{dd}{y}', f'{y}{m}{dd}'):
                    terms.add(fmt)
    for r in R.get('medications', []):
        add(r.get('name'))
if os.path.exists('data/private_terms.txt'):
    for line in open('data/private_terms.txt'):
        add(line.split('#')[0], 3)
open(sys.argv[1], 'w').write('\n'.join(sorted(terms)) + '\n')
print(f'leak check: {len(terms)} private terms', file=sys.stderr)
EOF

[ -s "$TERMS" ] || { echo "no private terms found; refusing to export without a leak check"; exit 1; }
if hits=$(grep -rniwF -f "$TERMS" --exclude-dir=.git "$DEST"); then
    echo "PRIVATE TERMS FOUND in $DEST; nothing should be published until these are gone:"
    echo "$hits" | cut -c1-200
    exit 1
fi
# nothing that looks like records data
if find "$DEST" -path "$DEST/.git" -prune -o \( -iname '*.pdf' -o -iname '*.db' -o -iname '*.tif' -o -iname '*.doc' \
        -o -iname '*.wav' -o -iname '*.iso' -o -name 'local.env' -o -name 'local.json' \) -print | grep -q .; then
    echo "record-like files found in $DEST"; exit 1
fi
echo "exported to $DEST; leak check clean"

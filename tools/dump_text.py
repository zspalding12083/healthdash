# dev helper: print condensed extracted text for files matching substrings; --dedupe drops lines seen before
import json, re, sys
args = [a for a in sys.argv[1:] if not a.startswith('--')]
dedupe = '--dedupe' in sys.argv
seen = set()
m = json.load(open('extracted/manifest.json'))
for e in m:
    if e['kind'] != 'file' or not e.get('sha256'): continue
    if not any(s in e['path'] for s in args): continue
    x = json.load(open('extracted/' + e['sha256'] + '.json'))
    t = '\n'.join(x['pages'])
    t = re.sub(r'https?://\S+', '', t)
    t = re.sub(r'[ \t]{2,}', '  ', t)
    out = []
    for line in t.splitlines():
        s = line.strip()
        if not s: continue
        if dedupe:
            k = s
            if k in seen and len(s) > 3: continue
            seen.add(k)
        out.append(s)
    print(f'##### {e["path"]}  (ocr pages {x["ocr_pages"]})\n' + '\n'.join(out) + '\n')

"""Checks that look over the records for likely mistakes and ask about them on the Review tab.

Nothing here changes a record by itself: each check is a question with answers. Answering
"same" merges and teaches the name, so later imports group it without asking; answering
"not the same" (or "still taking") is remembered, so the question doesn't come back.

- Medications still listed as current that look stale: a short supply (8 tablets, no refills)
  that ran out long ago, or a prescription later summaries stopped listing.
- Conditions, lab tests and medications that look like the same thing under two names.
- Places that look like a hospital department ("Respiratory Therapy Hospital").
"""

import difflib
import hashlib
import json
import math
import re
from datetime import date, timedelta

DISMISSED = 'checks_dismissed'
LAB_ALIAS = 'lab_alias'
CONDITION_ALIAS = 'condition_alias'
STALE_AFTER = 365          # days a prescription can go unlisted while newer summaries exist
WORDS = re.compile(r"[a-z0-9%#']+")
FILLER = {'of', 'the', 'and', 'with', 'without', 'in', 'on', 'to', 'a', 'an', 'nos', 'unspecified', 'type',
          'history', 'hx', 'h/o', 'status', 'post', 's/p', 'by', 'for', 'or', 'due'}
# words that make two otherwise alike names different things
TELLING = {'urine', 'ur', 'ua', 'csf', 'bf', 'fluid', 'serum', 'plasma', 'art', 'arterial', 'venous', 'capillary',
           'oral', 'axillary', 'temporal', 'tympanic', 'rectal', 'artery', 'ideal', 'usual', 'daily', 'interp',
           'source', 'comment', 'comments', 'culture', 'cast', 'casts', 'crystals', 'throat', 'od', 'os', 'ou', 'left', 'right',
           'aa', 'naa', 'igg', 'igm', 'iga', 'ige', 'dna', 'rna', 'rbc', 'wbc', 'abs', 'absolute', 'ratio', 'near', 'pre',
           'non', 'not', 'no', 'acute', 'chronic', 'mild', 'morbid', 'severe', 'family', 'fh', 'icteric', 'lipemic',
           'hemolysis', 'total', 'free', 'direct', 'indirect', 'random', 'fasting'}
VAGUE = {'infection', 'pain', 'disease', 'disorder', 'syndrome', 'problem', 'condition', 'abnormal', 'test',
         'result', 'band', 'ab', 'antibody', 'level'}
OPPOSITES = [('hypo', 'hyper'), ('brady', 'tachy'), ('positive', 'negative'), ('high', 'low')]
DEPT_PLACE = re.compile(r'^(respiratory|respiratory therapy|sleep|sleep lab|radiology|imaging|lab|laboratory|'
                        r'cardiology|cardiopulmonary|therapy|physical therapy|rehab|rehabilitation|infusion|wound|'
                        r'emergency|surgery|surgical|pre-?admission|endoscopy|pharmacy|nuclear medicine)\b.*\bhospital$|'
                        r'^(minor |main |ambulatory )?or$|^operating room|^pacu$', re.I)
HOSPITAL = re.compile(r'hospital|medical cent(er|re)', re.I)


def _short(name, n=60):
    name = (name or '').strip()
    return name if len(name) <= n else name[:n - 1].rstrip() + '…'


def _entry_side(kind, r, detail):
    """One side of a question about entries: its name, what it says, and where it came from."""
    return {'name': r.get('name') or '', 'open': f"{kind}|{r['id']}", 'rows': [{'text': detail, 'docs': (r.get('docs') or [])[:3],
                                                                             'date': r.get('first_noted') or r.get('start_date') or ''}]}


def _lab_side(store, name, limit=4):
    """A lab test's latest results (date, value, normal range, flag) with their source documents."""
    rows = []
    for d, value, ref, flag, docs in store.db.execute(
            'SELECT substr(date,1,10), value, ref_range, flag, docs FROM labs WHERE analyte=? ORDER BY date DESC LIMIT ?',
            (name, limit)).fetchall():
        text = f"{d}: {value}" + (f"  (normal {ref})" if ref else '') + (f"  {flag}" if flag else '')
        rows.append({'text': text, 'docs': json.loads(docs or '[]')[:3], 'date': d})
    total = store.db.execute('SELECT COUNT(*) FROM labs WHERE analyte=?', (name,)).fetchone()[0]
    return {'name': name, 'rows': rows, 'more': max(0, total - len(rows))}


def _day(v):
    v = (v or '')[:10]
    try:
        return date.fromisoformat(v) if len(v) == 10 else date.fromisoformat(v + '-01') if len(v) == 7 else None
    except ValueError:
        return None


def _key(*parts):
    return hashlib.sha256('|'.join(map(str, parts)).encode()).hexdigest()[:16]


def _setting(store, name, default):
    try:
        return json.loads(store.get_setting(name) or 'null') or default
    except ValueError:
        return default


def learned_lab_names(store):
    return _setting(store, LAB_ALIAS, {})


def learned_condition_names(store):
    return _setting(store, CONDITION_ALIAS, {})


def _words(name, brackets=False):
    name = (name or '').lower()
    if not brackets:
        name = re.sub(r'\(.*?\)', ' ', name)
    return [w for w in WORDS.findall(name) if w not in FILLER]


def _alike(a, b, extra=2):
    """Two names for maybe the same thing: the same words, nearly the same letters, or one's words
    inside the other's with at most `extra` more. Never when they differ by a telling word (a number,
    a specimen or body site, IgG/IgM, RBC/WBC), by opposites (hypo/hyper), or share only vague words."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    sa, sb = ' '.join(wa), ' '.join(wb)
    full = set(_words(a, True)) ^ set(_words(b, True))   # "(OD)" vs "(OS)" counts
    if any(re.search(r'\d', w) or w in TELLING for w in full):
        return False
    if not any(len(w) >= 3 and w not in VAGUE and w not in TELLING for w in set(wa) & set(wb)):
        if not (len(sa) >= 6 and difflib.SequenceMatcher(None, sa, sb).ratio() >= 0.9):
            return False   # nothing meaningful in common ("Source" and "HSV Source")
    if sa == sb:
        return True
    diff = set(wa) ^ set(wb)
    for x, y in OPPOSITES:
        if (x in sa and y in sb) or (y in sa and x in sb):
            return False
    shared = set(wa) & set(wb)
    if not any(len(w) >= 4 and w not in VAGUE for w in shared) and not (len(diff) == 2 and min(len(w) for w in diff) >= 5):
        # nothing meaningful in common: only a spelling difference can make them alike
        pass
    small, big = (set(wa), set(wb)) if len(set(wa)) <= len(set(wb)) else (set(wb), set(wa))
    if small <= big:
        return (len(big - small) <= extra and any(len(w) >= 4 and w not in VAGUE for w in small))
    if len(sa) >= 6 and difflib.SequenceMatcher(None, sa, sb).quick_ratio() >= 0.9 \
            and difflib.SequenceMatcher(None, sa, sb).ratio() >= 0.9:
        return all(len(w) >= 5 for w in diff)   # a spelling difference, not a different short word


# ---------------------------------------------------------------------------------- the checks

def _stale_meds(store, today):
    """Imported prescriptions still listed as current that look stale: a short supply that ran out
    long ago, or one newer documents no longer list. Each comes with a suggested stop date."""
    doc_dates = {i: _day(d) for i, d in store.db.execute('SELECT id, doc_date FROM documents').fetchall()}
    newest = max((d for i, d in doc_dates.items() if d), default=None)
    out = []
    for m in store.list('medications'):
        if m.get('otc') or m.get('given') or m.get('edited') or not (m['id'] or '').startswith('ccda-'):
            continue
        if not (m['status'] == 'active' or (not m['end_date'] and not m['status'])):
            continue
        start = _day(m['start_date'])
        seen = max((doc_dates.get(x) for x in m['docs'] or [] if doc_dates.get(x)), default=None)
        why, end = None, None
        sched = (m.get('schedule') or '').lower()
        qty = re.search(r'#\s*(\d+)\s*(tab|cap|pill|patch|each|ea)', sched)
        refills = re.search(r'(\d+)\s*refill', sched)
        if start and qty and refills and refills.group(1) == '0':
            per_day = 1
            q = re.search(r'q\s*(\d+)\s*h', sched)
            t = re.search(r'(\d)\s*(?:\(\w+\)\s*)?times', sched)
            if q and int(q.group(1)):
                per_day = max(1, 24 // int(q.group(1)))
            elif t:
                per_day = int(t.group(1))
            days = max(7, math.ceil(int(qty.group(1)) / per_day))
            if start + timedelta(days=days + 30) < today:
                end = start + timedelta(days=days)
                why = (f"It was {qty.group(1)} {qty.group(2)}s with no refills, from {start.isoformat()}: "
                       f"about {days} days' worth.")
        if not why and seen and newest and newest - seen > timedelta(days=STALE_AFTER):
            end = seen
            why = f"It was last listed on {seen.isoformat()}; newer records don't list it."
        if why:
            out.append({'key': 'med-stale:' + m['id'], 'kind': 'medications', 'ids': [m['id']],
                        'sides': [_entry_side('medications', m, f"{m['start_date'] or '?'} to now. {m.get('schedule') or ''}".strip())],
                        'title': m['name'], 'tag': 'Medication',
                        'ask': 'Is this still being taken?', 'note': why,
                        'answers': [{'id': 'stop', 'label': f"Stopped about {end.isoformat()}", 'end': end.isoformat()},
                                    {'id': 'keep', 'label': 'Still taking it'}]})
    return out


def _dup_conditions(store):
    """Pairs of conditions whose names look like one condition worded two ways."""
    rows = store.list('conditions')
    out = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if _alike(a['name'], b['name']):
                out.append({'key': 'cond:' + _key(*sorted([a['id'], b['id']])), 'kind': 'conditions',
                            'ids': [a['id'], b['id']], 'tag': 'Condition',
                            'sides': [_entry_side('conditions', r, f"{r['status'] or ''}, first noted {r['first_noted'] or 'unknown'}"
                                                  + (f". {r['notes']}" if r.get('notes') else '')) for r in (a, b)],
                            'title': f"{a['name']}  /  {b['name']}",
                            'ask': 'Are these the same condition?',
                            'note': f"First noted {a['first_noted'] or 'unknown'} and {b['first_noted'] or 'unknown'}.",
                            'answers': [{'id': 'keep:' + a['id'], 'label': f"Same: keep “{_short(a['name'])}”"},
                                        {'id': 'keep:' + b['id'], 'label': f"Same: keep “{_short(b['name'])}”"},
                                        {'id': 'no', 'label': 'Not the same'}]})
    return out


def _lab_profiles(store):
    """Per lab test: its units, how many results are numbers, and its values by day."""
    prof = {}
    for analyte, unit, num, d in store.db.execute('SELECT analyte, unit, num, substr(date,1,10) FROM labs').fetchall():
        p = prof.setdefault(analyte, {'units': set(), 'numeric': 0, 'n': 0, 'days': {}})
        if unit:
            p['units'].add(unit.lower().replace(' ', ''))
        p['n'] += 1
        p['numeric'] += num is not None
        p['days'].setdefault(d, set()).add(num)
    return prof


def _dup_labs(store):
    """Pairs of lab tests that look like one test under two names: alike names, compatible units
    and kinds of value, and never two different values on the same day."""
    prof = _lab_profiles(store)
    names = sorted(prof)
    out = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if not _alike(a, b, extra=1):
                continue
            pa, pb = prof[a], prof[b]
            if pa['units'] and pb['units'] and not pa['units'] & pb['units']:
                continue   # "Basophils %" and "Basophils, absolute": different measures
            if (pa['numeric'] / pa['n'] > .5) != (pb['numeric'] / pb['n'] > .5):
                continue   # a number and a word ("Glucose" and "Urine glucose: negative")
            clash = [d for d in pa['days'] if d in pb['days'] and not pa['days'][d] & pb['days'][d]]
            if clash:
                continue   # different values on the same day: two different tests
            keep, other = (a, b) if pa['n'] >= pb['n'] else (b, a)
            out.append({'key': 'lab:' + _key(*sorted([a, b])), 'kind': 'labs', 'names': [a, b], 'tag': 'Lab test',
                        'sides': [_lab_side(store, a), _lab_side(store, b)],
                        'title': f"{a}  /  {b}", 'ask': 'Are these the same lab test?',
                        'note': f"{pa['n']} and {pb['n']} results"
                                + (f", in {', '.join(sorted(pa['units'] | pb['units']))}" if pa['units'] | pb['units'] else '')
                                + '. They never disagree on the same day.',
                        'answers': [{'id': 'name:' + keep, 'label': f"Same: show as “{_short(keep)}”"},
                                    {'id': 'name:' + other, 'label': f"Same: show as “{_short(other)}”"},
                                    {'id': 'no', 'label': 'Not the same'}]})
    return out


def _dup_meds(store, today):
    """Pairs of prescriptions for what looks like one drug, both taken at the same time for two
    weeks or more (one after the other is a separate period or a dose change, not a duplicate)."""
    from .ccda import drug_key
    key, _, _ = store._drug_keys()
    meds = [m for m in store.list('medications') if not m.get('otc') and not m.get('given')]

    def span(m):
        s = _day(m['start_date'])
        e = _day(m['end_date']) or (today if m['status'] != 'stopped' else s)
        return s, e
    out = []
    for i, a in enumerate(meds):
        for b in meds[i + 1:]:
            ka, kb = key(a['name']), key(b['name'])
            same_drug = ka == kb or (len(ka) >= 5 and len(kb) >= 5 and
                                     difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.88)
            if not same_drug:
                continue
            (sa, ea), (sb, eb) = span(a), span(b)
            if not (sa and sb and ea and eb):
                continue
            overlap = (min(ea, eb) - max(sa, sb)).days
            if overlap < 14:
                continue   # one after the other: separate periods, or a dose change
            mine = [m for m in (a, b) if not (m['id'] or '').startswith('ccda-') or m.get('edited')]
            keep = mine[0] if len(mine) == 1 else max((a, b), key=lambda m: (span(m)[1], m['id']))
            other = b if keep is a else a
            out.append({'key': 'med:' + _key(*sorted([a['id'], b['id']])), 'kind': 'medications',
                        'ids': [a['id'], b['id']], 'tag': 'Medication',
                        'sides': [_entry_side('medications', r, f"{r['dose'] or ''} {r['start_date'] or '?'} to {r['end_date'] or 'now'}, "
                                                                 f"{r['status'] or ''}".strip()) for r in (a, b)],
                        'title': f"{a['name'][:60]}  /  {b['name'][:60]}",
                        'ask': 'Are these the same medication, listed twice?',
                        'note': f"{a['start_date'] or '?'} to {a['end_date'] or 'now'} and "
                                f"{b['start_date'] or '?'} to {b['end_date'] or 'now'}: {overlap} days overlap.",
                        'answers': [{'id': 'keep:' + keep['id'], 'label': f"Same: keep “{_short(keep['name'])}”"},
                                    {'id': 'keep:' + other['id'], 'label': f"Same: keep “{_short(other['name'])}”"},
                                    {'id': 'no', 'label': 'Not the same'}]})
    return out


PLACE_TABLES = ('encounters', 'procedures', 'tests', 'imaging', 'documents')


def _dept_places(store):
    """Place names that look like a hospital department, with the most used hospitals as answers."""
    counts, hospitals = {}, {}
    for t in PLACE_TABLES:
        for (f,) in store.db.execute(f'SELECT facility FROM {t} WHERE facility IS NOT NULL').fetchall():
            for part in f.split(' / '):
                part = part.strip()
                if DEPT_PLACE.search(part):
                    counts[part] = counts.get(part, 0) + 1
                elif HOSPITAL.search(part) and not DEPT_PLACE.search(part):
                    hospitals[part] = hospitals.get(part, 0) + 1
    top = [h for h, _n in sorted(hospitals.items(), key=lambda x: -x[1])[:3]]
    return [{'key': 'place:' + _key(p), 'kind': 'places', 'place': p, 'tag': 'Place', 'title': p,
             'ask': 'This looks like a hospital department, not a place. Which hospital is it at?',
             'note': f"Used on {n} entr{'y' if n == 1 else 'ies'}.",
             'answers': [{'id': 'place:' + h, 'label': _short(h)} for h in top] + [{'id': 'no', 'label': "It's a real place"}]}
            for p, n in sorted(counts.items())]


_CACHE = {}


def _stamp(store):
    parts = []
    for t in ('medications', 'conditions', 'labs', 'encounters', 'procedures', 'documents'):
        parts.append(store.db.execute(f'SELECT COUNT(*), MAX(updated) FROM {t}' if t != 'documents'
                                      else 'SELECT COUNT(*), MAX(doc_date) FROM documents').fetchone())
    parts.append(store.get_setting(DISMISSED))
    return json.dumps(parts, default=str)


def issues(store, today=None):
    """Every open question, most useful first."""
    today = today or date.today()
    stamp = (id(store), _stamp(store), today)
    if _CACHE.get(id(store), (None,))[0] == stamp:
        return _CACHE[id(store)][1]
    gone = set(_setting(store, DISMISSED, []))
    found = (_stale_meds(store, today) + _dup_meds(store, today) + _dup_conditions(store)
             + _dup_labs(store) + _dept_places(store))
    found = [x for x in found if x['key'] not in gone]
    _CACHE[id(store)] = (stamp, found)
    return found


def answer(store, key, action):
    """Apply one answer. Returns what changed."""
    found = {x['key']: x for x in issues(store)}
    x = found.get(key)
    if not x:
        raise ValueError('that question is no longer open')
    if action not in {a['id'] for a in x['answers']}:
        raise ValueError('not an answer to that question')
    done = {'key': key, 'action': action}
    if action in ('no', 'keep'):
        pass
    elif x['kind'] == 'medications' and action == 'stop':
        end = next(a['end'] for a in x['answers'] if a['id'] == 'stop')
        m = store.get('medications', x['ids'][0])
        note = ((m.get('notes') or '') + f" Stop date {end} estimated by the record check and confirmed on the dashboard.").strip()
        store.update('medications', m['id'], {'end_date': end, 'status': 'stopped', 'notes': note})
        done['stopped'] = m['id']
    elif x['kind'] in ('conditions', 'medications') and action.startswith('keep:'):
        keep_id = action.split(':', 1)[1]
        other_id = next(i for i in x['ids'] if i != keep_id)
        keep, other = store.get(x['kind'], keep_id), store.get(x['kind'], other_id)
        for d in other['docs']:
            store.link_doc(x['kind'], keep_id, d)
        if x['kind'] == 'conditions':
            dates = [v for v in (keep['first_noted'], other['first_noted']) if v]
            vals = {}
            if dates and min(dates) != keep['first_noted']:
                vals['first_noted'] = min(dates)
            if other['status'] == 'active' and keep['status'] != 'active' and (keep['id'] or '').startswith('ccda-'):
                vals['status'] = 'active'
            if vals:
                store.update('conditions', keep_id, vals)
            learned = learned_condition_names(store)
            learned[other['name'].strip().lower()] = keep['name']
            store.set_setting(CONDITION_ALIAS, json.dumps(learned))
        else:
            key_of, _, _ = store._drug_keys()
            a, b = key_of(other['name']), key_of(keep['name'])
            if a != b:
                with store.lock:
                    store.db.execute('INSERT OR REPLACE INTO drug_alias VALUES (?, ?)', (a, b))
                    store.db.commit()
        store.delete(x['kind'], other_id)
        done['merged'] = other_id
    elif x['kind'] == 'labs' and action.startswith('name:'):
        name = action.split(':', 1)[1]
        other = next(n for n in x['names'] if n != name)
        with store.lock:
            store.db.execute('UPDATE labs SET analyte=? WHERE analyte=?', (name, other))
            # the same result saved under both names: keep one
            store.db.execute('DELETE FROM labs WHERE rowid NOT IN (SELECT MIN(rowid) FROM labs '
                             'GROUP BY analyte, date, COALESCE(CAST(num AS TEXT), value)) AND analyte=?', (name,))
            store.db.commit()
        learned = learned_lab_names(store)
        learned[other.strip().lower()] = name
        store.set_setting(LAB_ALIAS, json.dumps(learned))
        done['renamed'] = other
    elif x['kind'] == 'places' and action.startswith('place:'):
        name, old = action.split(':', 1)[1], x['place']
        n = 0
        with store.lock:
            for t in PLACE_TABLES:
                for rid, f in store.db.execute(f'SELECT id, facility FROM {t} WHERE facility LIKE ?', (f'%{old}%',)).fetchall():
                    parts = [name if p.strip() == old else p.strip() for p in f.split(' / ')]
                    new = ' / '.join(dict.fromkeys(parts))
                    if new != f:
                        n += store.db.execute(f'UPDATE {t} SET facility=? WHERE id=?', (new, rid)).rowcount
            store.db.commit()
        done['changed'] = n
    gone = _setting(store, DISMISSED, [])
    if key not in gone:
        gone.append(key)
        store.set_setting(DISMISSED, json.dumps(gone))
    _CACHE.pop(id(store), None)
    return done

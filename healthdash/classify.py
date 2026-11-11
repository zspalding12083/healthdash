"""Work out what an uploaded document is from its text: kind, date, title, where it's from.

Plain rules, all local. Every answer comes with whether we're sure; anything we're not sure
about becomes a question for a person (the document is flagged "needs review").
"""

import os
import re
from datetime import date

from . import catalog, parsers

CATEGORIES = ['hospital', 'office', 'imaging', 'labs', 'procedures', 'tests', 'eye', 'dental',
              'medications', 'financial', 'legal', 'other']
LABELS = {'hospital': 'hospital / ER record', 'office': 'office visit note', 'imaging': 'imaging report',
          'labs': 'lab results', 'procedures': 'surgery / procedure report', 'tests': 'test report',
          'eye': 'eye exam', 'dental': 'dental record', 'medications': 'medication list / prescription',
          'financial': 'bill or statement', 'legal': 'form / authorization', 'other': 'other'}

KEYWORDS = {
    'labs': ['reference range', 'ref range', 'specimen', 'collected', 'lab results', 'laboratory',
             'hemoglobin', 'hematocrit', 'wbc', 'platelet', 'glucose', 'creatinine', 'a1c', 'cbc',
             'metabolic panel', 'lipid panel', 'cholesterol', 'mg/dl', 'k/ul', 'mmol/l'],
    'imaging': ['impression', 'findings', 'technique', 'radiology', 'radiologist', 'mri', 'ct',
                'x-ray', 'xray', 'radiograph', 'ultrasound', 'sonogram', 'mammogra', 'contrast', 'cta',
                'mra', 'views', 'comparison'],
    'hospital': ['emergency department', 'emergency room', 'discharge', 'admission', 'admitted',
                 'history of present illness', 'hospital course', 'ed course', 'triage', 'inpatient',
                 'attending', 'admit date', 'hospitalist', 'hospital day', 'nursing', 'subjective',
                 'objective', 'plan of care', 'code status'],
    'office': ['office visit', 'follow-up', 'follow up', 'chief complaint', 'assessment and plan',
               'assessment/plan', 'progress note', 'consultation', 'new patient', 'review of systems',
               'vital signs'],
    'procedures': ['operative report', 'operation', 'preoperative diagnosis', 'postoperative diagnosis',
                   'pre-operative', 'post-operative', 'anesthesia', 'surgeon', 'incision',
                   'estimated blood loss', 'procedure performed', 'thoracentesis', 'chest tube',
                   'midline', 'picc', 'catheter', 'biopsy', 'aspiration', 'time out', 'timeout',
                   'sterile', 'prepped and draped', 'consent obtained', 'complications', 'needle',
                   'tube placement', 'placed'],
    'tests': ['echocardiogram', 'ekg', 'ecg', 'electrocardiogram', 'eeg', 'sleep study',
              'polysomnogra', 'stress test', 'pulmonary function', 'holter', 'ejection fraction',
              'nerve conduction', 'emg'],
    'eye': ['visual acuity', 'optometr', 'ophthalm', 'intraocular', 'eyeglass', 'spectacle',
            'contact lens', 'refraction', 'retina', 'glaucoma', 'cataract', 'sph', 'cyl', 'axis',
            'pupillary distance', 'lens', 'lenses', 'frames', 'progressive', 'bifocal', 'o.d.',
            'eye exam'],
    'dental': ['dental', 'dentist', 'tooth', 'teeth', 'denture', 'extraction', 'periodontal',
               'dds', 'dmd', 'oral surgery', 'crown', 'filling', 'root canal', 'cavity', 'hygienist',
               'cdt', 'gum'],
    'medications': ['medication list', 'medications', 'prescription', 'refills', 'pharmacy',
                    'dispense', 'take 1 tablet', 'by mouth', 'sig:', 'tablet', 'capsule'],
    'financial': ['amount due', 'balance due', 'statement date', 'invoice', 'total charges',
                  'estimate', 'treatment plan', 'patient responsibility', 'billing', 'account number',
                  'pay this amount', 'charges', 'payment', 'insurance', 'remit'],
    'legal': ['authorization', 'release of information', 'health care proxy', 'power of attorney',
              'hipaa', 'consent', 'workers compensation', "workers' compensation", 'claim number',
              'signature of', 'notary'],
}

# phrases a document uses to name itself, in its file name or its first lines
NAMES = [
    ('hospital', r'discharge summary|discharge instructions|emergency (department|room)|\bed (note|visit)|'
                 r'admission (note|h&p)|history and physical|h&p|hospital course|er report|ed provider note'),
    ('office', r'office[- ]?(clinic )?(visit|note)|clinic note|progress note|consult(ation)? note|visit (summary|note)|'
               r'after visit summary|past visit details|follow[- ]?up visit'),
    ('procedures', r'operative report|op(eration)? report|procedure note|surgery report|brief op note'),
    ('imaging', r'\b(mri|mra|cta|ct|x-?ray|xr|ultrasound|us|mammogra\w*|pet|dexa|fluoro\w*)\b.{0,30}\b(report|scan|head|brain|chest|spine|'
                r'lumbar|cervical|thoracic|abdomen|pelvis|knee|hip|shoulder|neck|breast)\b|radiology report|imaging report'),
    ('tests', r'echocardiogram|\bekg\b|\becg\b|\beeg\b|sleep study|stress test|holter|pulmonary function|\bemg\b'),
    ('eye', r'\beyes?\b|\boct\b|optic nerve|retina|visual field|humphrey|eye exam|ophthalm\w*|optometr\w*|glasses|contact lens'),
    ('dental', r'dental|dentist|denture|oral surgery|orthodont\w*'),
    ('labs', r'lab(oratory)? results?|lab report|\blabs\b|blood work|pathology report'),
    ('medications', r'medication list|med(ication)? schedule|prescription(?! glasses)|\brx\b'),
    ('financial', r'statement|invoice|\bbill\b|billing|estimate|quote|explanation of benefits|\beob\b|receipt'),
    ('legal', r'authorization|release of information|health care proxy|power of attorney|consent form|'
              r'workers.? comp\w*|claim form'),
]


def named_category(filename, text):
    """(category, where) from the file name, else from the first few lines; None if neither says."""
    base = re.sub(r'[_]+', ' ', os.path.splitext(os.path.basename(filename or ''))[0]).lower()
    head = '\n'.join([ln for ln in (text or '').splitlines() if ln.strip()][:8]).lower()
    for where, src in (('name', base), ('heading', head)):
        for cat, rx in NAMES:
            if re.search(rx, src):
                return cat, where
    return None, None


_MON = {m: i for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep',
                                     'oct', 'nov', 'dec'], 1)}
_MONTHS = r'(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?'
_DATE_RES = [
    (re.compile(r'(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?!\d)'), 'mdy'),
    (re.compile(r'(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)'), 'ymd'),
    (re.compile(r'(?<!\d)(\d{1,2})-(\d{1,2})-(\d{4})(?!\d)'), 'mdy'),
    (re.compile(_MONTHS + r'\s+(\d{1,2}),?\s+(\d{4})', re.I), 'Mdy'),
    (re.compile(r'(?<!\d)(\d{1,2})[\s-]' + _MONTHS + r'[\s-](\d{4})', re.I), 'dMy'),
]
# a date on a line with one of these is the date of care
DATE_KEYS = ['date of service', 'service date', 'dos', 'exam date', 'date of exam', 'study date',
             'collected', 'visit date', 'date of visit', 'encounter date', 'statement date', 'report date',
             'procedure date', 'date of procedure', 'date of surgery', 'surgery date', 'date of note',
             'note date', 'exam performed']
WEAK_KEYS = ['admit date', 'admission date', 'date of admission', 'discharge date', 'appointment', 'date:',
             'signed', 'dictated']
# ...and never on a line with one of these
NOT_CARE = ['dob', 'd.o.b', 'birth', 'born', 'printed', 'print date', 'fax', 'expires', 'expiration',
            'effective date', 'member since']

AMOUNT = re.compile(r'(total(?: charges| fees?| estimate| amount| due)?|amount due|balance due|grand total|'
                    r'total treatment plan)\b[^\n$\d]{0,40}\$?\s*([\d,]+\.\d{2})', re.I)
TITLE_LINE = re.compile(r'^\s*(exam(?:ination)?|procedure|study|test|report|document|visit type|'
                        r'reason for (?:visit|exam))\s*:\s*(.{4,80}?)\s*$', re.I | re.M)


def _year(y):
    y = int(y)
    if y < 100:
        y += 2000 if y <= date.today().year % 100 else 1900
    return y


def _make(y, m, d):
    try:
        dt = date(_year(y), int(m), int(d))
    except ValueError:
        return None
    if dt.year < 1950 or dt > date.today():
        return None
    return dt.isoformat()


def find_dates(text):
    """[(iso date, label, position)] in the order they appear. The label is the text on the
    same line just before the date (back to the previous date), e.g. "Date of service:"."""
    out = []
    for rx, order in _DATE_RES:
        for m in rx.finditer(text):
            g = m.groups()
            if order == 'mdy':
                iso = _make(g[2], g[0], g[1])
            elif order == 'ymd':
                iso = _make(g[0], g[1], g[2])
            elif order == 'Mdy':
                iso = _make(g[2], _MON[g[0][:3].lower()], g[1])
            else:
                iso = _make(g[2], _MON[g[1][:3].lower()], g[0])
            if iso:
                out.append([iso, None, m.start(), m.end()])
    out.sort(key=lambda x: x[2])
    prev_end = 0
    for d in out:
        ls = text.rfind('\n', 0, d[2]) + 1
        d[1] = text[max(ls, prev_end):d[2]][-60:]
        prev_end = d[3]
    return [(iso, label, pos) for iso, label, pos, _ in out]


def _has(keys, low):
    return any(re.search(r'(?<![a-z])' + re.escape(k), low) for k in keys)


def filename_date(filename):
    base = os.path.splitext(os.path.basename(filename or ''))[0].replace('_', ' ')
    found = [d for d, _, _ in find_dates(base)]
    if found:
        return found[0]
    fn = catalog.guess_date(filename or '')
    return fn if fn and len(fn) == 10 else None


def guess_date(text, filename=''):
    """(date or None, sure)"""
    fd = filename_date(filename)
    if fd:
        # named by its date: sure when the document mentions that date too
        return fd, any(d == fd for d, _, _ in find_dates(text))
    allf = find_dates(text)
    lines = text.split('\n')
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln) + 1)
    birth, skip = set(), set()
    for n, (iso, label, pos) in enumerate(allf):
        i = max(0, sum(1 for st in starts if st <= pos) - 1)
        prev_line = lines[i - 1] if i > 0 else ''
        low = label.lower()
        if _has(['dob', 'd.o.b', 'date of birth', 'birth date', 'birthdate', 'born'], low) or \
                (not label.strip() and _has(['dob', 'date of birth', 'birth'], prev_line.lower()[-40:])):
            birth.add(iso)
        elif _has(NOT_CARE, low):
            skip.add(n)
    found = [d for n, d in enumerate(allf) if d[0] not in birth and n not in skip]
    for keys in (DATE_KEYS, WEAK_KEYS):
        keyed = [iso for iso, label, _ in found if _has(keys, label.lower())]
        if keyed:
            counts = {}
            for iso in keyed:
                counts[iso] = counts.get(iso, 0) + 1
            best = sorted(counts.items(), key=lambda kv: (-kv[1], keyed.index(kv[0])))
            sure = len(best) == 1 or best[0][1] >= 2 * best[1][1]
            return best[0][0], sure
    if found:
        counts = {}
        for iso, _, _ in found:
            counts[iso] = counts.get(iso, 0) + 1
        best = sorted(counts.items(), key=lambda kv: -kv[1])
        if best[0][1] >= 2 and (len(best) == 1 or best[0][1] > best[1][1]):
            return best[0][0], True
        if len(counts) == 1:
            return best[0][0], True
        fn = catalog.guess_date(filename)
        if fn and len(fn) == 10 and fn in counts:
            return fn, True
        return best[0][0], False
    fn = catalog.guess_date(filename)
    return fn, bool(fn and len(fn) == 10)


def score_categories(text):
    low = ' ' + re.sub(r'\s+', ' ', text.lower()) + ' '
    scores = {}
    for cat, words in KEYWORDS.items():
        n = 0
        for w in words:
            pat = r'(?<![a-z])' + re.escape(w) + (r'(?![a-z])' if len(w) <= 4 else '')
            if re.search(pat, low):
                n += 1
        scores[cat] = n
    return scores


def _is_estimate(text):
    return bool(re.search(r'\b(estimate|quote|treatment plan|proposed treatment)\b', text, re.I))


def guess_category(text, labs=0, filename=''):
    """(category, sure, scores)"""
    s = score_categories(text)
    named, where = named_category(filename, text)
    if named == 'financial' and s['dental'] >= 2:
        named = 'dental'
    if named:
        # trust what it calls itself; a heading needs some support from the text
        return named, where == 'name' or s.get(named, 0) >= 2 or (named == 'labs' and labs >= 3), s
    narrative = s['hospital'] + s['office'] + s['procedures']
    if labs >= 3 and narrative == 0:
        return 'labs', True, s
    if labs >= 3:
        s['labs'] = max(0, s['labs'] - 3)  # lab tables inside a note don't make it a lab report
    # an estimate or bill from the dentist is a dental record; its price goes on the financial tab
    if s['dental'] >= 2 and s['financial'] >= 2:
        return 'dental', True, s
    ranked = sorted(s.items(), key=lambda kv: -kv[1])
    (top, a), (_, b) = ranked[0], ranked[1]
    if a == 0:
        return 'other', False, s
    sure = a >= 3 and a >= 1.5 * b
    if top == 'labs' and labs == 0:
        sure = False  # lab words without lab values: probably a note that mentions results
    return top, sure, s


def guess_facility(text, known):
    """Match against facilities already in the records. (name or None, sure)"""
    low = text.lower()
    hits = []
    for name in known:
        if not name:
            continue
        base = re.sub(r'\s*\(.*?\)', '', name).strip()
        score = 0
        if base and base.lower() in low:
            score = 10
        else:
            words = [w for w in re.findall(r"[A-Za-z][A-Za-z'&-]{2,}", base)
                     if w.lower() not in _COMMON and (len(w) >= 5 or w.isupper())]
            if len(words) == 1 and not _distinctive(words[0]):
                words = []  # e.g. a town name: only the full name counts
            score = sum(1 for w in words if (_mentioned(w.lower(), low) if not w.isupper()
                                              else re.search(r'\b' + re.escape(w) + r'\b', text)))
            if words and score < min(2, len(words)):
                score = 0
        if score:
            hits.append((score, len(name), name))
    if not hits:
        return None, False
    hits.sort(reverse=True)
    best = [h for h in hits if h[0] == hits[0][0]]
    if len(best) > 1 and len({h[2].lower() for h in best}) > 1:
        # several names tie: if one contains the others it is the same place
        longest = best[0][2]
        if all(re.sub(r'\s*\(.*?\)', '', h[2]).strip().lower() in longest.lower() for h in best):
            return longest, True
        return longest, False
    return best[0][2], True


def _distinctive(word):
    """An acronym (ABC) or a made-up brand word (MediCorp), not an ordinary/town word."""
    return word.isupper() or any(c.isupper() for c in word[1:])


def _mentioned(word, low):
    """word appears other than as the town in an address ("Springfield, IL 62701")."""
    for m in re.finditer(r'\b' + re.escape(word) + r'\b', low):
        after = low[m.end():m.end() + 20]
        if re.match(r',?\s*[a-z]\.?[a-z]\.?(\s+\d{5}|\s*,|\s*$)', after, re.M):
            continue
        return True
    return False


_COMMON = set('''hospital medical center centre health family medicine practice associates group
radiology imaging services urgent emergency department office surgery surgical regional memorial
community physicians specialists clinic network system street avenue road dental vision'''.split())


def guess_title(text, category, filename):
    m = TITLE_LINE.search(text)
    if m:
        t = re.sub(r'\s{2,}.*$', '', m.group(2)).strip(' .:-')
        if t.isupper():
            t = t.capitalize()
        if len(t) >= 4:
            return t
    if category == 'financial' or (category == 'dental' and _is_estimate(text)):
        return 'Treatment estimate' if _is_estimate(text) else 'Bill / statement'
    defaults = {'labs': 'Lab results', 'imaging': 'Imaging report', 'hospital': 'Hospital record',
                'office': 'Office visit note', 'procedures': 'Procedure report', 'tests': 'Test report',
                'eye': 'Eye exam', 'dental': 'Dental record', 'medications': 'Medication list',
                'legal': 'Form'}
    return defaults.get(category) or catalog.guess_title(filename or 'Document')


def find_amount(text):
    """(total, sure): the one total on a bill or estimate."""
    vals = []
    for m in AMOUNT.finditer(text):
        v = float(m.group(2).replace(',', ''))
        if v > 0:
            vals.append(v)
    if not vals:
        return None, False
    if len(set(vals)) == 1:
        return vals[0], True
    return max(vals), False


def _lines(text):
    """Normalized lines that can identify where a document came from (letterheads, portal
    boilerplate). Digits are dropped so dates and account numbers don't matter."""
    out = set()
    for ln in text.splitlines():
        ln = re.sub(r'\d+', '#', ln.lower())
        ln = re.sub(r'[^a-z#]+', ' ', ln).strip()
        if 10 <= len(ln) <= 80 and sum(c.isalpha() for c in ln) >= 8:
            out.add(ln)
    return out


class Filed:
    """What we can learn from documents that are already filed: [(facility, category, text)]."""

    def __init__(self, docs):
        self.docs = [(f, c, _lines(t or '')) for f, c, t in docs]
        df = {}
        for _, _, ls in self.docs:
            for ln in ls:
                df[ln] = df.get(ln, 0) + 1
        common = max(3, len(self.docs) // 4)  # boilerplate on most documents tells us nothing
        self.by_fac = {}
        for f, _, ls in self.docs:
            if f:
                self.by_fac.setdefault(f, set()).update(ln for ln in ls if df[ln] < common)
        owners = {}
        for f, ls in self.by_fac.items():
            for ln in ls:
                owners.setdefault(ln, set()).add(f)
        self.unique = {f: {ln for ln in ls if len(owners[ln]) == 1} for f, ls in self.by_fac.items()}

    def facility(self, text):
        """(facility, sure) from lines only that facility's documents have."""
        ls = _lines(text)
        hits = sorted(((len(ls & u), f) for f, u in self.unique.items()), reverse=True)
        if not hits or hits[0][0] < 2:
            return None, False
        sure = hits[0][0] >= 3 and (len(hits) == 1 or hits[0][0] >= 2 * hits[1][0])
        return hits[0][1], sure


def shingles(text, n=5):
    """Runs of n words (numbers kept), for telling whether two documents say the same thing."""
    w = re.findall(r'[a-z]{2,}|\d+', (text or '').lower())
    return {' '.join(w[i:i + n]) for i in range(len(w) - n + 1)}


COPY, INSIDE = 0.85, 0.85  # different lab printouts from one portal overlap up to ~0.6


def near_duplicate(text, others):
    """others: [(id, title, date, text)]. Returns the best match as
    {'id', 'title', 'doc_date', 'score', 'how': 'copy' | 'inside'} or None."""
    a = shingles(text)
    if len(a) < 30:
        return None
    best = None
    for oid, title, d, t in others:
        b = shingles(t)
        if len(b) < 30:
            continue
        common = len(a & b)
        score = common / min(len(a), len(b))
        if score < min(COPY, INSIDE):
            continue
        ratio = min(len(a), len(b)) / max(len(a), len(b))
        how = 'copy' if ratio >= 0.7 else ('inside' if len(a) < len(b) else 'contains')
        if how == 'contains':  # the new one is the bigger document; the old one is part of it
            continue
        if not best or score > best['score']:
            best = {'id': oid, 'title': title, 'doc_date': d, 'score': round(score, 2), 'how': how}
    return best


def lab_rows(text, facility=None):
    rows = parsers.portal_list_labs(text, facility) + parsers.cerner_block_labs(text, facility)
    return parsers.normalize(rows)


def classify(text, filename='', known_facilities=(), filed=None):
    """Everything we can tell about a document, plus questions for what we can't.
    filed: a Filed built from documents already in the records (optional)."""
    text = text or ''
    facility, fac_sure = guess_facility(text, known_facilities)
    if filed and not fac_sure:
        f2, s2 = filed.facility(text)
        # a learned match is only a suggestion: similar portals and letterheads get mixed up
        if f2 and not facility:
            facility, fac_sure = f2, False
    labs = lab_rows(text, facility)
    category, cat_sure, _ = guess_category(text, len(labs), filename)
    doc_date, date_sure = guess_date(text, filename)
    if labs and not doc_date:
        doc_date, date_sure = labs[0]['date'], True
    title = guess_title(text, category, filename)
    money = None
    if category == 'financial' or (category == 'dental' and _is_estimate(text)):
        amount, amount_sure = find_amount(text)
        money = {'kind': 'Estimate (not billed)' if _is_estimate(text) else 'Bill',
                 'charged': amount, 'sure': amount_sure}
    questions = []
    if len(text.strip()) < 25:
        questions.append("I couldn't read any text in this file. What is it?")
    else:
        if not cat_sure:
            questions.append(f"What kind of document is this? My best guess is {LABELS[category]}.")
        if not doc_date:
            questions.append("What date is it from? I couldn't find one.")
        elif not date_sure:
            questions.append("Is the date right? It mentions several dates and I picked one.")
        if not facility:
            questions.append("Who is it from? It doesn't match any doctor or hospital already in the records.")
        elif not fac_sure:
            questions.append(f"Is it from {facility}? More than one place is mentioned.")
        if money and money['charged'] is None:
            questions.append("I couldn't find the total on this bill or estimate. What was the amount?")
        elif money and not money['sure']:
            questions.append(f"Is the total {money['charged']:,.2f}? The document shows more than one total.")
    return {'title': title, 'category': category, 'doc_date': doc_date, 'facility': facility,
            'labs': labs, 'money': money, 'questions': questions,
            'sure': {'category': cat_sure, 'date': date_sure, 'facility': fac_sure}}

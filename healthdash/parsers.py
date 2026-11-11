"""Parsers for lab printouts with a regular layout.

portal_list_labs: patient-portal "Lab Results" printouts, where each result is
"<name>\\n<value> <unit>\\nDate: Mar 4, 2020 07:13 a.m. EDT  Reference Range: lo - hi".

cerner_block_labs: hospital result blocks, "Final Result  Collected 04-Mar-2020 22:35" followed
by lines like "GLUCOSE  130H  74-106  mg/dL".
"""

import re
from datetime import datetime

from . import config

_DATE = re.compile(r'Date:\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s+(\d{1,2}:\d{2} [ap]\.m\.)')
_REF = re.compile(r'Reference Range:\s*(.*)$')
_NUM = re.compile(r'^[<>]?\s*=?\s*(-?\d+(?:\.\d+)?)')
_NOT_PANELS = {'Negative', 'Positive', 'View all for this result', 'See Below', 'Final'}
_NOISE = re.compile(r'^(' + ''.join(rf'\d+/\d+/\d{{4}}\s+{re.escape(w)}|{re.escape(w)}|' for w in config.NOISE_LINES) +
                    r'Lab Results|\d+/\d+$|The information|questions about|'
                    r'Filtered results|Date Range:|Learn More|https?://|Operator Name:|Draw Date:)')


def _clean(line):
    line = line.replace('\u00ad', '-').replace('\xa0', ' ')
    line = re.sub(r'[\ue000-\uf8ff]', '', line)  # icon glyphs from the portal's web font
    return re.sub(r'\s+', ' ', line).strip()


def _range(ref):
    ref = ref.replace('­', '-')
    m = re.findall(r'(-?\d+(?:\.\d+)?)', ref)
    if len(m) >= 2 and ' - ' in re.sub(r'\s+', ' ', ref):
        return float(m[0]), float(m[1])
    return None


def flag_for(num, ref):
    rng = _range(ref) if ref else None
    if num is None or not rng:
        return None
    lo, hi = rng
    if num < lo:
        return 'L'
    if num > hi:
        return 'H'
    return None


def portal_list_labs(text, facility=None):
    """Return lab result dicts: date, time, panel, analyte, value, num, unit, ref_range, flag."""
    lines = [_clean(l) for l in text.replace('\f', '\n').splitlines()]
    lines = [l for l in lines if l and not _NOISE.match(l)]
    out, panel = [], None
    i = 0
    while i < len(lines):
        m = _DATE.search(lines[i])
        if not m:
            i += 1
            continue
        # look back for value and name (skipping blank/noise already removed)
        value = lines[i - 1] if i >= 1 else ''
        name = lines[i - 2] if i >= 2 else ''
        # panel header: a line before the name that is not itself a value/date line
        if i >= 3 and (i == 3 or _DATE.search(lines[i - 4])) and not _DATE.search(lines[i - 3]):
            prev = lines[i - 3]
            if len(prev) < 40 and not _NUM.match(prev) and prev not in _NOT_PANELS:
                panel = prev
        ref = (_REF.search(lines[i]).group(1).strip() if _REF.search(lines[i]) else '')
        ref = re.sub(r'\s+', ' ', ref.replace('­', '-'))
        d = datetime.strptime(m.group(1), '%b %d, %Y').strftime('%Y-%m-%d')
        nm = _NUM.match(value)
        num = float(nm.group(1)) if nm and not value.lstrip().startswith(('<', '>')) else None
        if _DATE.search(name) or 'Reference Range' in name:
            i += 1
            continue
        unit = value[nm.end():].strip() if nm else ''
        out.append({'date': d, 'time': m.group(2), 'panel': panel, 'analyte': name, 'value': value,
                    'num': num, 'unit': unit or None, 'ref_range': ref or None,
                    'flag': flag_for(num, ref), 'facility': facility})
        i += 1
    return out


# ---- Cerner-style hospital result blocks ------------------------------------------------
# "Final Result  Collected 04-Mar-2020 22:35" followed by lines like
# "GLUCOSE  130H  74-106  mg/dL" or "CHLORIDE  109 H  98-107  mmol/L"
_HA_COLLECTED = re.compile(r'Collected\s+(\d{1,2}-[A-Z][a-z]{2}-\d{4})')
_HA_LINE = re.compile(r'^([A-Z][A-Z0-9 #%()/\-]{1,40}?)\s{2,}([<>]?\s*\d[\d.,]*)\s*([HL])?\s{2,}'
                      r'(\d[\d.,]*\s*-\s*\d[\d.,]*|>=?\s*\d+)\s*(\S*)\s*$')


def cerner_block_labs(text, facility=None):
    out, date = [], None
    for raw in text.replace('\f', '\n').splitlines():
        line = re.sub(r'[ \t]{2,}', '  ', raw.strip())
        m = _HA_COLLECTED.search(line)
        if m:
            date = datetime.strptime(m.group(1), '%d-%b-%Y').strftime('%Y-%m-%d')
            continue
        m = _HA_LINE.match(line)
        if not m or not date:
            continue
        name, val, flag, ref, unit = m.groups()
        val = val.replace(',', '.').replace(' ', '')
        try:
            num = None if val[0] in '<>' else float(val)
        except ValueError:
            continue
        ref = ref.replace(',', '.').replace(' ', '')
        out.append({'date': date, 'analyte': name.strip().title().replace('Poc', 'POC').replace('Gfr', 'GFR')
                    .replace('Inr', 'INR').replace('Bun', 'BUN').replace('Mcv', 'MCV').replace('Mch', 'MCH')
                    .replace('Mchc', 'MCHC').replace('Rdw', 'RDW').replace('Mpv', 'MPV').replace('Wbc', 'WBC')
                    .replace('Rbc', 'RBC').replace('Hgb', 'HGB').replace('Hct', 'HCT').replace('Ast', 'AST')
                    .replace('Alt', 'ALT').replace('Aptt', 'APTT'),
                    'value': val + (f' {unit}' if unit else ''), 'num': num, 'unit': unit or None,
                    'ref_range': ref, 'flag': flag or flag_for(num, ref.replace('-', ' - ')), 'facility': facility})
    # same analyte + date reported in several documents: keep one
    seen, uniq = set(), []
    for r in out:
        k = (r['date'], r['analyte'], r['value'])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


# One name per analyte so values from different labs line up on the trend charts
SYNONYMS = {
    'hgb': 'Hemoglobin', 'hemoglobin': 'Hemoglobin', 'hct': 'Hematocrit', 'hematocrit': 'Hematocrit',
    'platelet': 'Platelets', 'platelet count': 'Platelets', 'platelets': 'Platelets',
    'wbc': 'WBC', 'rbc': 'RBC', 'mcv': 'MCV', 'mch': 'MCH', 'mchc': 'MCHC', 'rdw': 'RDW', 'mpv': 'MPV',
    'glucose': 'Glucose', 'glucose lvl': 'Glucose', 'sodium': 'Sodium', 'sodium lvl': 'Sodium',
    'potassium': 'Potassium', 'potassium lvl': 'Potassium', 'chloride': 'Chloride',
    'co2': 'CO2 (bicarbonate)', 'carbon dioxide': 'CO2 (bicarbonate)', 'agap': 'Anion gap', 'anion gap': 'Anion gap',
    'bun': 'BUN', 'creatinine': 'Creatinine', 'calcium': 'Calcium', 'calcium lvl': 'Calcium',
    'alt': 'ALT', 'ast': 'AST', 'alk phos': 'Alkaline phosphatase', 'alkaline phosphatase': 'Alkaline phosphatase',
    'alkaune phosphatase': 'Alkaline phosphatase', 'total protein': 'Total protein', 'albumin': 'Albumin',
    'albumin lvl': 'Albumin', 'bili total': 'Bilirubin, total', 'total bilirubin': 'Bilirubin, total',
    'inr': 'INR', 'pt': 'Prothrombin time', 'prothrombin time': 'Prothrombin time', 'aptt': 'APTT',
    'lactic acid': 'Lactic acid', 'lactic acid lvl': 'Lactic acid', 'magnesium': 'Magnesium',
    'neutro auto': 'Neutrophils %', 'neut auto': 'Neutrophils %', 'neutrophil': 'Neutrophils %',
    'lymph auto': 'Lymphocytes %', 'lymphocyte': 'Lymphocytes %', 'mono auto': 'Monocytes %', 'monocyte': 'Monocytes %',
    'eos auto': 'Eosinophils %', 'eosinophil': 'Eosinophils %', 'basophil auto': 'Basophils %', 'basophil': 'Basophils %',
    'neut absolute': 'Neutrophils, absolute', 'neutr absolute': 'Neutrophils, absolute',
    'abs neutrophil count': 'Neutrophils, absolute', 'lymph absolute': 'Lymphocytes, absolute',
    'lymph# (auto)': 'Lymphocytes, absolute', 'mono absolute': 'Monocytes, absolute', 'mono# (auto)': 'Monocytes, absolute',
    'mono # (auto)': 'Monocytes, absolute', 'eos absolute': 'Eosinophils, absolute', 'eos # (auto)': 'Eosinophils, absolute',
    'baso absolute': 'Basophils, absolute', 'baso# (auto)': 'Basophils, absolute',
    'estimated gfr': 'eGFR', 'glucose poc result': 'Glucose (fingerstick)', 'glucose poc': 'Glucose (fingerstick)',
    'immgr%': 'Immature granulocytes %', 'troponin high sensitivity': 'Troponin (high sensitivity)',
}


SYNONYMS.update({
    'lymphs': 'Lymphocytes %', 'eos': 'Eosinophils %', 'basos': 'Basophils %', 'neutrophils': 'Neutrophils %',
    'monocytes': 'Monocytes %', 'neutrophils (absolute)': 'Neutrophils, absolute',
    'lymphs (absolute)': 'Lymphocytes, absolute', 'monocytes(absolute)': 'Monocytes, absolute',
    'monocytes (absolute)': 'Monocytes, absolute', 'eos (absolute)': 'Eosinophils, absolute',
    'baso (absolute)': 'Basophils, absolute', 'immature granulocytes': 'Immature granulocytes %',
    'immature grans (abs)': 'Immature granulocytes, absolute', 'carbon dioxide, total': 'CO2 (bicarbonate)',
    'protein, total': 'Total protein', 'bilirubin, total': 'Bilirubin, total', 'ast (sgot)': 'AST',
    'alt (sgpt)': 'ALT', 'alkaline phosphatase, s': 'Alkaline phosphatase', 'globulin, total': 'Globulin',
    'bun/creatinine ratio': 'BUN/Creat Ratio', 'egfr if nonafricn am': 'eGFR', 'hemoglobin a1c': 'Hemoglobin A1c',
    't4,free(direct)': 'Free T4', 'urea nitrogen (bun)': 'BUN', 'carbon dioxide': 'CO2 (bicarbonate)',
    'protein, total, serum': 'Total protein', 'bilirubin, total, serum': 'Bilirubin, total',
    'alkaline phosphatase': 'Alkaline phosphatase', 'ast (sgot)': 'AST', 'alt (sgpt)': 'ALT',
    'egfr': 'eGFR', 'egfr creatinine (ckd-epi 2021)': 'eGFR', 'hemoglobin a1c (hba1c)': 'Hemoglobin A1c',
    'tsh': 'TSH', 'oxygen saturation in arterial blood by pulse oximetry': 'Oxygen saturation (SpO2)', 'cholesterol, total': 'Cholesterol, total', 'hdl cholesterol': 'HDL', 'ldl-cholesterol': 'LDL',
    'triglycerides': 'Triglycerides', 'bun/creatinine ratio': 'BUN/Creat Ratio', 'mean platelet volume': 'MPV', 'triiodothyronine,free': 'Free T3', 'c-reactive protein, quant': 'CRP',
    'thyroid peroxidase (tpo) ab': 'TPO antibodies', 'immunoglobulin a, qn': 'IgA',
})


def _key(name):
    n = re.sub(r'\s+', ' ', name.strip().lower())
    n = re.sub(r',\s*(serum|plasma|blood|whole blood)$', '', n)  # "Glucose, Serum" -> "glucose"
    return n


def normalize(rows):
    """Rename analytes to one standard name and drop duplicates of the same value on the same day."""
    seen, out = set(), []
    for r in rows:
        r = dict(r)
        k = _key(r['analyte'])
        r['analyte'] = SYNONYMS.get(r['analyte'].strip().lower()) or SYNONYMS.get(k) or (
            r['analyte'].strip() if k == r['analyte'].strip().lower() else re.sub(r',\s*(Serum|Plasma|Blood)$', '', r['analyte'].strip(), flags=re.I))
        key = (r['date'], r.get('time'), r['analyte'], r['num'] if r['num'] is not None else r['value'])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# ---- LabCorp "Patient Report" tables -----------------------------------------------------
# "Date Collected: 03/14/2013", then under a "Test  Current Result and Flag ... Units  Reference
# Interval" header, rows such as
#   "RBC 01            4.90          x10E6/uL     3.77-5.28"
#   "MCH     01"  followed by  "      25.5     Low     pg     26.6-33.0"   (value on the next line)
_LC_DATE = re.compile(r'Date [Cc]ollected:\s*(\d{1,2})/(\d{1,2})/(\d{2,4})')
_LC_HEADER = re.compile(r'Current Result and Flag')
_LC_CODE = re.compile(r'\s+\d{2}$')               # the lab-site code after a test name
_LC_RANGE = re.compile(r'^(?:[<>]=?\s*-?\d[\d.,]*|-?\d[\d.,]*\s*-\s*-?\d[\d.,]*)$')
_LC_FLAGS = {'high': 'H', 'low': 'L', 'abnormal': 'A', 'critical': 'C', 'alert': 'A', 'h': 'H', 'l': 'L'}
_LC_VALUE = re.compile(r'^(?:[<>]=?\s*)?-?\d[\d.,]*$|^(negative|positive|not detected|detected|nonreactive|'
                       r'non-reactive|reactive|normal|abnormal|see below|weak positive|equivocal)$', re.I)
_LC_DATE_CELL = re.compile(r'^\d{1,2}/\d{1,2}/\d{2,4}$')


_LC_SITE = re.compile(r'^0\d$')                 # the same code as its own column ("WBC     01")
_LC_JUNK_NAME = re.compile(r'\d\s*-\s*\d|>or=|<or=|^(result \d+|written authorization|ambig abbrev)', re.I)


def _cells(line):
    cells = [c for c in re.split(r'\s{2,}', line.strip()) if c]
    if len(cells) >= 2 and _LC_SITE.match(cells[1]) and not _LC_CODE.search(cells[0]):
        cells.pop(1)
    return cells


def labcorp_labs(text, facility=None):
    """Result rows from LabCorp patient reports (one dict per result, like the other parsers)."""
    if not _LC_HEADER.search(text or ''):
        return []
    out, date, in_table, pending = [], None, False, None
    for raw in (text or '').replace('\f', '\n').splitlines():
        if not raw.strip():
            continue
        m = _LC_DATE.search(raw)
        if m:
            mo, d, y = m.groups()
            y = int(y) + (2000 if len(y) == 2 else 0)
            date = f'{y:04d}-{int(mo):02d}-{int(d):02d}'
            in_table = False
            continue
        if _LC_HEADER.search(raw):
            in_table, pending = True, None
            continue
        if not raw.startswith(' '):  # panel titles, page footers, patient header lines
            in_table = '(Cont.)' in raw
            pending = None
            continue
        if not in_table or not date:
            continue
        cells = _cells(raw)
        if pending is None and cells and not _LC_VALUE.match(cells[0]):
            name = _LC_CODE.sub('', cells[0]).strip()
            rest = cells[1:]
            if len(cells) == 1 and _LC_CODE.search(cells[0]) is None and cells[0].endswith(','):
                pending = ('join', name)  # a name broken over two lines
                continue
            if pending and pending[0] == 'join':
                name = pending[1] + ' ' + name
            if not rest:
                pending = ('value', name)  # the value is on the next line
                continue
            pending = None
        elif pending and pending[0] == 'join' and cells:
            name, rest, pending = pending[1] + ' ' + _LC_CODE.sub('', cells[0]).strip(), cells[1:], None
        elif pending and pending[0] == 'value':
            name, rest, pending = pending[1], cells, None
        else:
            continue
        if not rest or not _LC_VALUE.match(rest[0]) or len(name) > 60 or ':' in name or _LC_JUNK_NAME.search(name):
            continue
        value, flag, unit, ref = rest[0], None, None, None
        tail = rest[1:]
        if tail and tail[0].lower() in _LC_FLAGS:
            flag = _LC_FLAGS[tail.pop(0).lower()]
        # previous result (and its date), when shown, sit between the flag and the units
        if len(tail) >= 2 and _LC_DATE_CELL.match(tail[1]):
            tail = tail[2:]
        elif tail and not re.match(r'^-?\d', value) and _LC_VALUE.match(tail[0]):
            tail = tail[1:]  # "Negative  Negative": the second is last time's result
        for c in tail:
            if _LC_RANGE.match(c):
                ref = c.replace(' ', '')
            elif _LC_DATE_CELL.match(c) or c.lower() in _LC_FLAGS:
                continue
            elif unit is None and len(c) <= 15 and not _LC_VALUE.match(c):
                unit = c
        v = value.replace(',', '')
        try:
            num = None if v[0] in '<>' else float(v)
        except ValueError:
            num = None
        out.append({'date': date, 'analyte': name, 'value': value + (f' {unit}' if unit else ''), 'num': num,
                    'unit': unit, 'ref_range': ref, 'flag': flag or (flag_for(num, ref.replace('-', ' - ')) if ref else None),
                    'facility': facility or 'LabCorp'})
    return out


# ---- MyChart test result pages (saved by the browser plugin) ----------------------------
# "GLUCOSE" / "Normal range: 70 - 200 mg/dL" / "Your value is 368 mg/dLThis value is Abnormal..."
# dated by "Collected on Nov 06, 2026 5:23 PM" or "Collection date:" + "Nov 06, 2026 ...".
_MC_YOUR = re.compile(r'Your value is\s*(.+?)(?=This value is|Normal range:|$)')
_MC_FLAG = re.compile(r'This value is\s*(Abnormal|High|Low|Critical|Normal)', re.I)
_MC_RANGE = re.compile(r'Normal range:\s*(.+?)(?=Your value is|This value is|$)')
_MC_DATE = re.compile(r'(?:Collected on|Collection date:)\s*([A-Z][a-z]{2}) (\d{1,2}), (\d{4})')
_MC_SKIP_NAME = re.compile(r'serial|machine|instrument|device|lot\b|lot #|operator|comment|interpretation', re.I)
_MC_NOT_NAME = re.compile(r'^(view trends|value|results|new|normal range:.*|your value is.*|this value is.*|'
                          r'compare result trends|ask a question|learn more.*|additional information|\d[\d,.]*)$', re.I)


def mychart_labs(text, facility=None):
    if 'Your value is' not in (text or ''):
        return []
    flat = re.sub(r'[ \t]+', ' ', text.replace('\f', '\n'))
    m = _MC_DATE.search(flat.replace('\n', ' '))
    if not m:
        return []
    date = datetime.strptime(' '.join(m.groups()), '%b %d %Y').strftime('%Y-%m-%d')
    lines = [l.strip() for l in flat.splitlines() if l.strip()]
    out, name, rng = [], None, None
    for line in lines:
        if 'Your value is' in line:
            v = _MC_YOUR.search(line)
            rm = _MC_RANGE.search(line)
            ref = (rm.group(1).strip() if rm else rng) or None
            if not v or not name or _MC_SKIP_NAME.search(name):
                name, rng = None, None
                continue
            raw = v.group(1).strip()
            vm = re.match(r'^([<>]=?\s*)?(-?\d[\d,]*\.?\d*)\s*(.*)$', raw)
            unit = num = None
            if vm:
                num_txt = vm.group(2).replace(',', '')
                unit = vm.group(3).strip() or None
                try:
                    num = None if vm.group(1) else float(num_txt)
                except ValueError:
                    num = None
                if num is not None and num >= 1e6:   # an id number, not a result
                    name, rng = None, None
                    continue
            ref_clean, ref_unit = None, None
            if ref:
                rr = re.match(r'^\s*(-?[\d.]+)\s*-\s*(-?[\d.]+)\s*(.*)$', ref)
                if rr:
                    ref_clean, ref_unit = f'{rr.group(1)}-{rr.group(2)}', rr.group(3).strip() or None
                else:
                    ref_clean = ref
            unit = unit or ref_unit
            word = (_MC_FLAG.search(line).group(1).lower() if _MC_FLAG.search(line) else '')
            flag = {'high': 'H', 'low': 'L', 'critical': 'C'}.get(word)
            if not flag and ref_clean and num is not None:
                flag = flag_for(num, ref_clean.replace('-', ' - '))
            if not flag and word == 'abnormal':
                flag = 'A'
            pretty = name.title() if name.isupper() else name
            out.append({'date': date, 'analyte': pretty, 'value': raw, 'num': num, 'unit': unit,
                        'ref_range': ref_clean, 'flag': flag, 'facility': facility})
            name, rng = None, None
        elif line.startswith('Normal range:'):
            rm = _MC_RANGE.search(line)
            rng = rm.group(1).strip() if rm else None
        elif not _MC_NOT_NAME.match(line) and len(line) <= 60 and not line.endswith(':'):
            name = line
    seen, uniq = set(), []
    for r in out:
        k = (r['analyte'], r['value'])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq

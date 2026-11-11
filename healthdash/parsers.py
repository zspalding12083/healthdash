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


def normalize(rows):
    """Rename analytes to one standard name and drop duplicates of the same value on the same day."""
    seen, out = set(), []
    for r in rows:
        r = dict(r)
        r['analyte'] = SYNONYMS.get(r['analyte'].strip().lower(), r['analyte'].strip())
        key = (r['date'], r.get('time'), r['analyte'], r['num'] if r['num'] is not None else r['value'])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out

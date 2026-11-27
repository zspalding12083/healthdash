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
    """'H' or 'L' for a number outside a normal range like '70-99' or '<5', else None."""
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
    """Result rows from hospital lab printouts laid out as blocks under "Collected:" dates."""
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
    'tsh': 'TSH', 'body weight measured': 'Body weight', 'body mass index (bmi) [ratio]': 'BMI',
    'systolic blood pressure': 'Systolic blood pressure', 'diastolic blood pressure': 'Diastolic blood pressure',
    'heart rate sitting': 'Heart rate', 'body surface area derived from formula': 'Body surface area', 'oxygen saturation in arterial blood by pulse oximetry': 'Oxygen saturation (SpO2)', 'cholesterol, total': 'Cholesterol, total', 'hdl cholesterol': 'HDL', 'ldl-cholesterol': 'LDL',
    'triglycerides': 'Triglycerides', 'bun/creatinine ratio': 'BUN/Creat Ratio', 'mean platelet volume': 'MPV', 'triiodothyronine,free': 'Free T3', 'c-reactive protein, quant': 'CRP',
    'thyroid peroxidase (tpo) ab': 'TPO antibodies', 'immunoglobulin a, qn': 'IgA',
    'hgb a1c': 'Hemoglobin A1c', 'hba1c': 'Hemoglobin A1c', 'hemoglobin a1c (hgb a1c)': 'Hemoglobin A1c',
    'glycohemoglobin': 'Hemoglobin A1c', 'glycated hemoglobin': 'Hemoglobin A1c',
})

# The same test as other labs and portals name it. Only names that can't mean anything else.
SYNONYMS.update({
    # cholesterol panel
    'chol': 'Cholesterol, total', 'cholesterol': 'Cholesterol, total',
    'trig': 'Triglycerides', 'hdl': 'HDL', 'hdl chol': 'HDL', 'ldl': 'LDL',
    'ldl chol calc (nih)': 'LDL', 'ldl chol, calculated': 'LDL', 'ldl cholesterol': 'LDL',
    'ldl cholesterol - calculated': 'LDL', 'ldl cholesterol calc': 'LDL',
    'vldl cholesterol cal': 'VLDL', 'vldl, calc': 'VLDL', 'vldl cholesterol': 'VLDL',
    'chol/hdl': 'Cholesterol/HDL ratio', 'chol/hdl ratio': 'Cholesterol/HDL ratio',
    't. chol/hdl ratio': 'Cholesterol/HDL ratio',
    # chemistry
    'glob': 'Globulin', 'globulin': 'Globulin', 'tsh reflex': 'TSH',
    'egfr-naa': 'eGFR', 'egfr ckd-epi 2021': 'eGFR', 'urea nitrogen': 'BUN',
    'alanine aminotransferase': 'ALT', 'aspartate aminotransferase': 'AST',
    'amylase lvl': 'Amylase', 'lipase lvl': 'Lipase', 'ferritin lvl': 'Ferritin', 'vitamin b12 lvl': 'Vitamin B12',
    'prothrombin time (pt)': 'Prothrombin time', 'protime': 'Prothrombin time',
    'microalb/creat ratio': 'Microalbumin/creatinine ratio', 'microalbumin/creat ratio': 'Microalbumin/creatinine ratio',
    # blood counts
    'lymph': 'Lymphocytes %', 'lymphocytes': 'Lymphocytes %', 'basophils': 'Basophils %',
    'eosinophils': 'Eosinophils %', 'polys': 'Neutrophils %', 'neutrophils, %': 'Neutrophils %',
    'absolute neutrophils': 'Neutrophils, absolute', 'polys, abs. count': 'Neutrophils, absolute',
    'absolute lymph': 'Lymphocytes, absolute', 'lymphs, abs. count': 'Lymphocytes, absolute',
    'lymphocytes (absolute)': 'Lymphocytes, absolute',
    'absolute mono': 'Monocytes, absolute', 'monos, abs. count': 'Monocytes, absolute',
    'absolute eos': 'Eosinophils, absolute', 'eos, abs. count': 'Eosinophils, absolute',
    'eosinophils (absolute)': 'Eosinophils, absolute',
    'absolute baso': 'Basophils, absolute', 'basos, abs. count': 'Basophils, absolute',
    'basophils (absolute)': 'Basophils, absolute',
    'absolute immature gran': 'Immature granulocytes, absolute', 'imm gran #': 'Immature granulocytes, absolute',
    'ig, abs. count': 'Immature granulocytes, absolute', 'ig absolute': 'Immature granulocytes, absolute',
    'imm gran %': 'Immature granulocytes %',
    'red blood cell (rbc) count': 'RBC', 'white blood cell (wbc) count': 'WBC',
    'rdw- cv': 'RDW', 'rdw cv': 'RDW', 'rdw- sd': 'RDW-SD', 'rdw sd': 'RDW-SD',
    # vital signs
    'bmi': 'BMI', 'body mass index measured': 'BMI', 'body mass index calculated': 'BMI',
    'clinical weight': 'Body weight', 'height/length measured': 'Body height',
    'body surface area calculated': 'Body surface area',
    'heart rate': 'Heart rate', 'heart rate monitored': 'Heart rate', 'peripheral pulse rate': 'Heart rate',
    'respiratory rate': 'Respiratory rate', 'spo2': 'Oxygen saturation (SpO2)',
    # long official (LOINC) names some hospital portals use
    'alanine aminotransferase [enzymatic activity/volume] in serum or plasma': 'ALT',
    'aspartate aminotransferase [enzymatic activity/volume] in serum or plasma': 'AST',
    'albumin [mass/volume] in serum or plasma': 'Albumin',
    'alkaline phosphatase [enzymatic activity/volume] in serum or plasma': 'Alkaline phosphatase',
    'bilirubin.total [mass/volume] in serum or plasma': 'Bilirubin, total',
    'calcium [mass/volume] in serum or plasma': 'Calcium',
    'carbon dioxide, total [moles/volume] in serum or plasma': 'CO2 (bicarbonate)',
    'chloride [moles/volume] in serum or plasma': 'Chloride',
    'creatinine [mass/volume] in serum or plasma': 'Creatinine',
    'glucose [mass/volume] in serum or plasma': 'Glucose',
    'glucose [mass/volume] in capillary blood by glucometer': 'Glucose (fingerstick)',
    'hematocrit [volume fraction] of blood by automated count': 'Hematocrit',
    'hemoglobin [mass/volume] in blood': 'Hemoglobin',
    'leukocytes [#/volume] in blood by automated count': 'WBC',
    'erythrocytes [#/volume] in blood': 'RBC',
    'lipase [enzymatic activity/volume] in serum or plasma': 'Lipase',
    'magnesium [mass/volume] in serum or plasma': 'Magnesium',
    'mch [entitic mass] by automated count': 'MCH',
    'mchc [mass/volume] in blood from fetus by automated count': 'MCHC',
    'mcv [entitic volume] by automated count': 'MCV',
    'platelets [#/volume] in blood by automated count': 'Platelets',
    'platelet mean volume [entitic volume] in blood by automated count': 'MPV',
    'erythrocyte distribution width [entitic volume] by automated count': 'RDW-SD',
    'potassium [moles/volume] in serum or plasma': 'Potassium',
    'protein [mass/volume] in serum or plasma': 'Total protein',
    'sodium [moles/volume] in serum or plasma': 'Sodium',
    'urea nitrogen [mass/volume] in serum or plasma': 'BUN',
    'inr in blood by coagulation assay': 'INR',
    'baso auto': 'Basophils %',
})

# Urinalysis: every lab names these differently ("UA Spec Grav", "Specific Gravity, Ur (S.G.)", ...)
for _std, _names in {
    'Urine RBC': ['ua rbc', 'rbc, urine', 'rbc,urine'],
    'Urine WBC': ['ua wbc', 'wbc, urine', 'wbc urine'],
    'Urine bacteria': ['ua bacteria', 'bacteria, urine'],
    'Urine bilirubin': ['ua bili', 'bilirubin, urine', 'bilirubin, urine dipstick', 'urine bilirubin'],
    'Urine blood': ['ua blood', 'blood, urine', 'blood, urine dipstick'],
    'Urine glucose': ['ua glucose', 'glucose, urine, qual', 'glucose, urine dipstick', 'glucose,urine'],
    'Urine ketones': ['ua ketones', 'ketone, qual', 'ketones, urine dipstick'],
    'Urine leukocyte esterase': ['ua leuk est', 'leukocyte esterase', 'leukocytes, urine dipstick'],
    'Urine nitrite': ['ua nitrite', 'nitrite', 'nitrite, ur (ni)', 'nitrite, urine dipstick', 'nitrites, urine'],
    'Urine pH': ['ua ph', 'ph, ur', 'ph, urine dipstick'],
    'Urine protein': ['ua protein', 'protein, total, random urine', 'protein, urine dipstick', 'protein,urine'],
    'Urine specific gravity': ['ua spec grav', 'specific gravity', 'specific gravity, ur (s.g.)',
                               'specific gravity, urine dipst*', 'specific gravity, urine dipstick'],
    'Urine urobilinogen': ['ua urobilinogen', 'urobilinogen', 'urobilinogen, urine dipstick'],
    'Urine epithelial cells': ['ua epithelial', 'epithelial cells'],
    'Urine color': ['ua color'],
    'Urine appearance': ['ua appear', 'character'],
}.items():
    for _n in _names:
        SYNONYMS[_n] = _std

# Not results: device and staff details that some portals put in the result list
JUNK_ANALYTE = re.compile(r'serial ?#|serial number|operator name|^operator\b|lot (number|#)|\banalyzer\b|'
                          r'^(analyzed|draw|collection|collected|received) (date|time)$|^name of machine$|'
                          r'\b(collected|performed) by\b|comment\b|\bnote$|^specimen outdate$|\bbf type\b|^bf type', re.I)


def _key(name):
    n = re.sub(r'\s+', ' ', name.strip().lower())
    n = _tidy(n)
    n = re.sub(r',\s*(serum|plasma|blood|whole blood)$', '', n)  # "Glucose, Serum" -> "glucose"
    return n


def _tidy(name):
    """'Hgb A1c.' -> 'Hgb A1c'; 'Hemoglobin A1c A,' (a flag column read into the name) -> 'Hemoglobin A1c'."""
    name = re.sub(r'\s+[Aa],\s*$', '', name.strip())
    return name.rstrip(' .,:;')


def display_name(name):
    """How a name is shown: a standard name as written ("BMI", "eGFR"), other all-capitals names in
    title case ("GLUCOSE POC" -> "Glucose Poc"); every spelling of a standard name in capitals ("Bmi",
    "Wbc", from older versions) becomes the standard spelling."""
    canon = _canonical()
    if name in canon.values():
        return name
    if name.lower() in canon:
        return canon[name.lower()]
    return name.title() if name.isupper() else name


def _canonical():
    global _CANON
    if _CANON is None:
        _CANON = {v.lower(): v for v in SYNONYMS.values()}
    return _CANON


_CANON = None


def standard_name(name):
    """The one name an analyte is shown under."""
    k = _key(name)
    return SYNONYMS.get(name.strip().lower()) or SYNONYMS.get(k) or (
        _tidy(name) if k == _tidy(name).lower() else re.sub(r',\s*(Serum|Plasma|Blood)$', '', _tidy(name), flags=re.I))


def normalize(rows):
    """Rename analytes to one standard name and drop duplicates of the same value on the same day."""
    seen, out = set(), []
    for r in rows:
        r = dict(r)
        if JUNK_ANALYTE.search(r['analyte']):
            continue
        r['analyte'] = standard_name(r['analyte'])
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
_LC_LETTER = re.compile(r'\s+[A-D],?$')          # a footnote letter after the name: "BUN B", "Potassium B,"
_LC_KEEP_LETTER = re.compile(r'(vitamin|hepatitis|hep|type|factor|influenza|flu|group|apolipoprotein|strep|'
                             r'streptococcus|protein|blood|lipoprotein|immunoglobulin|ig)$', re.I)


def labcorp_name(name):
    """'BUN B' -> 'BUN'; 'Vitamin B' and 'Hepatitis A' stay as they are."""
    m = _LC_LETTER.search(name)
    if m and not _LC_KEEP_LETTER.search(name[:m.start()].strip()):
        return name[:m.start()].strip()
    return name
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
        out.append({'date': date, 'analyte': labcorp_name(name), 'value': value + (f' {unit}' if unit else ''), 'num': num,
                    'unit': unit, 'ref_range': ref, 'flag': flag or (flag_for(num, ref.replace('-', ' - ')) if ref else None),
                    'facility': facility or 'LabCorp'})
    return out


# ---- MyChart test result pages (printed or saved from the portal) ----------------------------
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
    """Result rows from a MyChart test result page."""
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

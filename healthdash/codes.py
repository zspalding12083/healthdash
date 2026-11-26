"""Medical codes on conditions and procedures: ICD-10-CM, ICD-9-CM, SNOMED CT, CPT, HCPCS, ICD-10-PCS.

A record keeps its codes in one text field, written the same way everywhere:

    "ICD-10-CM M54.50; SNOMED CT 279039007"

Codes come from health summaries (C-CDA codeSystem OIDs, FHIR coding systems) or are typed by a
person, who may write just the code ("M54.50", "22612") or the system and code ("cpt 22612"). No
code tables are shipped and nothing is looked up outside: the dashboard stores and shows the codes
the records give, and checks only that a typed code has the right shape for its system.
"""

import re

# C-CDA codeSystem OID -> name shown
OIDS = {
    '2.16.840.1.113883.6.90': 'ICD-10-CM',
    '2.16.840.1.113883.6.3': 'ICD-10',
    '2.16.840.1.113883.6.103': 'ICD-9-CM',
    '2.16.840.1.113883.6.104': 'ICD-9-CM',   # volume 3, procedures
    '2.16.840.1.113883.6.96': 'SNOMED CT',
    '2.16.840.1.113883.6.12': 'CPT',
    '2.16.840.1.113883.6.285': 'HCPCS',
    '2.16.840.1.113883.6.14': 'HCPCS',
    '2.16.840.1.113883.6.4': 'ICD-10-PCS',
}
# FHIR coding.system -> name shown
URLS = {
    'http://hl7.org/fhir/sid/icd-10-cm': 'ICD-10-CM',
    'http://hl7.org/fhir/sid/icd-10': 'ICD-10',
    'http://hl7.org/fhir/sid/icd-9-cm': 'ICD-9-CM',
    'http://snomed.info/sct': 'SNOMED CT',
    'http://www.ama-assn.org/go/cpt': 'CPT',
    'urn:oid:2.16.840.1.113883.6.12': 'CPT',
    'http://www.cms.gov/medicare/coding/medhcpcsgeninfo': 'HCPCS',
    'urn:oid:2.16.840.1.113883.6.285': 'HCPCS',
    'http://www.cms.gov/medicare/coding/icd10': 'ICD-10-PCS',
}
SYSTEMS = ['ICD-10-CM', 'ICD-10', 'ICD-9-CM', 'SNOMED CT', 'CPT', 'HCPCS', 'ICD-10-PCS']
# what a person may type for each system (lower case, spaces and dashes removed)
ALIASES = {
    'icd10cm': 'ICD-10-CM', 'icd10': 'ICD-10-CM', 'icd': 'ICD-10-CM', 'dx': 'ICD-10-CM',
    'icd9cm': 'ICD-9-CM', 'icd9': 'ICD-9-CM',
    'snomedct': 'SNOMED CT', 'snomed': 'SNOMED CT', 'sct': 'SNOMED CT',
    'cpt': 'CPT', 'cpt4': 'CPT', 'hcpcs': 'HCPCS', 'icd10pcs': 'ICD-10-PCS', 'pcs': 'ICD-10-PCS',
}
SHAPE = {   # the form of a code in each system
    'ICD-10-CM': re.compile(r'^[A-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?$'),
    'ICD-10': re.compile(r'^[A-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?$'),
    'ICD-9-CM': re.compile(r'^([0-9]{2,3}|V[0-9]{2}|E[0-9]{3})(\.[0-9]{1,2})?$'),
    'SNOMED CT': re.compile(r'^[0-9]{6,18}$'),
    'CPT': re.compile(r'^[0-9]{4}[0-9FTU]$'),
    'HCPCS': re.compile(r'^[A-V][0-9]{4}$'),
    'ICD-10-PCS': re.compile(r'^[0-9A-HJ-NP-Z]{7}$'),
}
HINT = 'for example "ICD-10 M54.50" or "CPT 22612"; separate several with ";"'


class CodeError(ValueError):
    pass


def _guess(code, kind):
    """The system of a code typed without one, from its shape and whether it's a condition."""
    if SHAPE['SNOMED CT'].match(code):
        return 'SNOMED CT'
    if kind == 'procedures':
        for s in ('CPT', 'HCPCS', 'ICD-10-PCS'):
            if SHAPE[s].match(code):
                return s
    elif SHAPE['ICD-10-CM'].match(code) or re.match(r'^[A-Z][0-9]{2}[0-9A-Z]{1,4}$', code):
        return 'ICD-10-CM'
    elif SHAPE['ICD-9-CM'].match(code):
        return 'ICD-9-CM'
    return None


def _icd_dot(code):
    """ICD-10 codes written without the dot ("M5450") get it back ("M54.50")."""
    if len(code) > 3 and '.' not in code:
        return code[:3] + '.' + code[3:]
    return code


def one(system, code):
    """'SYSTEM CODE' for a code from a record, or None when it isn't one we keep."""
    code = (code or '').strip().upper()
    if not system or not code or code in ('UNK', 'NI', 'NA'):
        return None
    if system in ('ICD-10-CM', 'ICD-10'):
        code = _icd_dot(code)
    return f'{system} {code}'


def from_ccda(el, ns):
    """Codes on a C-CDA coded element (its own code and its translations), as 'SYSTEM CODE' strings."""
    out = []
    if el is None:
        return out
    for c in [el] + list(el.iterfind(ns + 'translation')):
        s = one(OIDS.get(c.get('codeSystem') or ''), c.get('code'))
        if s and s not in out:
            out.append(s)
    return out


def from_fhir(cc):
    """Codes in a FHIR CodeableConcept, as 'SYSTEM CODE' strings."""
    out = []
    for cd in (cc or {}).get('coding') or []:
        s = one(URLS.get((cd.get('system') or '').lower().rstrip('/')), cd.get('code'))
        if s and s not in out:
            out.append(s)
    return out


def split(text):
    """'ICD-10-CM M54.50; CPT 22612' -> [('ICD-10-CM', 'M54.50'), ('CPT', '22612')]."""
    out = []
    for part in re.split(r'[;\n]+', text or ''):
        part = part.strip()
        if not part:
            continue
        for s in sorted(SYSTEMS, key=len, reverse=True):
            if part.upper().startswith(s.upper() + ' '):
                out.append((s, part[len(s):].strip()))
                break
        else:
            out.append(('', part))
    return out


def join(pairs):
    """[(system, code)] -> the stored text, each code once; None when there are none."""
    seen, out = set(), []
    for s, c in pairs:
        t = f'{s} {c}'.strip()
        if t and t.upper() not in seen:
            seen.add(t.upper())
            out.append(t)
    return '; '.join(out) or None


def normalize(text, kind=None):
    """Codes as typed by a person -> the stored form. Raises CodeError for one that can't be read."""
    pairs = []
    for part in re.split(r'[;,\n]+', text or ''):
        part = part.strip()
        if not part:
            continue
        m = re.match(r'^(.*?)[\s:]*([A-Za-z0-9]+(?:\.[A-Za-z0-9]+)?)$', part)
        label, code = (m.group(1), m.group(2).upper()) if m else ('', part.upper())
        key = re.sub(r'[\s\-_.]', '', label.lower())
        if key:   # the system was written out: its full name or a short form
            system = ALIASES.get(key) or next(
                (s for s in SYSTEMS if s.lower().replace(' ', '').replace('-', '') == key), None)
        else:     # just the code: tell the system from its shape
            system = _guess(code, kind)
        if not system:
            raise CodeError(f'"{part}" is not a code this dashboard knows ({HINT})')
        if system in ('ICD-10-CM', 'ICD-10'):
            code = _icd_dot(code)
        if not SHAPE[system].match(code):
            raise CodeError(f'"{code}" does not look like a {system} code ({HINT})')
        pairs.append((system, code))
    return join(pairs)


def merge(*texts):
    """Every code in any of the texts, once, in the order first seen."""
    return join([p for t in texts for p in split(t)])

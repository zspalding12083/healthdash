"""Read C-CDA documents (the standard "Health Summary" / "Summary of Care" files that patient
portals such as MyChart export, often as an IHE XDM zip of DOC0001.XML, DOC0002.XML, ...).

Each document becomes: the visit it describes, a readable page of its narrative, and structured
entries (results and vital signs, medications, problems, allergies). Python standard library
only; documents with a DOCTYPE or entity declarations are refused.
"""

import hashlib
import html
import io
import re
import zipfile
import xml.etree.ElementTree as ET

N = '{urn:hl7-org:v3}'
XSI_TYPE = '{http://www.w3.org/2001/XMLSchema-instance}type'
MAX_MEMBERS, MAX_XML = 2000, 50 * 1024 * 1024
SECTIONS = {  # LOINC section code -> what it is
    '30954-2': 'results', '8716-3': 'vitals', '10160-0': 'medications', '11450-4': 'problems',
    '11348-0': 'resolved', '48765-2': 'allergies', '46240-8': 'encounters', '29299-5': 'reason',
    '51848-0': 'diagnoses', '10164-2': 'notes', '47519-4': 'procedures',
}
FLAGS = {'H': 'H', 'HH': 'H', 'HU': 'H', 'L': 'L', 'LL': 'L', 'LU': 'L', 'A': 'A', 'AA': 'A', 'C': 'C'}


class NotCCDA(ValueError):
    pass


def _t(el):
    """Text of an element; table cells and paragraphs kept apart by spaces."""
    if el is None:
        return ''
    s = re.sub(r'\s+', ' ', ' '.join(el.itertext())).strip()
    return re.sub(r'\s+([,.;:)])', r'\1', re.sub(r'\(\s+', '(', s))


# mixed into injections given in clinic; not medications in their own right
DILUENTS = re.compile(r'^(water|sterile water|sodium chloride 0\.9 ?%?|normal saline|saline|bacteriostatic water)\b', re.I)

# rows in Epic result tables that label other rows rather than being results
NOT_RESULTS = re.compile(r'^(units?|lab interpretation|interpretation|comments?|narrative|result|note|'
                         r'reference range|specimen|component|value)$', re.I)


def _rows(el):
    """Text of each table row's cells (header rows skipped)."""
    out = []
    for tr in el.iter(N + 'tr'):
        cells = [_t(td) for td in tr.iterfind(N + 'td')]
        if any(cells):
            out.append(' – '.join(c for c in cells if c))
    return out


def _date(v):
    """HL7 timestamp (20261020153000-0400) -> YYYY-MM-DD (or YYYY-MM, YYYY)."""
    m = re.match(r'(\d{4})(\d{2})?(\d{2})?', v or '')
    if not m:
        return None
    y, mo, d = m.groups()
    if int(y) <= 1900:   # 1/1/1900 is how some portals write "no date"
        return None
    return '-'.join(p for p in (y, mo, d) if p)


def _key(*parts):
    return hashlib.sha1('|'.join(str(p or '').lower() for p in parts).encode()).hexdigest()[:16]


def parse(data):
    """One C-CDA document (bytes) -> dict, or NotCCDA."""
    head = data[:4096].lower()
    if b'<!doctype' in head or b'<!entity' in data[:200000].lower():
        raise NotCCDA('XML with a DOCTYPE or entities is not accepted')
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise NotCCDA(f'not readable XML ({e})')
    if root.tag != N + 'ClinicalDocument':
        raise NotCCDA('not a C-CDA clinical document')
    ids, id_el, parent = {}, {}, {}
    for el in root.iter():
        for c in el:
            parent[c] = el
        if el.get('ID'):
            ids[el.get('ID')] = _t(el)
            id_el[el.get('ID')] = el

    def row_date(el):
        """A date in the narrative table row an entry points to (Cerner puts procedure dates there)."""
        r = el.find(N + 'text/' + N + 'reference') if el is not None else None
        if r is None:
            c = el.find(f'{N}code/{N}originalText/{N}reference') if el is not None else None
            r = c
        node = id_el.get((r.get('value') or '').lstrip('#')) if r is not None else None
        while node is not None and node.tag != N + 'tr':
            node = parent.get(node)
        if node is None:
            return None
        for cell in node.iter():
            t = (cell.text or '').strip()
            m = re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})', t)
            if m:
                mo, d, y = m.groups()
                y = int(y) + (2000 if len(y) == 2 and int(y) <= 50 else 1900 if len(y) == 2 else 0)
                return f'{y:04d}-{int(mo):02d}-{int(d):02d}'
            if re.fullmatch(r'(19|20)\d{2}', t):
                return t
        return None

    def ref(el):
        """Text of a coded element: originalText (or its #reference), else displayName."""
        if el is None:
            return ''
        ot = el.find(N + 'originalText')
        if ot is not None:
            r = ot.find(N + 'reference')
            if r is not None and r.get('value', '').lstrip('#') in ids:
                return ids[r.get('value').lstrip('#')]
            if _t(ot):
                return _t(ot)
        if el.get('displayName'):
            return el.get('displayName')
        for tr in el.iterfind(N + 'translation'):
            if tr.get('displayName'):
                return tr.get('displayName')
        return ''

    def textref(el):
        r = el.find(N + 'text/' + N + 'reference') if el is not None else None
        return ids.get((r.get('value') or '').lstrip('#'), '') if r is not None else ''

    def when(el):
        if el is None:
            return None
        et = el.find(N + 'effectiveTime')
        if et is None:
            return None
        if et.get('value'):
            return _date(et.get('value'))
        lo = et.find(N + 'low')
        return _date(lo.get('value')) if lo is not None else None

    out = {'title': _t(root.find(N + 'title')) or 'Health summary', 'labs': [], 'medications': [],
           'problems': [], 'allergies': [], 'procedures': [], 'sections': []}
    eff = root.find(N + 'effectiveTime')
    out['doc_date'] = _date(eff.get('value')) if eff is not None else None
    cust = root.find(f'.//{N}custodian//{N}representedCustodianOrganization/{N}name')
    out['facility'] = _t(cust) or None
    enc = root.find(f'{N}componentOf/{N}encompassingEncounter')
    out['visit'] = None
    if enc is not None:
        lo = enc.find(f'{N}effectiveTime/{N}low')
        hi = enc.find(f'{N}effectiveTime/{N}high')
        org = enc.find(f'.//{N}responsibleParty//{N}representedOrganization/{N}name')
        loc = enc.find(f'.//{N}location//{N}name')
        who = enc.find(f'.//{N}encounterParticipant//{N}assignedPerson/{N}name')
        code = enc.find(N + 'code')
        eid = enc.find(N + 'id')
        out['visit'] = {
            'id': (eid.get('extension') or eid.get('root')) if eid is not None else None,
            'date': _date(lo.get('value')) if lo is not None else out['doc_date'],
            'end_date': _date(hi.get('value')) if hi is not None else None,
            'kind': ref(code) if code is not None else '',
            'facility': _t(org) or _t(loc) or out['facility'],
            'location': _t(loc) or None,
            'provider': ' '.join(_t(p) for p in who) if who is not None else None,
        }
        out['facility'] = out['visit']['facility'] or out['facility']
        if not out['visit']['date'] and not out['visit']['end_date']:
            out['visit'] = None   # a summary of the whole history, not of one visit

    for sec in root.iter(N + 'section'):
        code = sec.find(N + 'code')
        kind = SECTIONS.get(code.get('code') if code is not None else '')
        title = _t(sec.find(N + 'title'))
        narrative = sec.find(N + 'text')
        out['sections'].append((title, narrative))
        if kind in ('results', 'vitals'):
            for org in sec.iter(N + 'organizer'):
                panel = ref(org.find(N + 'code')) or None
                odate = when(org)
                if not odate:
                    at = org.find(f'{N}author/{N}time')
                    odate = _date(at.get('value')) if at is not None else None
                for obs in org.iter(N + 'observation'):
                    row = _observation(obs, ref, when, odate, panel if kind == 'results' else 'Vital signs')
                    if row:
                        out['labs'].append(row)
        elif kind == 'medications':
            for sa in sec.iter(N + 'substanceAdministration'):
                mat = sa.find(f'.//{N}manufacturedMaterial')
                name = ref(mat.find(N + 'code')) if mat is not None else ''
                if not name and mat is not None:
                    name = _t(mat.find(N + 'name'))
                if not name or DILUENTS.match(name.strip()):
                    continue
                times = [e for e in sa.iterfind(N + 'effectiveTime') if e.find(N + 'low') is not None or e.find(N + 'high') is not None]
                lo = times[0].find(N + 'low') if times else None
                hi = times[0].find(N + 'high') if times else None
                dq = sa.find(N + 'doseQuantity')
                dose = f"{dq.get('value')} {dq.get('unit') or ''}".strip() if dq is not None and dq.get('value') else None
                st = sa.find(N + 'statusCode')
                out['medications'].append({
                    'name': name, 'dose': dose, 'schedule': textref(sa) or None,
                    'start_date': _date(lo.get('value')) if lo is not None and lo.get('value') else None,
                    'end_date': _date(hi.get('value')) if hi is not None and hi.get('value') else None,
                    'status': st.get('code') if st is not None else None})
        elif kind in ('problems', 'resolved'):
            for act in sec.iterfind(f'{N}entry/{N}act'):
                obs = act.find(f'.//{N}observation')
                if obs is None:
                    continue
                name = ref(obs.find(N + 'value')) or textref(obs) or textref(act)
                if not name:
                    continue
                st = act.find(N + 'statusCode')
                out['problems'].append({
                    'name': name, 'first_noted': when(obs) or when(act),
                    'status': 'resolved' if kind == 'resolved' or (st is not None and st.get('code') in ('completed', 'resolved'))
                    else 'active'})
        elif kind == 'procedures':
            for proc in sec.iter(N + 'procedure'):
                name = ref(proc.find(N + 'code')) or textref(proc)
                if not name or name.lower() in ('procedure', 'none'):
                    continue
                st = proc.find(N + 'statusCode')
                if st is not None and st.get('code') in ('cancelled', 'aborted', 'nullified'):
                    continue
                who = proc.find(f'.//{N}performer//{N}assignedPerson/{N}name')
                date = when(proc) or row_date(proc)
                name = re.sub(r'\s*\(None\)', '', name)
                m = re.search(r'\s+(\d{1,2})/((?:19|20)\d{2})\s*$', name)   # "LAMINECTOMY 9/2017"
                if m:
                    date = date or f'{m.group(2)}-{int(m.group(1)):02d}'
                    name = name[:m.start()]
                name = re.sub(r'\s+\d{1,2}$', '', name).strip(' ,;')     # table footnote numbers
                out['procedures'].append({'name': name, 'date': date,
                                          'provider': _t(who) or None})
        elif kind == 'allergies':
            for act in sec.iterfind(f'{N}entry/{N}act'):
                obs = act.find(f'.//{N}observation')
                if obs is None or obs.get('negationInd') == 'true':
                    continue
                pe = obs.find(f'.//{N}participant//{N}playingEntity')
                substance = (ref(pe.find(N + 'code')) or _t(pe.find(N + 'name'))) if pe is not None else ''
                substance = substance or textref(act)
                if not substance or NOT_ALLERGY.match(substance.strip()):
                    continue
                reaction = ''
                for er in obs.iterfind(N + 'entryRelationship'):
                    o2 = er.find(N + 'observation')
                    if o2 is not None and o2.find(N + 'value') is not None:
                        v = ref(o2.find(N + 'value'))
                        if v and v.lower() not in ('active', 'inactive', 'resolved'):
                            reaction = v
                            break
                out['allergies'].append({'substance': substance, 'reaction': reaction or None,
                                         'first_noted': when(obs) or when(act)})
    if out['visit'] and (not out['visit']['kind'] or not out['visit']['provider']):
        for sec in root.iter(N + 'section'):
            code = sec.find(N + 'code')
            if code is None or code.get('code') != '46240-8':
                continue
            e = sec.find(f'.//{N}encounter')
            if e is not None:
                out['visit']['kind'] = out['visit']['kind'] or ref(e.find(N + 'code'))
                p = e.find(f'.//{N}performer//{N}assignedPerson/{N}name')
                out['visit']['provider'] = out['visit']['provider'] or _t(p) or None
            break
    if out['visit']:
        for title, nar in out['sections']:
            low = title.lower()
            if nar is None:
                continue
            if 'reason for visit' in low and not out['visit'].get('reason'):
                txt = ('; '.join(_rows(nar)) or _t(nar))[:300]
                out['visit']['reason'] = None if not txt or FILLER.search(txt) else txt
            if 'diagnos' in low and not out['visit'].get('diagnoses'):
                txt = ('; '.join(_rows(nar)) or _t(nar))[:500]
                out['visit']['diagnoses'] = None if not txt or FILLER.search(txt) else txt
    return out


def _observation(obs, ref, when, odate, panel):
    v = obs.find(N + 'value')
    if v is None or v.get('nullFlavor'):
        return None
    name = ref(obs.find(N + 'code'))
    if not name or NOT_RESULTS.match(name.strip()):
        return None
    kind = (v.get(XSI_TYPE) or '').split(':')[-1]
    num = unit = None
    if kind == 'PQ' and v.get('value'):
        try:
            num = float(v.get('value'))
        except ValueError:
            return None
        unit = v.get('unit') if v.get('unit') not in (None, '1') else None
        value = f"{v.get('value')} {unit}".strip() if unit else v.get('value')
    elif kind in ('ST', 'ED'):
        value = _t(v)
    elif kind == 'CD' or kind == 'CE':
        value = ref(v)
    elif kind == 'INT' and v.get('value'):
        num, value = float(v.get('value')), v.get('value')
    else:
        value = v.get('value') or _t(v)
    if not value or len(value) > 200:
        return None
    rr = obs.find(f'{N}referenceRange/{N}observationRange')
    ref_range = None
    if rr is not None:
        lo, hi = rr.find(f'{N}value/{N}low'), rr.find(f'{N}value/{N}high')
        if lo is not None and hi is not None and lo.get('value') and hi.get('value'):
            ref_range = f"{lo.get('value')}-{hi.get('value')}"
        else:
            ref_range = _t(rr.find(N + 'text')) or None
    ic = obs.find(N + 'interpretationCode')
    flag = FLAGS.get(ic.get('code')) if ic is not None and ic.get('code') else None
    date = when(obs) or odate
    if not date:
        return None
    return {'date': date, 'panel': panel, 'analyte': name, 'value': value, 'num': num, 'unit': unit,
            'ref_range': ref_range, 'flag': flag}


def page(doc):
    """A readable HTML page of a document's narrative (shown and searched on the dashboard)."""
    parts = [f'<h1>{html.escape(doc["title"])}</h1>']
    v = doc.get('visit')
    if v:
        bits = [v.get('kind'), v.get('date'), v.get('facility'), v.get('location'), v.get('provider')]
        parts.append('<p>' + ' · '.join(html.escape(b) for b in bits if b) + '</p>')
    for title, nar in doc['sections']:
        if nar is None or not _t(nar):
            continue
        parts.append(f'<h2>{html.escape(title)}</h2>' + _narrative_html(nar))
    return ('<!doctype html><html><head><meta charset="utf-8"><title>' + html.escape(doc['title']) +
            '</title></head><body>' + '\n'.join(parts) + '</body></html>')


_KEEP = {'table': 'table', 'thead': 'thead', 'tbody': 'tbody', 'tr': 'tr', 'td': 'td', 'th': 'th',
         'paragraph': 'p', 'list': 'ul', 'item': 'li', 'br': 'br', 'caption': 'caption', 'content': 'span'}


def _narrative_html(el):
    """C-CDA narrative block -> plain safe HTML (only structural tags, text escaped)."""
    def walk(e):
        tag = e.tag.replace(N, '')
        out = html.escape(e.text or '')
        for c in e:
            out += walk(c) + html.escape(c.tail or '')
        h = _KEEP.get(tag)
        if h == 'br':
            return '<br>'
        return f'<{h}>{out}</{h}>' if h else out
    return '<div>' + walk(el) + '</div>'


def documents_in(data, name=''):
    """[(name, xml bytes)] from an IHE XDM zip or a single XML file."""
    if data[:2] == b'PK':
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise NotCCDA('not a readable zip file')
        members = [i for i in z.infolist() if not i.is_dir()]
        if len(members) > MAX_MEMBERS:
            raise NotCCDA('too many files in the zip')
        out = []
        for i in members:
            base = i.filename.rsplit('/', 1)[-1]
            if not re.match(r'(?i)^.+\.xml$', base) or base.upper() == 'METADATA.XML' or i.file_size > MAX_XML:
                continue
            out.append((i.filename, z.read(i)))
        return out
    return [(name or 'document.xml', data)]


def zip_extras(data):
    """Other things in an export zip: a FHIR bundle (.json), scans (.png/.jpg/.pdf), HTML documents
    that have no XML twin, and the names of files that are left out."""
    out = {'json': [], 'scans': [], 'html': [], 'skipped': []}
    if data[:2] != b'PK':
        return out
    z = zipfile.ZipFile(io.BytesIO(data))
    names = [i for i in z.infolist() if not i.is_dir()]
    stems = {re.sub(r'(?i)\.(xml|html?|txt|rtf)$', '', i.filename) for i in names if i.filename.lower().endswith('.xml')}
    for i in names:
        low = i.filename.lower()
        stem = re.sub(r'(?i)\.(xml|html?|txt|rtf)$', '', i.filename)
        if i.file_size > MAX_XML * 2:
            out['skipped'].append(i.filename)
        elif low.endswith('.json'):
            out['json'].append((i.filename, z.read(i)))
        elif low.endswith(('.png', '.jpg', '.jpeg', '.pdf', '.tif', '.tiff')) and 'patientphoto' not in low:
            out['scans'].append((i.filename, z.read(i)))
        elif low.endswith(('.html', '.htm')) and stem not in stems:
            out['html'].append((i.filename, z.read(i)))
        elif low.endswith(('.txt', '.rtf', '.html', '.htm')) and stem in stems:
            continue   # a printable copy of an XML document that is imported
        elif not low.endswith('.xml'):
            out['skipped'].append(i.filename)
    return out


FILLER = re.compile(r'no data available for this section|no information|not on file', re.I)
NOT_ALLERGY = re.compile(r'^(unable|no known|nkda|nka\b|none\b|not on file|unknown)', re.I)
_FORM_WORDS = set('oral tablet tablets tab tabs capsule capsules caps cap solution injection injectable extended '
                  'release delayed hcl hydrochloride sodium sulfate mg mcg ml the and per dose pen subcutaneous topical '
                  'cream patch film coated chewable dispersible enteric er dr sr xl cpcr cpep tbec tb24 tbcr cp24 or po '
                  'each kit inhaler aerosol hfa unit units iv intravenous premix bag vial syringe suspension drops '
                  'ophthalmic nasal spray powder packet liquid elixir syrup lozenge gel ointment lotion rectal '
                  'suppository tromethamine succinate tartrate potassium calcium besylate maleate mesylate'.split())


def drug_key(name):
    """The drug a medication line is about ("EXAMPLOL HCl 2 MG Oral Tablet" -> "examplol")."""
    n = re.sub(r'\(.*?\)', ' ', (name or '').lower())
    words = [w for w in re.findall(r'[a-z][a-z-]{2,}', n) if w not in _FORM_WORDS]
    return words[0] if words else n.strip()[:20]


_CREDENTIALS = {'md', 'do', 'pa', 'pac', 'np', 'fnp', 'aprn', 'rn', 'dpm', 'dds', 'dmd', 'od', 'phd', 'facs', 'facp',
                'facc', 'facog', 'msn', 'mph', 'crna', 'anp', 'cnm', 'dnp', 'lcsw', 'rpac', 'cot', 'mbbs', 'pt', 'dpt', 'ms', 'dr', 'jr', 'sr', 'ii', 'iii'}


def people(line):
    """The people one provider line names: "Dr. Pat Example (attending); ER: Dr. Sam Sample" ->
    ["Dr. Pat Example", "Dr. Sam Sample"], with notes in brackets and labels like "ER:" taken off."""
    out = []
    for part in re.split(r';|\band\b|\s/\s', line or ''):
        part = re.sub(r'\([^()]*\)', ' ', part)
        part = re.sub(r'^\s*([A-Za-z][\w -]{0,20}:|(sent|referred) by\b)', ' ', part, flags=re.I)
        part = re.sub(r'\s+', ' ', part).strip(' ,.-')
        if part:
            out.append(part)
    return out


def doctor_key(name):
    """The person a provider line is about, ignoring credentials, middle names and notes in
    brackets ("Pat Q. Example MD" and "Dr. Pat Example (cardiology)" -> "pat example"); None when
    it isn't a person's name."""
    name = (people(name) or [''])[0]
    words = [w for w in re.findall(r"[a-z][a-z'-]*", (name or '').lower()) if w.replace('-', '') not in _CREDENTIALS]
    words = [w for w in words if len(w) > 1]
    if len(words) < 2 or not re.search(r'[a-z]', name or ''):
        return None   # "MD ER", a department, a lone surname
    return f'{words[0]} {words[-1]}'


def place(*parts):
    """'Clinic / Clinic' -> 'Clinic': join location and facility without repeating one."""
    out = []
    for x in parts:
        if x and x.strip() and x.strip().lower() not in (o.lower() for o in out):
            out.append(x.strip())
    return ' / '.join(out)


def record_ids(kind, *parts):
    return f'ccda-{kind}-{_key(*parts)}'

"""Read a FHIR R4 Bundle (JSON), e.g. the AllPatientData export from FollowMyHealth.

Gives the same kinds of entries as ccda.py: visits (encounters, with type and doctor from the
appointment that day), vital signs and results, medications, problems, allergies, procedures,
plus readable pages for portal messages and family history, and a map
of the export's attached files to their type and date. Python standard library only.
"""

import html
import json
import re
from collections import defaultdict


class NotFHIR(ValueError):
    pass


def _date(v):
    m = re.match(r'(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?', v or '')
    if not m or m.group(1) in ('0001', '1900'):
        return None
    return '-'.join(p for p in m.groups() if p)


def _cc(c):
    """Text of a CodeableConcept."""
    if not c:
        return ''
    if c.get('text'):
        return c['text'].strip()
    for cd in c.get('coding') or []:
        if cd.get('display'):
            return cd['display'].strip()
    return ''


def _status(c):
    return ((c or {}).get('coding') or [{}])[0].get('code')


def parse(data):
    try:
        b = json.loads(data)
    except (ValueError, UnicodeDecodeError) as e:
        raise NotFHIR(f'not readable JSON ({e})')
    if not isinstance(b, dict) or b.get('resourceType') != 'Bundle':
        raise NotFHIR('not a FHIR bundle')
    R = defaultdict(list)
    for e in b.get('entry') or []:
        r = e.get('resource') or {}
        R[r.get('resourceType')].append(r)
    out = {'visits': [], 'labs': [], 'medications': [], 'problems': [], 'allergies': [], 'procedures': [],
           'invoices': [], 'files': {}, 'messages': [], 'family': []}

    # visits: one per day she was seen; type and doctor from the appointment she arrived for
    appts = defaultdict(list)
    for a in R['Appointment']:
        if a.get('status') in ('arrived', 'fulfilled', 'checked-in') and _date(a.get('start')):
            who = [p.get('actor', {}).get('display') for p in a.get('participant') or []
                   if 'Practitioner' in (p.get('actor', {}).get('reference') or '') and p.get('actor', {}).get('display')]
            appts[_date(a['start'])[:10]].append({'kind': _cc(a.get('appointmentType')) or 'Appointment',
                                                  'provider': ', '.join(who) or None,
                                                  'reason': '; '.join(_cc(x) for x in a.get('reasonCode') or [] if _cc(x)) or None})
    for day, items in sorted(appts.items()):
        kinds = list(dict.fromkeys(i['kind'] for i in items))
        out['visits'].append({'date': day, 'kind': kinds[0], 'provider': items[0]['provider'],
                              'reason': items[0]['reason'] or ('; '.join(kinds[1:]) or None)})
    out['encounter_days'] = len({_date((e.get('period') or {}).get('start')) for e in R['Encounter']} - {None})

    # vital signs and results
    for o in R['Observation']:
        when = _date(o.get('effectiveDateTime') or (o.get('effectivePeriod') or {}).get('start') or o.get('issued'))
        if not when:
            continue
        cat = _status(((o.get('category') or [{}])[0]))
        panel = 'Vital signs' if cat == 'vital-signs' else None
        parts = [(o.get('code'), o)] if not o.get('component') else [(c.get('code'), c) for c in o['component']]
        for code, src in parts:
            name = _cc(code)
            row = _value(src)
            if not name or not row:
                continue
            value, num, unit = row
            rr = (src.get('referenceRange') or o.get('referenceRange') or [{}])[0]
            ref = None
            if rr.get('low', {}).get('value') is not None and rr.get('high', {}).get('value') is not None:
                ref = f"{rr['low']['value']}-{rr['high']['value']}"
            elif rr.get('text'):
                ref = rr['text']
            flag = _status((src.get('interpretation') or o.get('interpretation') or [{}])[0])
            out['labs'].append({'date': when, 'panel': panel, 'analyte': name, 'value': value, 'num': num, 'unit': unit,
                                'ref_range': ref, 'flag': {'H': 'H', 'HH': 'H', 'L': 'L', 'LL': 'L', 'A': 'A'}.get(flag)})

    # medications
    for m in R['MedicationStatement'] + R['MedicationRequest']:
        name = _cc(m.get('medicationCodeableConcept')) or (m.get('medicationReference') or {}).get('display') or ''
        if not name:
            continue
        period = m.get('effectivePeriod') or {}
        start = _date(period.get('start') or m.get('effectiveDateTime') or m.get('authoredOn'))
        end = _date(period.get('end') or ((m.get('dispenseRequest') or {}).get('validityPeriod') or {}).get('end'))
        sig = '; '.join(d.get('text') for d in m.get('dosageInstruction') or [] if d.get('text')) or None
        st = m.get('status')
        out['medications'].append({'name': name, 'start_date': start, 'end_date': end, 'schedule': sig,
                                   'status': 'active' if st == 'active' else 'stopped' if st in ('stopped', 'completed', 'cancelled') else None})

    for c in R['Condition']:
        name = _cc(c.get('code'))
        if name:
            out['problems'].append({'name': name, 'status': 'resolved' if _status(c.get('clinicalStatus')) in ('resolved', 'inactive', 'remission') else 'active',
                                    'first_noted': _date(c.get('onsetDateTime') or c.get('recordedDate'))})
    for a in R['AllergyIntolerance']:
        name = _cc(a.get('code'))
        if name and not re.match(r'^(unable|no known|nkda|none\b|unknown)', name.strip(), re.I):
            react = '; '.join(_cc(m) for r in a.get('reaction') or [] for m in r.get('manifestation') or [] if _cc(m))
            out['allergies'].append({'substance': name, 'reaction': react or None})
    for p in R['Procedure']:
        name = _cc(p.get('code'))
        if name:
            out['procedures'].append({'name': name, 'date': _date(p.get('performedDateTime') or (p.get('performedPeriod') or {}).get('start'))})

    for d in R['DocumentReference']:
        for c in d.get('content') or []:
            url = ((c.get('attachment') or {}).get('url') or '').lstrip('./')
            if url:
                out['files'][url] = {'type': _cc(d.get('type')), 'date': _date(c['attachment'].get('creation') or d.get('date'))}
    for c in R['Communication']:
        text = '\n'.join(p.get('contentString') for p in c.get('payload') or [] if p.get('contentString'))
        if text:
            out['messages'].append({'date': _date(c.get('sent') or c.get('received')), 'topic': _cc(c.get('topic')), 'text': text})
    for f in R['FamilyMemberHistory']:
        rel = _cc(f.get('relationship'))
        for c in f.get('condition') or []:
            out['family'].append({'relative': rel, 'condition': _cc(c.get('code'))})
    return out


def _value(src):
    if 'valueQuantity' in src:
        q = src['valueQuantity']
        if q.get('value') is None:
            return None
        unit = q.get('unit') or None
        return (f"{q['value']} {unit}".strip() if unit else str(q['value'])), float(q['value']), unit
    if src.get('valueString'):
        return src['valueString'][:200], None, None
    if src.get('valueCodeableConcept'):
        v = _cc(src['valueCodeableConcept'])
        return (v, None, None) if v else None
    if src.get('valueInteger') is not None:
        return str(src['valueInteger']), float(src['valueInteger']), None
    return None


def messages_page(msgs, title='Portal messages'):
    rows = sorted(msgs, key=lambda m: m['date'] or '')
    body = ''.join(f"<h2>{html.escape(m['date'] or 'Undated')}: {html.escape(m['topic'] or 'Message')}</h2>"
                   f"<p>{html.escape(m['text']).replace(chr(10), '<br>')}</p>" for m in rows)
    return f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>'


def summary_page(f, title):
    parts = [f'<h1>{html.escape(title)}</h1>']
    if f['visits']:
        parts.append('<h2>Visits</h2><table><tr><th>Date</th><th>Type</th><th>Doctor</th></tr>' + ''.join(
            f"<tr><td>{v['date']}</td><td>{html.escape(v['kind'] or '')}</td><td>{html.escape(v['provider'] or '')}</td></tr>"
            for v in f['visits']) + '</table>')
    if f['family']:
        parts.append('<h2>Family history</h2><ul>' + ''.join(
            f"<li>{html.escape(x['relative'] or '')}: {html.escape(x['condition'] or '')}</li>" for x in f['family']) + '</ul>')
    if f['problems']:
        parts.append('<h2>Problems</h2><ul>' + ''.join(
            f"<li>{html.escape(p['name'])} ({p['status']}{', ' + p['first_noted'] if p['first_noted'] else ''})</li>" for p in f['problems']) + '</ul>')
    if f['medications']:
        parts.append('<h2>Medications</h2><ul>' + ''.join(
            f"<li>{html.escape(m['name'])} {m['start_date'] or ''}{' to ' + m['end_date'] if m['end_date'] else ''} ({m['status'] or 'unknown'})</li>"
            for m in f['medications']) + '</ul>')
    return f'<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title></head><body>' + ''.join(parts) + '</body></html>'

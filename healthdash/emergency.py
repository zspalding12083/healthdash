"""The emergency card: one page with what a paramedic, an ER or a new caregiver needs first.

Most of it comes from the records (allergies, current prescription medicines, conditions, recent
procedures). The rest isn't in any medical record, so each person enters it once on the dashboard
(Summary -> Emergency card -> Edit details), kept in their own database as the setting `emergency`:

    dob          date of birth, YYYY-MM-DD
    blood_type   A+, A-, B+, B-, AB+, AB-, O+, O- or unknown
    contacts     up to 4 people to call: {name, relation, phone}
    doctor, pharmacy   {name, phone}
    insurance    plan and member number, as on the insurance card
    devices      implants and devices (pacemaker, stent, joint replacement, insulin pump, ...)
    notes        anything else to know first (advance directive, blood thinner, language, hearing, ...)
    conditions   ids of the conditions to show; none chosen = the most recently noted active ones

card() builds it as printout blocks (see report.py), so it prints as a page or a PDF like the others.
"""

import json
import re
from datetime import date

from .report import FOOTER, _cell, current_meds, fdate
from .store import ValidationError

SETTING = 'emergency'
BLOOD_TYPES = ['A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-', 'unknown']
MAX_CONTACTS = 4
AUTO_CONDITIONS = 12     # shown when none are chosen
MAX_PROCEDURES = 6
MAX_ALLERGY_ROWS = 12  # more than this: only severe ones get a table row
SHORT, LONG = 100, 800   # longest text for a name or phone, and for the free-text fields
SEVERE = re.compile(r'severe|high|anaphyla|life', re.I)


def _text(v, limit, what):
    v = '' if v is None else str(v).strip()
    if len(v) > limit:
        raise ValidationError(f'{what} is too long (up to {limit} characters)')
    return v


def _person(v, what):
    v = v if isinstance(v, dict) else {}
    return {k: _text(v.get(k), SHORT, f'{what} {k}') for k in ('name', 'phone')}


def clean(data):
    """The details as typed -> what is stored. Raises ValidationError for a bad date, blood type or
    anything too long; empty fields are kept empty."""
    data = data if isinstance(data, dict) else {}
    dob = _text(data.get('dob'), 10, 'date of birth')
    if dob and not re.match(r'^\d{4}-\d{2}-\d{2}$', dob):
        raise ValidationError('date of birth must look like 1960-03-07')
    blood = _text(data.get('blood_type'), 8, 'blood type').upper().replace('UNKNOWN', 'unknown')
    if blood and blood not in BLOOD_TYPES:
        raise ValidationError('blood type must be one of ' + ', '.join(BLOOD_TYPES))
    contacts = []
    for c in (data.get('contacts') or [])[:MAX_CONTACTS + 4]:
        c = c if isinstance(c, dict) else {}
        row = {k: _text(c.get(k), SHORT, f'contact {k}') for k in ('name', 'relation', 'phone')}
        if any(row.values()):
            contacts.append(row)
    if len(contacts) > MAX_CONTACTS:
        raise ValidationError(f'up to {MAX_CONTACTS} emergency contacts')
    conds = [str(x) for x in (data.get('conditions') or []) if isinstance(x, (str, int))][:100]
    return {'dob': dob, 'blood_type': blood, 'contacts': contacts,
            'doctor': _person(data.get('doctor'), 'doctor'), 'pharmacy': _person(data.get('pharmacy'), 'pharmacy'),
            'insurance': _text(data.get('insurance'), LONG, 'insurance'),
            'devices': _text(data.get('devices'), LONG, 'implants and devices'),
            'notes': _text(data.get('notes'), LONG, 'other information'), 'conditions': conds}


def load(store):
    """The saved details (all fields, empty when nothing was entered)."""
    try:
        return clean(json.loads(store.get_setting(SETTING) or '{}'))
    except (ValueError, ValidationError):
        return clean({})


def save(store, data):
    d = clean(data)
    store.set_setting(SETTING, json.dumps(d))
    return d


def active_conditions(store):
    """Current conditions, most recently noted first."""
    act = [c for c in store.list('conditions') if (c['status'] or 'active') != 'resolved']
    return sorted(act, key=lambda c: c['first_noted'] or '', reverse=True)


def _age(dob, today):
    try:
        y, m, d = (int(x) for x in dob.split('-'))
        return today.year - y - ((today.month, today.day) < (m, d))
    except ValueError:
        return None


def card(store, name, today=None):
    """The emergency card for one person, as printout blocks."""
    today = today or date.today()
    d = load(store)
    facts = []
    if d['dob']:
        age = _age(d['dob'], today)
        facts.append(f"Born {fdate(d['dob'])}" + (f' (age {age})' if age is not None else ''))
    if d['blood_type']:
        facts.append(f"Blood type {d['blood_type']}")
    facts.append(f'Updated {fdate(today.isoformat())}')
    b = [('h1', f'Emergency health information: {name}'), ('meta', ' · '.join(facts))]

    b.append(('h2', 'Emergency contacts'))
    if d['contacts']:
        b.append(('table', [('Name', 3), ('Relationship', 2), ('Phone', 2.5)],
                  [[_cell(c['name'], bold=True), _cell(c['relation']), _cell(c['phone'], bold=True)] for c in d['contacts']]))
    else:
        b.append(('note', 'None entered yet (Summary, Emergency card, Edit details).'))

    al = store.list('allergies')
    b.append(('h2', f'Allergies ({len(al)})' if al else 'Allergies'))
    if al:
        # a long list keeps the card short: severe ones in the table, the rest as one compact list
        severe = [a for a in al if SEVERE.search(a['severity'] or '')]
        table = al if len(al) <= MAX_ALLERGY_ROWS else severe
        if table:
            b.append(('table', [('Allergic to', 3), ('Reaction', 3), ('Severity', 1.5)],
                      [[_cell(a['substance'], bold=True), _cell(a['reaction']),
                        _cell(a['severity'], flag=a in severe)] for a in table]))
        rest = [a for a in al if a not in table]
        if rest:
            b.append(('strong', ('Also allergic to: ' if table else '')
                      + '; '.join(a['substance'] + (f" ({a['reaction']})" if a['reaction'] else '') for a in rest) + '.'))
    else:
        b.append(('strong', 'No known allergies recorded.'))

    meds = [m for m in current_meds(store) if not m['otc']]
    b.append(('h2', f'Current prescription medicines ({len(meds)})'))
    b.append(('table', [('Medicine', 3), ('Dose', 1.5), ('When', 2.5)],
              [[_cell(m['name'], bold=True), _cell(m['dose']), _cell(m['schedule'])] for m in meds]))
    otc = sum(1 for m in current_meds(store) if m['otc'])
    if otc:
        b.append(('note', f'Also {otc} over-the-counter medicine{"s" if otc != 1 else ""} or supplement{"s" if otc != 1 else ""} '
                          '(on the medicines list).'))

    act = active_conditions(store)
    chosen = [c for c in act if c['id'] in set(d['conditions'])] if d['conditions'] else act[:AUTO_CONDITIONS]
    b.append(('h2', 'Medical conditions'))
    b.append(('table', [('Condition', 4), ('Since', 1.3)],
              [[_cell(c['name'], bold=True), _cell(fdate(c['first_noted']))] for c in chosen]))
    if len(act) > len(chosen):
        b.append(('note', f'{len(act) - len(chosen)} more current conditions are on the dashboard'
                          + ('.' if d['conditions'] else '; choose which appear here under Edit details.')))

    if d['devices']:
        b += [('h2', 'Implants and devices'), ('strong', d['devices'])]

    procs = sorted((p for p in store.list('procedures') if p['date']), key=lambda p: p['date'], reverse=True)[:MAX_PROCEDURES]
    if procs:
        b += [('h2', 'Recent surgeries and procedures'),
              ('table', [('Date', 1.2), ('Procedure', 4), ('Where', 2)],
               [[_cell(fdate(p['date'])), _cell(p['name'], bold=True), _cell(p['facility'])] for p in procs])]

    care = [(label, p) for label, p in (('Doctor', d['doctor']), ('Pharmacy', d['pharmacy'])) if p['name'] or p['phone']]
    if care or d['insurance']:
        rows = [[_cell(label, muted=True), _cell(p['name'], bold=True), _cell(p['phone'])] for label, p in care]
        if d['insurance']:
            rows.append([_cell('Insurance', muted=True), _cell(d['insurance']), _cell('')])
        b += [('h2', 'Doctor, pharmacy and insurance'), ('table', [('', 1.2), ('Name', 4), ('Phone', 2)], rows)]

    if d['notes']:
        b += [('h2', 'Other important information'), ('strong', d['notes'])]
    b.append(('footer', FOOTER))
    return {'title': f'Emergency health information: {name}', 'large': False, 'blocks': b}

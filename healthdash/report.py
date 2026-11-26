"""The printable health summary: one self-contained HTML page of what is going on and when.

Built from the records on the dashboard (nothing is looked up elsewhere) for a new doctor, a
specialist visit or a person's own files. It opens at /report, where the browser prints it or saves
it as a PDF; /report?download=1 gives the same page as a file. /report?view=meds is a short list of
just the current medicines and allergies (med_list), for a wallet, the fridge or a new doctor's form. The page has no outside links,
fonts or scripts beyond its Print button, so a downloaded copy reads the same anywhere.

Sections, in order (each can be left out with the `sections` option):
  overview     current conditions with their codes, allergies, current medications
  procedures   surgeries and procedures, with codes
  life         life events that may bear on health (accidents, family, losses, moves)
  labs         lab results flagged high or low, newest first
  history      a dated list of visits, tests, imaging, procedures, medication changes, vaccines and
               life events, grouped by year
  past         resolved conditions and past medications with when they started and stopped
"""

import html
from datetime import date

SECTIONS = ['overview', 'procedures', 'life', 'labs', 'history', 'past']
HISTORY_KINDS = {'encounters': 'Visit', 'tests': 'Test', 'imaging': 'Imaging', 'procedures': 'Procedure',
                 'medications': 'Medication', 'immunizations': 'Vaccine', 'life_events': 'Life event'}
MONTHS = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()
MAX_LABS = 60

CSS = """
*{box-sizing:border-box}
body{font:12.5px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#1d2421;margin:0 auto;max-width:900px;padding:24px}
h1{font-size:22px;margin:0}h2{font-size:15px;margin:22px 0 6px;padding-bottom:3px;border-bottom:2px solid #256c6c;color:#256c6c}
h3{font-size:13px;margin:14px 0 4px}
.meta{color:#56605b;margin:4px 0 0}.note{color:#56605b;font-size:11.5px;margin:4px 0 8px}
table{width:100%;border-collapse:collapse;margin:4px 0 8px}
th,td{text-align:left;vertical-align:top;padding:3px 6px;border-bottom:1px solid #e2e5e1}
th{font-size:11px;text-transform:uppercase;letter-spacing:.03em;color:#56605b;font-weight:600}
td.d{white-space:nowrap;width:1%;font-variant-numeric:tabular-nums}
.code{display:inline-block;font:11px ui-monospace,Menlo,Consolas,monospace;background:#eef0ed;border-radius:4px;padding:0 4px;margin:1px 3px 1px 0;white-space:nowrap}
.flag{color:#b3261e;font-weight:600}.muted{color:#8a938e}
.kind{font-size:10.5px;text-transform:uppercase;letter-spacing:.03em;color:#56605b;white-space:nowrap}
.year{font-weight:700;background:#f5f6f4}
.bar{display:flex;gap:8px;margin:0 0 16px}
.bar button{font:inherit;padding:6px 14px;border:1px solid #256c6c;background:#256c6c;color:#fff;border-radius:6px;cursor:pointer}
.medlist{font-size:15px}.medlist td{padding:6px 8px}.medlist h1{font-size:24px}
tr.blank td{height:26px;border-bottom:1px solid #b8bfbb}
footer{margin-top:24px;color:#8a938e;font-size:10.5px;border-top:1px solid #e2e5e1;padding-top:6px}
@page{margin:14mm 12mm}
@media print{body{padding:0;max-width:none}.bar{display:none}tr{break-inside:avoid}h2{break-after:avoid}}
"""


def fdate(d):
    """'2024-03-07' -> '7 Mar 2024'; a year or year-month stays as short as it is."""
    if not d:
        return ''
    parts = d.split('-')
    try:
        if len(parts) == 3:
            return f'{int(parts[2])} {MONTHS[int(parts[1]) - 1]} {parts[0]}'
        if len(parts) == 2:
            return f'{MONTHS[int(parts[1]) - 1]} {parts[0]}'
    except (ValueError, IndexError):
        pass
    return d


def _e(v):
    """A value as HTML text, escaped; empty for None."""
    return html.escape(str(v)) if v not in (None, '') else ''


def _codes(text):
    """Codes as small tags: 'ICD-10-CM M54.50; CPT 22612' -> two tags."""
    from .codes import split
    return ''.join(f'<span class="code">{_e((s + " " + c).strip())}</span>' for s, c in split(text))


def _in_range(d, start, end):
    """Is a (possibly partial) date inside [start, end]? Undated entries are never in a range."""
    if not d:
        return False
    return (not start or d[:len(start)] >= start[:len(d)]) and (not end or d[:len(end)] <= end[:len(d)])


def _table(head, rows):
    """A table with these column headings and ready-made rows, or a 'None recorded' line."""
    if not rows:
        return '<p class="muted">None recorded.</p>'
    return ('<table><thead><tr>' + ''.join(f'<th>{h}</th>' for h in head) + '</tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table>')


def _page(title, nonce, body):
    """A whole page: the policy that allows only the Print script, the styles, a Print button, body."""
    return '\n'.join([
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{_e(title)}</title>'
        f'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; '
        f'script-src \'nonce-{nonce}\'">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style></head><body>',
        '<div class="bar"><button id="print">Print or save as PDF</button></div>',
        f'<script nonce="{nonce}">document.getElementById("print").onclick = () => print();</script>',
        body, '</body></html>'])


def current_meds(store):
    """Medicines taken now: active, as needed, or with no stop date and no status; not ones given
    during a procedure. Prescriptions first, then over the counter, each by name."""
    meds = [m for m in store.list('medications') if not m['given']]
    cur = [m for m in meds if m['status'] in ('active', 'as needed') or (not m['end_date'] and not m['status'])]
    return sorted(cur, key=lambda m: (bool(m['otc']), (m['name'] or '').lower()))


def med_list(store, name, otc=True, nonce=None):
    """The quick printout: allergies, then current medicines, on one page with large type and a few
    blank rows to write changes in. `otc` includes over-the-counter medicines and supplies."""
    import secrets
    today = date.today().isoformat()
    al = store.list('allergies')
    meds = [m for m in current_meds(store) if otc or not m['otc']]
    rx = [m for m in meds if not m['otc']]
    over = [m for m in meds if m['otc']]
    blank = '<tr class="blank"><td>&nbsp;</td><td></td><td></td><td></td></tr>' * 3

    def row(m):
        return (f"<tr><td><b>{_e(m['name'])}</b></td><td>{_e(m['dose'])}</td><td>{_e(m['schedule'])}</td>"
                f"<td>{_e(m['purpose'])}</td></tr>")
    body = [f'<div class="medlist"><h1>Medicines and allergies: {_e(name)}</h1>',
            f'<p class="meta">As of {fdate(today)}, from the health dashboard. Check with the pharmacy list if unsure.</p>',
            '<h2>Allergies</h2>' + (_table(['Allergic to', 'Reaction', 'Severity'],
                                          [f"<tr><td><b>{_e(a['substance'])}</b></td><td>{_e(a['reaction'])}</td>"
                                           f"<td>{_e(a['severity'])}</td></tr>" for a in al])
                                   if al else '<p><b>No known allergies recorded.</b></p>'),
            f'<h2>Prescription medicines ({len(rx)})</h2>'
            + _table(['Medicine', 'Dose', 'When', 'For'], [row(m) for m in rx] + [blank])]
    if otc:
        body.append(f'<h2>Over the counter and supplements ({len(over)})</h2>'
                    + _table(['Medicine', 'Dose', 'When', 'For'], [row(m) for m in over] + [blank]))
    body.append('</div>')
    return _page(f'Medicines and allergies: {name}', nonce or secrets.token_urlsafe(16), '\n'.join(body))


def build(store, name, start=None, end=None, sections=None, oldest_first=False, kinds=None, nonce=None):
    """The summary page for one person, as HTML text.

    store         the person's Store
    name          shown at the top
    start, end    YYYY[-MM[-DD]] limits for dated lists (history, procedures, labs, life events);
                  the overview always shows what is current today
    sections      which of SECTIONS to include, in their fixed order (default: all)
    oldest_first  the history in date order instead of newest first
    kinds         which kinds of entry the history lists (keys of HISTORY_KINDS; default: all)
    nonce         a random word: the page's own policy then lets only its Print script run, also in a
                  downloaded copy (a meta tag), so nothing written into a record can ever run as code
    """
    import secrets
    nonce = nonce or secrets.token_urlsafe(16)
    sections = [s for s in SECTIONS if s in (sections or SECTIONS)]
    kinds = set(kinds or HISTORY_KINDS)
    sm = store.summary()
    today = date.today().isoformat()
    span = (f'{fdate(start) or "the start"} to {fdate(end) or "today"}' if start or end else
            f"{(sm['first_date'] or '')[:4]} to {(sm['last_date'] or '')[:4]}" if sm['first_date'] else '')
    out = [f'<h1>Health summary: {_e(name)}</h1>',
           f'<p class="meta">Made {fdate(today)} from the records on the health dashboard'
           + (f' · covering {_e(span)}' if span else '') + '.</p>']
    conds = store.list('conditions')
    meds = [m for m in store.list('medications') if not m['given']]

    if 'overview' in sections:
        active = [c for c in conds if (c['status'] or 'active') != 'resolved']
        out.append('<h2>Current conditions</h2>' + _table(
            ['Condition', 'Since', 'Codes', 'Notes'],
            [f"<tr><td><b>{_e(c['name'])}</b></td><td class=d>{fdate(c['first_noted'])}</td>"
             f"<td>{_codes(c.get('codes'))}</td><td>{_e(c['notes'])}</td></tr>" for c in active]))
        al = store.list('allergies')
        out.append('<h2>Allergies</h2>' + _table(
            ['Substance', 'Reaction', 'Severity'],
            [f"<tr><td><b>{_e(a['substance'])}</b></td><td>{_e(a['reaction'])}</td><td>{_e(a['severity'])}</td></tr>"
             for a in al]))
        cur = current_meds(store)
        out.append('<h2>Current medications</h2>' + _table(
            ['Medication', 'Dose', 'When', 'For', 'Since'],
            [f"<tr><td><b>{_e(m['name'])}</b>{' <span class=muted>(over the counter)</span>' if m['otc'] else ''}</td>"
             f"<td>{_e(m['dose'])}</td><td>{_e(m['schedule'])}</td><td>{_e(m['purpose'])}</td>"
             f"<td class=d>{fdate(m['start_date']) or '<span class=muted>unknown</span>'}</td></tr>"
             for m in cur]))

    if 'procedures' in sections:
        procs = [p for p in store.list('procedures') if not (start or end) or _in_range(p['date'], start, end)]
        procs.sort(key=lambda p: p['date'] or '', reverse=not oldest_first)
        out.append('<h2>Surgeries and procedures</h2>' + _table(
            ['Date', 'Procedure', 'Where', 'Doctor', 'Codes'],
            [f"<tr><td class=d>{fdate(p['date'])}</td><td><b>{_e(p['name'])}</b>"
             + (f"<div class=note>{_e(p['notes'])}</div>" if p['notes'] else '') + '</td>'
             f"<td>{_e(p['facility'])}</td><td>{_e(p['provider'])}</td><td>{_codes(p.get('codes'))}</td></tr>"
             for p in procs]))

    if 'life' in sections:
        life = [x for x in store.list('life_events') if not (start or end) or _in_range(x['date'], start, end)]
        life.sort(key=lambda x: x['date'] or '', reverse=not oldest_first)
        out.append('<h2>Life events</h2><p class="note">Things outside medical care that may bear on health, '
                   'as entered on the dashboard.</p>' + _table(
                       ['When', 'What', 'Kind', 'How it may affect health'],
                       [f"<tr><td class=d>{fdate(x['date'])}"
                        + (f" to {fdate(x['end_date'])}" if x['end_date'] and x['end_date'] != x['date'] else '')
                        + f"</td><td><b>{_e(x['title'])}</b>" + (f"<div class=note>{_e(x['notes'])}</div>" if x['notes'] else '')
                        + f"</td><td>{_e(x['category'])}</td><td>{_e(x['health_effect'])}</td></tr>" for x in life]))

    if 'labs' in sections:
        flagged = [r for r in store.list('labs') if r['flag'] and (not (start or end) or _in_range(r['date'], start, end))]
        flagged.sort(key=lambda r: r['date'] or '', reverse=True)
        more = len(flagged) - MAX_LABS
        out.append('<h2>Lab results outside the normal range</h2>'
                   + _table(['Date', 'Test', 'Result', 'Normal range'],
                            [f"<tr><td class=d>{fdate(r['date'])}</td><td>{_e(r['analyte'])}</td>"
                             f"<td class=flag>{_e(r['value'])} {_e(r['unit'])} ({_e(r['flag'])})</td><td>{_e(r['ref_range'])}</td></tr>"
                             for r in flagged[:MAX_LABS]])
                   + (f'<p class="note">The newest {MAX_LABS} are shown; {more} older flagged results are on the dashboard.</p>'
                      if more > 0 else ''))

    if 'history' in sections:
        ev = [e for e in store.timeline() if e['kind'] in kinds and (not (start or end) or _in_range(e['date'], start, end))]
        if oldest_first:
            ev.reverse()
        rows, year = [], None
        for e in ev:
            if e['date'][:4] != year:
                year = e['date'][:4]
                rows.append(f'<tr class="year"><td colspan="4">{_e(year)}</td></tr>')
            kind = HISTORY_KINDS[e['kind']]
            if e['kind'] == 'tests' and (e.get('category') or '').lower() == 'imaging':
                kind = 'Imaging'
            until = f" to {fdate(e['end_date'])}" if e.get('end_date') and e['end_date'] != e['date'] else ''
            rows.append(f"<tr><td class=d>{fdate(e['date'])}{_e(until)}</td><td class=kind>{kind}</td>"
                        f"<td><b>{_e(e['title'])}</b>{' <span class=flag>abnormal</span>' if e.get('abnormal') else ''}"
                        + (f"<div class=note>{_e((e['detail'] or '')[:300])}</div>" if e.get('detail') else '')
                        + f"</td><td>{_e(e['facility'])}</td></tr>")
        out.append(f'<h2>History ({len(ev)} entries, {"oldest" if oldest_first else "newest"} first)</h2>'
                   + _table(['Date', 'Kind', 'What', 'Where'], rows))

    if 'past' in sections:
        past = [c for c in conds if c['status'] == 'resolved']
        out.append('<h2>Past conditions</h2>' + _table(
            ['Condition', 'First noted', 'Codes'],
            [f"<tr><td>{_e(c['name'])}</td><td class=d>{fdate(c['first_noted'])}</td><td>{_codes(c.get('codes'))}</td></tr>"
             for c in past]))
        stopped = [m for m in meds if not m['otc'] and m['end_date'] and (not (start or end) or _in_range(m['end_date'], start, end))]
        stopped.sort(key=lambda m: m['end_date'] or '', reverse=True)
        out.append('<h2>Past medications</h2>' + _table(
            ['Medication', 'Dose', 'Started', 'Stopped', 'Notes'],
            [f"<tr><td>{_e(m['name'])}</td><td>{_e(m['dose'])}</td><td class=d>{fdate(m['start_date'])}</td>"
             f"<td class=d>{fdate(m['end_date'])}</td><td>{_e(m['notes'])}</td></tr>" for m in stopped]))

    out.append('<footer>Personal records, not medical advice. Some details were read automatically from documents '
               'and can be wrong; the original documents are the record.</footer>')
    return _page(f'Health summary: {name}', nonce, '\n'.join(out))

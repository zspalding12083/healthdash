"""The printouts: a health summary of what is going on and when, and a quick list of medicines and
allergies. Each is built once as a plain description (a list of blocks) and drawn either as a
self-contained HTML page or as a PDF (pdf.py), so both always say the same thing.

Built from the records on the dashboard (nothing is looked up elsewhere) for a new doctor, a
specialist visit or a person's own files. /report shows the summary, where the browser prints it;
/report?download=1 gives the same page as a file and /report?format=pdf a PDF. /report?view=meds is
the quick list of current medicines and allergies, for a wallet, the fridge or a new doctor's
forms. /report?view=card is the emergency card (emergency.py) and
/report?view=questions the questions to take to a visit (questions.py) and
/report?view=readings the home readings log (readings.py). The HTML page has no outside links, fonts or scripts beyond its Print button, so a downloaded
copy reads the same anywhere.

Summary sections, in order (each can be left out with the `sections` option):
  overview     current conditions with their codes, allergies, current medications
  procedures   surgeries and procedures, with codes
  life         life events that may bear on health (accidents, family, losses, moves)
  labs         lab results flagged high or low, newest first
  history      a dated list of visits, tests, imaging, procedures, medication changes, vaccines and
               life events, grouped by year
  past         resolved conditions and past medications with when they started and stopped

Blocks (what summary() and med_list() return, in a dict with `title`, `large` and `blocks`):
  ('h1', text)  ('meta', text)  ('h2', text)  ('note', text)  ('strong', text)  ('footer', text)
  ('table', columns, rows)   columns: [(heading, relative width)]; a row is a list of cells,
                             ('year', text) for a year heading, or ('blank',) for an empty row to
                             write in. A cell is a string or a dict: text, bold, note (a smaller line
                             under it), flag (shown in red), muted, codes (a codes text, shown as tags).
"""

import html
from datetime import date

SECTIONS = ['overview', 'procedures', 'life', 'labs', 'history', 'past']
HISTORY_KINDS = {'encounters': 'Visit', 'tests': 'Test', 'imaging': 'Imaging', 'procedures': 'Procedure',
                 'medications': 'Medication', 'immunizations': 'Vaccine', 'life_events': 'Life event'}
MONTHS = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()
MAX_LABS = 60
FOOTER = ('Personal records, not medical advice. Some details were read automatically from documents '
          'and can be wrong; the original documents are the record.')

CSS = """
*{box-sizing:border-box}
body{font:12.5px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#1d2421;margin:0 auto;max-width:900px;padding:24px}
h1{font-size:22px;margin:0}h2{font-size:15px;margin:22px 0 6px;padding-bottom:3px;border-bottom:2px solid #256c6c;color:#256c6c}
.meta{color:#56605b;margin:4px 0 0}.note{color:#56605b;font-size:11.5px;margin:4px 0 8px}
td .note{margin:2px 0 0}
table{width:100%;border-collapse:collapse;margin:4px 0 8px}
th,td{text-align:left;vertical-align:top;padding:3px 6px;border-bottom:1px solid #e2e5e1}
th{font-size:11px;text-transform:uppercase;letter-spacing:.03em;color:#56605b;font-weight:600}
.code{display:inline-block;font:11px ui-monospace,Menlo,Consolas,monospace;background:#eef0ed;border-radius:4px;padding:0 4px;margin:1px 3px 1px 0;white-space:nowrap}
.flag{color:#b3261e;font-weight:600}.muted{color:#8a938e}
.year td{font-weight:700;background:#f5f6f4}
.bar{display:flex;gap:8px;margin:0 0 16px}
.bar button,.bar a{font:inherit;padding:6px 14px;border:1px solid #256c6c;background:#256c6c;color:#fff;border-radius:6px;cursor:pointer;text-decoration:none}
.bar a{background:#fff;color:#256c6c}
.large{font-size:15px}.large td{padding:6px 8px}.large h1{font-size:24px}
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


def _in_range(d, start, end):
    """Is a (possibly partial) date inside [start, end]? Undated entries are never in a range."""
    if not d:
        return False
    return (not start or d[:len(start)] >= start[:len(d)]) and (not end or d[:len(end)] <= end[:len(d)])


def _value(r):
    """A lab result with its unit once ("5.7 %", not "5.7 % %": some values already carry it)."""
    v, u = str(r['value'] or '').strip(), str(r['unit'] or '').strip()
    return v if not u or v.lower().endswith(u.lower()) else f'{v} {u}'.strip()


def _cell(text, **kw):
    """A table cell: text plus any of bold, note, flag, muted, codes."""
    return dict(text='' if text is None else str(text), **kw)


def current_meds(store):
    """Medicines taken now: active, as needed, or with no stop date and no status; not ones given
    during a procedure. Prescriptions first, then over the counter, each by name."""
    meds = [m for m in store.list('medications') if not m['given']]
    cur = [m for m in meds if m['status'] in ('active', 'as needed') or (not m['end_date'] and not m['status'])]
    return sorted(cur, key=lambda m: (bool(m['otc']), (m['name'] or '').lower()))


# ---- what each printout says ------------------------------------------------------------

def med_list(store, name, otc=True):
    """The quick printout: allergies, then current medicines, in large type with a few blank rows
    to write changes in. `otc` includes over-the-counter medicines and supplies."""
    al = store.list('allergies')
    meds = [m for m in current_meds(store) if otc or not m['otc']]
    rx = [m for m in meds if not m['otc']]
    over = [m for m in meds if m['otc']]
    cols = [('Medicine', 3), ('Dose', 1.4), ('When', 2), ('For', 2)]

    def rows(ms):
        return [[_cell(m['name'], bold=True), _cell(m['dose']), _cell(m['schedule']), _cell(m['purpose'])]
                for m in ms] + [('blank',)] * 3
    blocks = [('h1', f'Medicines and allergies: {name}'),
              ('meta', f'As of {fdate(date.today().isoformat())}, from the health dashboard. '
                       'Check with the pharmacy list if unsure.'),
              ('h2', 'Allergies')]
    if al:
        blocks.append(('table', [('Allergic to', 3), ('Reaction', 3), ('Severity', 1.5)],
                       [[_cell(a['substance'], bold=True), _cell(a['reaction']), _cell(a['severity'])] for a in al]))
    else:
        blocks.append(('strong', 'No known allergies recorded.'))
    blocks += [('h2', f'Prescription medicines ({len(rx)})'), ('table', cols, rows(rx))]
    if otc:
        blocks += [('h2', f'Over the counter and supplements ({len(over)})'), ('table', cols, rows(over))]
    return {'title': f'Medicines and allergies: {name}', 'large': True, 'blocks': blocks}


def summary(store, name, start=None, end=None, sections=None, oldest_first=False, kinds=None):
    """The health summary for one person.

    store         the person's Store
    name          shown at the top
    start, end    YYYY[-MM[-DD]] limits for dated lists (history, procedures, labs, life events,
                  past medications); the overview always shows what is current today
    sections      which of SECTIONS to include, in their fixed order (default: all)
    oldest_first  dated lists in date order instead of newest first
    kinds         which kinds of entry the history lists (keys of HISTORY_KINDS; default: all)
    """
    sections = [s for s in SECTIONS if s in (sections or SECTIONS)]
    kinds = set(kinds or HISTORY_KINDS)
    ranged = lambda d: not (start or end) or _in_range(d, start, end)   # noqa: E731
    sm = store.summary()
    span = (f'{fdate(start) or "the start"} to {fdate(end) or "today"}' if start or end else
            f"{(sm['first_date'] or '')[:4]} to {(sm['last_date'] or '')[:4]}" if sm['first_date'] else '')
    b = [('h1', f'Health summary: {name}'),
         ('meta', f'Made {fdate(date.today().isoformat())} from the records on the health dashboard'
                  + (f' · covering {span}' if span else '') + '.')]
    conds = store.list('conditions')
    meds = [m for m in store.list('medications') if not m['given']]

    if 'overview' in sections:
        active = [c for c in conds if (c['status'] or 'active') != 'resolved']
        b += [('h2', 'Current conditions'),
              ('table', [('Condition', 3), ('Since', 1.1), ('Codes', 2.6), ('Notes', 2.3)],
               [[_cell(c['name'], bold=True), _cell(fdate(c['first_noted'])), _cell(c.get('codes'), codes=True),
                 _cell(c['notes'])] for c in active])]
        b += [('h2', 'Allergies'),
              ('table', [('Substance', 3), ('Reaction', 3), ('Severity', 1.5)],
               [[_cell(a['substance'], bold=True), _cell(a['reaction']), _cell(a['severity'])]
                for a in store.list('allergies')])]
        b += [('h2', 'Current medications'),
              ('table', [('Medication', 3), ('Dose', 1.2), ('When', 1.8), ('For', 1.8), ('Since', 1.1), ('Codes', 1.4)],
               [[_cell(m['name'], bold=True, note='over the counter' if m['otc'] else None), _cell(m['dose']),
                 _cell(m['schedule']), _cell(m['purpose']),
                 _cell(fdate(m['start_date']) or 'unknown', muted=not m['start_date']),
                 _cell(m.get('codes'), codes=True)] for m in current_meds(store)])]

    if 'procedures' in sections:
        procs = sorted([p for p in store.list('procedures') if ranged(p['date'])],
                       key=lambda p: p['date'] or '', reverse=not oldest_first)
        b += [('h2', 'Surgeries and procedures'),
              ('table', [('Date', 1.1), ('Procedure', 3), ('Where', 2), ('Doctor', 1.8), ('Codes', 2)],
               [[_cell(fdate(p['date'])), _cell(p['name'], bold=True, note=p['notes']), _cell(p['facility']),
                 _cell(p['provider']), _cell(p.get('codes'), codes=True)] for p in procs])]

    if 'life' in sections:
        life = sorted([x for x in store.list('life_events') if ranged(x['date'])],
                      key=lambda x: x['date'] or '', reverse=not oldest_first)
        b += [('h2', 'Life events'),
              ('note', 'Things outside medical care that may bear on health, as entered on the dashboard.'),
              ('table', [('When', 1.4), ('What', 3), ('Kind', 1.5), ('How it may affect health', 3)],
               [[_cell(fdate(x['date']) + (f" to {fdate(x['end_date'])}" if x['end_date'] and x['end_date'] != x['date'] else '')),
                 _cell(x['title'], bold=True, note=x['notes']), _cell(x['category']), _cell(x['health_effect'])]
                for x in life])]

    if 'labs' in sections:
        flagged = sorted([r for r in store.list('labs') if r['flag'] and ranged(r['date'])],
                         key=lambda r: r['date'] or '', reverse=True)
        more = len(flagged) - MAX_LABS
        b += [('h2', 'Lab results outside the normal range'),
              ('table', [('Date', 1.1), ('Test', 3), ('Result', 2), ('Normal range', 1.6)],
               [[_cell(fdate(r['date'])), _cell(r['analyte']),
                 _cell(f"{_value(r)} ({r['flag']})", flag=True),
                 _cell(r['ref_range'])] for r in flagged[:MAX_LABS]])]
        if more > 0:
            b.append(('note', f'The newest {MAX_LABS} are shown; {more} older flagged results are on the dashboard.'))

    if 'history' in sections:
        ev = [e for e in store.timeline() if e['kind'] in kinds and ranged(e['date'])]
        if oldest_first:
            ev.reverse()
        rows, year = [], None
        for e in ev:
            if e['date'][:4] != year:
                year = e['date'][:4]
                rows.append(('year', year))
            kind = HISTORY_KINDS[e['kind']]
            if e['kind'] == 'tests' and (e.get('category') or '').lower() == 'imaging':
                kind = 'Imaging'
            until = f" to {fdate(e['end_date'])}" if e.get('end_date') and e['end_date'] != e['date'] else ''
            rows.append([_cell(fdate(e['date']) + until), _cell(kind, muted=True),
                         _cell(e['title'] + (' (abnormal)' if e.get('abnormal') else ''), bold=True,
                               flag=bool(e.get('abnormal')), note=(e['detail'] or '')[:300] or None),
                         _cell(e['facility'])])
        b += [('h2', f'History ({len(ev)} entries, {"oldest" if oldest_first else "newest"} first)'),
              ('table', [('Date', 1.3), ('Kind', 1), ('What', 4.5), ('Where', 2)], rows)]

    if 'past' in sections:
        b += [('h2', 'Past conditions'),
              ('table', [('Condition', 3), ('First noted', 1.2), ('Codes', 3)],
               [[_cell(c['name']), _cell(fdate(c['first_noted'])), _cell(c.get('codes'), codes=True)]
                for c in conds if c['status'] == 'resolved'])]
        stopped = sorted([m for m in meds if not m['otc'] and m['end_date'] and ranged(m['end_date'])],
                         key=lambda m: m['end_date'] or '', reverse=True)
        b += [('h2', 'Past medications'),
              ('table', [('Medication', 3), ('Dose', 1.2), ('Started', 1.1), ('Stopped', 1.1), ('Notes', 3)],
               [[_cell(m['name'], note=None), _cell(m['dose']), _cell(fdate(m['start_date'])),
                 _cell(fdate(m['end_date'])), _cell(m['notes'])] for m in stopped])]

    b.append(('footer', FOOTER))
    return {'title': f'Health summary: {name}', 'large': False, 'blocks': b}


# ---- drawing it as HTML -------------------------------------------------------------------

def _e(v):
    """A value as HTML text, escaped; empty for None."""
    return html.escape(str(v)) if v not in (None, '') else ''


def _codes(text):
    """Codes as small tags: 'ICD-10-CM M54.50; CPT 22612' -> two tags."""
    from .codes import split
    return ''.join(f'<span class="code">{_e((s + " " + c).strip())}</span>' for s, c in split(text))


def _html_cell(c):
    """One table cell as HTML (everything escaped)."""
    if isinstance(c, str):
        c = _cell(c)
    inner = _codes(c['text']) if c.get('codes') else _e(c['text'])
    if c.get('bold'):
        inner = f'<b>{inner}</b>'
    cls = 'flag' if c.get('flag') else 'muted' if c.get('muted') else ''
    note = f'<div class="note">{_e(c["note"])}</div>' if c.get('note') else ''
    return f'<td{f" class={cls}" if cls else ""}>{inner}{note}</td>'


def to_html(doc, nonce, pdf_link=None):
    """The whole page. Its policy (header and meta tag, so a saved copy keeps it) lets only its own
    Print script run, by `nonce`, so nothing written into a record can ever run as code.
    `pdf_link` adds a "Download PDF" link (left out of downloaded copies, where it would not work)."""
    out = []
    for blk in doc['blocks']:
        k = blk[0]
        if k == 'h1':
            out.append(f'<h1>{_e(blk[1])}</h1>')
        elif k == 'h2':
            out.append(f'<h2>{_e(blk[1])}</h2>')
        elif k == 'meta':
            out.append(f'<p class="meta">{_e(blk[1])}</p>')
        elif k == 'note':
            out.append(f'<p class="note">{_e(blk[1])}</p>')
        elif k == 'strong':
            out.append(f'<p><b>{_e(blk[1])}</b></p>')
        elif k == 'footer':
            out.append(f'<footer>{_e(blk[1])}</footer>')
        elif k == 'table':
            cols, rows = blk[1], blk[2]
            if not rows:
                out.append('<p class="muted">None recorded.</p>')
                continue
            body = []
            for r in rows:
                if isinstance(r, tuple) and r[0] == 'year':
                    body.append(f'<tr class="year"><td colspan="{len(cols)}">{_e(r[1])}</td></tr>')
                elif isinstance(r, tuple) and r[0] == 'blank':
                    body.append('<tr class="blank">' + '<td>&nbsp;</td>' * len(cols) + '</tr>')
                else:
                    body.append('<tr>' + ''.join(_html_cell(c) for c in r) + '</tr>')
            out.append('<table><thead><tr>' + ''.join(f'<th>{_e(h)}</th>' for h, _w in cols) + '</tr></thead><tbody>'
                       + ''.join(body) + '</tbody></table>')
    pdf = f'<a href="{_e(pdf_link)}">Download PDF</a>' if pdf_link else ''
    return '\n'.join([
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{_e(doc["title"])}</title>'
        f'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; '
        f'script-src \'nonce-{nonce}\'">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style></head>'
        f'<body{" class=large" if doc.get("large") else ""}>',
        f'<div class="bar"><button id="print">Print</button>{pdf}</div>',
        f'<script nonce="{nonce}">document.getElementById("print").onclick = () => print();</script>',
        *out, '</body></html>'])


def build(store, name, start=None, end=None, sections=None, oldest_first=False, kinds=None, nonce=None):
    """The summary as an HTML page (see summary() for the options)."""
    import secrets
    return to_html(summary(store, name, start, end, sections, oldest_first, kinds), nonce or secrets.token_urlsafe(16))

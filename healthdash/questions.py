"""Questions for the next visit: written down as they come up, taken along, answered there.

Each question is an entry of the kind `questions` (store.KINDS), so it is added, edited, searched
and deleted like any other entry:

    date          when it was written down (today when left empty)
    question      what to ask
    ask           who to ask: a doctor, a clinic or a kind of doctor ("Dr Sample", "cardiology");
                  empty = whoever is seen next
    important     1 = ask this first (listed first, marked on the printout)
    status        open, or answered
    answer        what was said
    answered_on   when
    encounter_id  the visit it was answered at, when that visit is in the records

sheet() is the printout to take along (/report?view=questions, optionally &ask=...): the open
questions grouped by who to ask, important ones first, each with room to write the answer.
"""

from datetime import date

from .report import FOOTER, _cell, fdate

ANYONE = 'Any doctor'
WRITE_ROWS = 2     # lines left under each question for the answer
EXTRA_ROWS = 4     # lines at the end for questions that come up there


def _key(who):
    return ' '.join((who or '').lower().split())


def open_questions(store, ask=None):
    """Open questions, important first, then oldest first (asked longest ago); only those for
    `ask` (and those for anyone) when given."""
    qs = [q for q in store.list('questions') if (q['status'] or 'open') == 'open']
    if ask:
        qs = [q for q in qs if not _key(q['ask']) or _key(q['ask']) == _key(ask)]
    return sorted(qs, key=lambda q: (-(q['important'] or 0), q['date'] or '', q['id']))


def groups(questions):
    """[(who to ask, [questions])], named groups alphabetically, "any doctor" last. Names that differ
    only in case or spacing go together, under the spelling used most (or with more capitals)."""
    out = {}
    for q in questions:
        out.setdefault(_key(q['ask']), []).append(q)

    def name(items):
        spellings = [' '.join(q['ask'].split()) for q in items]
        return max(spellings, key=lambda w: (spellings.count(w), sum(c.isupper() for c in w)))
    keys = sorted(k for k in out if k) + ([''] if '' in out else [])
    return [(name(out[k]) if k else ANYONE, out[k]) for k in keys]


def sheet(store, name, ask=None, today=None):
    """The list to take to the visit, as printout blocks (see report.py)."""
    today = today or date.today()
    qs = open_questions(store, ask)
    title = f'Questions for {ask.strip()}' if ask else 'Questions for my next visit'
    b = [('h1', f'{title}: {name}'),
         ('meta', f'{len(qs)} question{"s" if len(qs) != 1 else ""} · printed {fdate(today.isoformat())}')]
    if not qs:
        b.append(('note', 'No open questions. Add them on the dashboard (the Questions tab) as they come up.'))
    n = 0
    for who, items in groups(qs):
        b.append(('h2', who if who == ANYONE else f'For {who}'))
        rows = []
        for q in items:
            n += 1
            note = ' · '.join(x for x in ('Important' if q['important'] else '',
                                          f"written down {fdate(q['date'])}" if q['date'] else '') if x)
            rows.append([_cell(f'{n}.', muted=True), _cell(q['question'], bold=True, note=note or None, flag=bool(q['important']))])
            rows += [('blank',)] * WRITE_ROWS
        b.append(('table', [('', 0.4), ('Question, and the answer', 9)], rows))
    b += [('h2', 'Other questions that came up'), ('table', [('', 0.4), ('', 9)], [('blank',)] * EXTRA_ROWS)]
    b.append(('footer', FOOTER))
    return {'title': f'{title}: {name}', 'large': True, 'blocks': b}

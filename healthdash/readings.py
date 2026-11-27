"""Home readings: blood pressure, pulse, weight, blood sugar and the like, measured at home.

Each reading is an entry of the kind `readings` (store.KINDS):

    date, time    when it was measured (YYYY-MM-DD; HH:MM, 24-hour, optional)
    type          a key of TYPES (blood_pressure, pulse, ...) or any other name for something else
    reading       the number (systolic for blood pressure)
    reading2      the second number (diastolic), blood pressure only
    unit          as measured (lb or kg, °F or °C, ...); the type's usual unit when left empty
    notes         anything to remember (after a walk, missed a dose, left arm, ...)

The ranges below only flag a reading for a second look on the dashboard and the printout. They are
common general guides (adult blood pressure, pulse at rest, oxygen, temperature, blood sugar), not
anyone's own target, which their doctor sets.

parse() reads pasted lines (from a notebook or a meter's or app's CSV export); sheet() is the log
to take to a doctor (/report?view=readings, with days= and types=).
"""

import re
from datetime import date, timedelta

from .report import FOOTER, _cell, fdate

# key: label, unit, whether it has a second number, the range shown on its chart, and flags
TYPES = {
    'blood_pressure': {'label': 'Blood pressure', 'unit': 'mmHg', 'two': True, 'names': ('systolic', 'diastolic'),
                       'band': (90, 120), 'band2': (60, 80)},
    'pulse': {'label': 'Pulse', 'unit': 'bpm', 'band': (60, 100)},
    'weight': {'label': 'Weight', 'unit': 'lb'},
    'glucose': {'label': 'Blood sugar', 'unit': 'mg/dL', 'band': (70, 180)},
    'temperature': {'label': 'Temperature', 'unit': '°F', 'band': (97, 99.5)},
    'oxygen': {'label': 'Oxygen (SpO2)', 'unit': '%', 'band': (95, 100)},
    'peak_flow': {'label': 'Peak flow', 'unit': 'L/min'},
    'pain': {'label': 'Pain (0-10)', 'unit': '/10'},
}
MAX_ROWS = 150     # per type on the printout; the averages count them all
MAX_PASTE = 5000   # readings saved from one paste
TIME_RE = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')


def label(t):
    return TYPES.get(t, {}).get('label') or t


def flag(r):
    """'H' (high), 'L' (low) or None, by common general guides; None for types without one, and
    for units other than the usual one (a temperature in °C, a sugar in mmol/L)."""
    t, v, v2 = r.get('type'), r.get('reading'), r.get('reading2')
    if v is None or (r.get('unit') and t in TYPES and r['unit'] != TYPES[t]['unit']):
        return None
    if t == 'blood_pressure':
        if v >= 140 or (v2 or 0) >= 90:
            return 'H'
        return 'L' if v < 90 or (v2 is not None and v2 < 60) else None
    rules = {'pulse': (50, 100), 'glucose': (70, 180), 'temperature': (95, 100.4), 'oxygen': (92, None)}
    if t not in rules:
        return None
    lo, hi = rules[t]
    return 'L' if lo is not None and v < lo else 'H' if hi is not None and v > hi else None


WHOLE = {'blood_pressure', 'pulse', 'glucose', 'oxygen'}   # averages shown as whole numbers


def _num(v, digits=1):
    if v is None:
        return ''
    t = f'{v:.{digits}f}'
    return t.rstrip('0').rstrip('.') if '.' in t else t


def show(r):
    """'128/82 mmHg', '72 bpm', '161.4 lb'."""
    v = _num(r.get('reading')) + (f"/{_num(r['reading2'])}" if r.get('reading2') is not None else '')
    unit = r.get('unit') or TYPES.get(r.get('type'), {}).get('unit') or ''
    return f'{v} {unit}'.strip()


def stats(rows):
    """Count, averages, lowest and highest, and how many were flagged, for a list of one type."""
    vals = [r['reading'] for r in rows if r['reading'] is not None]
    v2 = [r['reading2'] for r in rows if r.get('reading2') is not None]
    if not vals:
        return None
    avg = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
    return {'count': len(vals), 'avg': avg(vals), 'avg2': avg(v2), 'low': min(vals), 'high': max(vals),
            'flagged': sum(1 for r in rows if flag(r))}


# ---- pasted lines -------------------------------------------------------------------------

DATE_FORMS = [(re.compile(r'^(\d{4})-(\d{1,2})-(\d{1,2})$'), 'ymd'), (re.compile(r'^(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})$'), 'mdy')]
TIME_TOKEN = re.compile(r'^(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?$')
NUMBER = re.compile(r'^-?\d+(?:\.\d+)?$')
BP = re.compile(r'^(\d{2,3})/(\d{2,3})$')


def _date(tok):
    for rx, form in DATE_FORMS:
        m = rx.match(tok)
        if m:
            a, b, c = m.groups()
            y, mo, d = (a, b, c) if form == 'ymd' else (c if len(c) == 4 else '20' + c, a, b)
            try:
                return date(int(y), int(mo), int(d)).isoformat()
            except ValueError:
                return None
    return None


def parse(text, kind, unit=None, today=None):
    """Pasted lines -> ({'rows': [readings], 'errors': [(line number, text, why)]}). One reading per
    line: a date (2026-11-20, 11/20/2026 or 11/20/26), maybe a time (8:15, 8:15 PM, 20:15), then the
    number(s); for blood pressure 128/82 or 128 82, and a third number is the pulse. Commas, tabs
    and semicolons work as spaces (a CSV export). Lines with no date, like a heading, are skipped."""
    today = today or date.today()
    rows, errors = [], []
    for n, line in enumerate((text or '').splitlines(), 1):
        raw = line.strip()
        toks = [t for t in re.split(r'[,\t;]+|\s+(?![AaPp][Mm]\b)', raw) if t]
        if not toks:
            continue
        d = _date(toks[0])
        if not d:
            continue                       # a heading or a note
        if d > today.isoformat():
            errors.append((n, raw, 'the date is in the future'))
            continue
        rest, t = toks[1:], None
        if rest and TIME_TOKEN.match(rest[0]):
            h, mi, ampm = TIME_TOKEN.match(rest[0]).groups()
            h = int(h) % 12 + (12 if ampm and ampm.lower() == 'pm' else 0) if ampm else int(h)
            t = f'{h:02d}:{mi}' if h < 24 else None
            rest = rest[1:]
        nums = []
        for tok in rest:
            m = BP.match(tok)
            if m:
                nums += [float(m.group(1)), float(m.group(2))]
            elif NUMBER.match(tok):
                nums.append(float(tok))
        base = {'date': d, 'time': t, 'type': kind, 'unit': unit or TYPES.get(kind, {}).get('unit')}
        if kind == 'blood_pressure':
            if len(nums) < 2:
                errors.append((n, raw, 'needs two numbers, like 128/82'))
                continue
            if not (50 <= nums[0] <= 260 and 30 <= nums[1] <= 160 and nums[0] > nums[1]):
                errors.append((n, raw, "those numbers don't look like a blood pressure"))
                continue
            rows.append(dict(base, reading=nums[0], reading2=nums[1]))
            if len(nums) > 2 and 25 <= nums[2] <= 250:
                rows.append(dict(base, type='pulse', reading=nums[2], reading2=None, unit='bpm'))
        else:
            if not nums:
                errors.append((n, raw, 'no number'))
                continue
            rows.append(dict(base, reading=nums[0], reading2=None))
    return {'rows': rows, 'errors': errors}


def add_pasted(store, text, kind, unit=None, apply=False, today=None):
    """Read pasted lines; with apply, save the ones not already there (same type, day, time and
    numbers). Returns what parse() gives, with `new` (how many are not there yet) and `saved`."""
    if not (kind or '').strip():
        from .store import ValidationError
        raise ValidationError('choose what was measured')
    p = parse(text, kind.strip(), (unit or '').strip() or None, today)
    have = {(r['type'], r['date'], r['time'] or '', r['reading'], r['reading2']) for r in store.list('readings')}
    new = [r for r in p['rows'] if (r['type'], r['date'], r['time'] or '', r['reading'], r['reading2']) not in have]
    saved = 0
    if apply and len(new) > MAX_PASTE:
        from .store import ValidationError
        raise ValidationError(f'up to {MAX_PASTE} readings at a time; paste the rest separately')
    if apply:
        for r in new:
            store.create('readings', r)
            saved += 1
    return {'rows': p['rows'], 'errors': [{'line': n, 'text': t[:200], 'why': w} for n, t, w in p['errors']],
            'new': len(new), 'saved': saved}


# ---- the printout -------------------------------------------------------------------------

def _when(r):
    return fdate(r['date']) + (f" {r['time']}" if r.get('time') else '')


def sheet(store, name, days=None, types=None, today=None):
    """The readings log to take to a doctor, as printout blocks (see report.py): for each type, its
    averages (and mornings against evenings for blood pressure, when times were noted), then every
    reading, newest first."""
    today = today or date.today()
    since = (today - timedelta(days=days)).isoformat() if days else None
    rows = [r for r in store.list('readings') if r['date'] and (not since or r['date'] >= since)]
    order = list(TYPES) + sorted({r['type'] for r in rows} - set(TYPES))
    kinds = [t for t in order if any(r['type'] == t for r in rows) and (not types or t in types)]
    span = f'the last {days} days ({fdate(since)} to {fdate(today.isoformat())})' if days else 'all readings'
    b = [('h1', f'Home readings: {name}'), ('meta', f'{span} · printed {fdate(today.isoformat())}')]
    if not kinds:
        b.append(('note', 'No home readings in this time. Add them on the dashboard (the Home readings tab).'))
    for t in kinds:
        rs = sorted((r for r in rows if r['type'] == t), key=lambda r: (r['date'], r['time'] or ''), reverse=True)
        s = stats(rs)
        unit = rs[0]['unit'] or TYPES.get(t, {}).get('unit') or ''
        b.append(('h2', f'{label(t)} ({len(rs)})'))
        dg = 0 if t in WHOLE else 1
        avg = _num(s['avg'], dg) + (f"/{_num(s['avg2'], dg)}" if s['avg2'] is not None else '')
        if s['count'] == 1:
            facts = ['One reading']
        else:
            facts = [f'Average {avg} {unit}'.strip(), f"lowest {_num(s['low'])}, highest {_num(s['high'])}"]
            if t == 'weight':
                change = rs[0]['reading'] - rs[-1]['reading']
                facts.append(f"{'up' if change > 0 else 'down'} {_num(abs(change))} {unit} over this time" if change else 'no change')
        if s['flagged']:
            facts.append(f"{s['flagged']} outside the usual range")
        b.append(('strong', '; '.join(facts) + '.'))
        if t == 'blood_pressure':
            am = [r for r in rs if r['time'] and r['time'] < '12:00']
            pm = [r for r in rs if r['time'] and r['time'] >= '12:00']
            if am and pm:
                part = lambda xs: f"{_num(stats(xs)['avg'], 0)}/{_num(stats(xs)['avg2'], 0)} ({len(xs)})"  # noqa: E731
                b.append(('note', f'Mornings average {part(am)}; afternoons and evenings {part(pm)}.'))
        cols = [('When', 2), ('Reading', 2), ('Notes', 5)]
        table = [[_cell(_when(r)), _cell(show(r), bold=True, flag=bool(flag(r))), _cell(r['notes'] or '')]
                 for r in rs[:MAX_ROWS]]
        b.append(('table', cols, table))
        if len(rs) > MAX_ROWS:
            b.append(('note', f'The newest {MAX_ROWS} are listed; the averages count all {len(rs)}.'))
    b.append(('note', 'Readings in red are outside common general ranges (blood pressure 140/90 or more, or under '
                      '90/60; pulse under 50 or over 100; oxygen under 92%; temperature over 100.4 °F; blood sugar '
                      'under 70 or over 180 mg/dL). A doctor may set different targets.'))
    b.append(('footer', FOOTER))
    return {'title': f'Home readings: {name}', 'large': False, 'blocks': b}


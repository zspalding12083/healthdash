"""A small PDF writer for the printouts (report.py), using only the standard library.

It draws report blocks (headings, notes, tables with wrapped text, year rows, blank rows to write
in) on US Letter pages with the PDF base fonts Helvetica and Helvetica-Bold, which every PDF reader
has, so nothing is embedded or fetched. Tables continue on the next page with their headings
repeated; every page says "Page N of M" at the foot.

Limits: the base fonts cover Latin-1 (Western European) letters. Common typographic characters
(dashes, curly quotes, arrows, the ellipsis) are written as their plain forms; anything else
outside Latin-1 becomes "?".
"""

import zlib

PAGE_W, PAGE_H = 612, 792            # US Letter, in points (1/72 inch)
MARGIN = 42
BOTTOM = 52                          # room for the page number
TEAL, GREY, LIGHT, RED, INK = (0.145, 0.424, 0.424), (0.34, 0.38, 0.36), (0.87, 0.89, 0.88), (0.70, 0.15, 0.12), (0.11, 0.14, 0.13)

# Glyph widths (per 1000 units of font size) for characters 32-126, from the standard Adobe metrics
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556,
         556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278,
         500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469,
         556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500,
         278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
_BOLD = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556,
         556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278,
         556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584,
         556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556,
         333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]
_PLAIN = {'–': '-', '—': '-', '−': '-', '‘': "'", '’': "'", '“': '"', '”': '"',
          '…': '...', '→': '->', '←': '<-', '•': '\xb7', ' ': ' ', '＋': '+'}


def clean(text):
    """Text the base fonts can draw: plain forms of common typographic characters, '?' for others."""
    text = ''.join(_PLAIN.get(ch, ch) for ch in str(text or ''))
    return ''.join(ch if ord(ch) < 256 and (ch >= ' ' or ch == '\n') else '?' for ch in text)


def width(text, size, bold=False):
    """How wide a line of (clean) text is, in points."""
    table = _BOLD if bold else _HELV
    return sum(table[ord(ch) - 32] if 32 <= ord(ch) <= 126 else 556 for ch in text) * size / 1000


def wrap(text, w, size, bold=False):
    """Break text into lines no wider than w (long words are split); keeps the text's own line breaks."""
    lines = []
    for para in clean(text).split('\n'):
        line = ''
        for word in para.split(' '):
            trial = (line + ' ' + word) if line else word
            if width(trial, size, bold) <= w:
                line = trial
                continue
            if line:
                lines.append(line)
            while width(word, size, bold) > w and len(word) > 1:   # a word too long for the column
                cut = len(word)
                while cut > 1 and width(word[:cut], size, bold) > w:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


def _esc(text):
    return text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


class _Writer:
    """Pages being drawn: a list of content-stream command lists, and the cursor y on the current one."""
    def __init__(self, scale=1.0):
        self.pages, self.scale = [], scale
        self.new_page()

    def new_page(self):
        self.ops = []
        self.pages.append(self.ops)
        self.y = PAGE_H - MARGIN

    def room(self, h):
        """Start a new page unless h points still fit on this one. Returns True if it did."""
        if self.y - h < BOTTOM:
            self.new_page()
            return True
        return False

    def text(self, x, y, s, size, bold=False, color=INK):
        self.ops.append(f'BT /{"F2" if bold else "F1"} {size:.2f} Tf {color[0]} {color[1]} {color[2]} rg '
                        f'{x:.2f} {y:.2f} Td ({_esc(s)}) Tj ET')

    def line(self, x0, y, x1, color=LIGHT, w=0.6):
        self.ops.append(f'{color[0]} {color[1]} {color[2]} RG {w} w {x0:.2f} {y:.2f} m {x1:.2f} {y:.2f} l S')

    def fill(self, x, y, w, h, color):
        self.ops.append(f'{color[0]} {color[1]} {color[2]} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f')

    def para(self, s, size, bold=False, color=INK, before=0, after=4):
        """A paragraph across the page."""
        self.y -= before
        for ln in wrap(s, PAGE_W - 2 * MARGIN, size, bold):
            self.room(size * 1.3)
            self.y -= size * 1.25
            self.text(MARGIN, self.y, ln, size, bold, color)
        self.y -= after


def _cell_lines(c, w, s):
    """A cell's wrapped lines: [(text, size, bold, color)]."""
    from .codes import split
    if isinstance(c, str):
        c = {'text': c}
    text = c.get('text') or ''
    if c.get('codes'):
        text = '\n'.join(f'{a} {b}'.strip() for a, b in split(text))
    color = RED if c.get('flag') else GREY if c.get('muted') else INK
    size, bold = (8.5 * s if c.get('codes') else 9.5 * s), bool(c.get('bold') or c.get('flag'))
    out = [(ln, size, bold, color) for ln in wrap(text, w, size, bold)] if text else []
    if c.get('note'):
        out += [(ln, 8 * s, False, GREY) for ln in wrap(c['note'], w, 8 * s)]
    return out


def _table(wr, cols, rows):
    s = wr.scale
    if not rows:
        wr.para('None recorded.', 9.5 * s, color=GREY)
        return
    total = PAGE_W - 2 * MARGIN
    weights = sum(w for _h, w in cols)
    widths = [total * w / weights for _h, w in cols]
    xs = [MARGIN + sum(widths[:i]) for i in range(len(cols))]
    pad = 4

    def header():
        wr.y -= 12 * s
        for (h, _w), x in zip(cols, xs):
            wr.text(x + pad, wr.y, clean(h).upper(), 7.5 * s, True, GREY)
        wr.y -= 4
        wr.line(MARGIN, wr.y, MARGIN + total, GREY, 0.8)
    wr.room(40 * s)
    header()
    for r in rows:
        if isinstance(r, tuple) and r[0] == 'year':
            h = 15 * s
            if wr.room(h + 30 * s):
                header()
            wr.fill(MARGIN, wr.y - h, total, h, (0.96, 0.965, 0.96))
            wr.text(MARGIN + pad, wr.y - h + 4 * s, clean(r[1]), 9.5 * s, True)
            wr.y -= h
            continue
        if isinstance(r, tuple) and r[0] == 'blank':
            h = 22 * s
            if wr.room(h):
                header()
            wr.y -= h
            wr.line(MARGIN, wr.y, MARGIN + total, (0.72, 0.75, 0.73), 0.7)
            continue
        cells = [_cell_lines(c, w - 2 * pad, s) for c, w in zip(r, widths)]
        h = max([sum(sz * 1.25 for _t, sz, _b, _c in ls) for ls in cells] + [10 * s]) + 6
        if h > PAGE_H - MARGIN - BOTTOM - 40:   # a row taller than a page: keep what fits
            cells = [ls[:40] for ls in cells]
            h = max(sum(sz * 1.25 for _t, sz, _b, _c in ls) for ls in cells) + 6
        if wr.room(h):
            header()
        for ls, x in zip(cells, xs):
            y = wr.y - 3
            for t, sz, b, col in ls:
                y -= sz * 1.2
                wr.text(x + pad, y, t, sz, b, col)
        wr.y -= h
        wr.line(MARGIN, wr.y, MARGIN + total)
    wr.y -= 6


def render(doc):
    """A report document (report.summary or report.med_list) as PDF bytes."""
    s = 1.15 if doc.get('large') else 1.0
    wr = _Writer(s)
    for blk in doc['blocks']:
        k = blk[0]
        if k == 'h1':
            wr.para(blk[1], 17 * s, True, after=2)
        elif k == 'meta':
            wr.para(blk[1], 9 * s, color=GREY, after=6)
        elif k == 'h2':
            wr.room(60)
            wr.y -= 10
            wr.para(blk[1], 12 * s, True, TEAL, after=3)
            wr.line(MARGIN, wr.y, PAGE_W - MARGIN, TEAL, 1.2)
            wr.y -= 2
        elif k == 'note':
            wr.para(blk[1], 8.5 * s, color=GREY, before=2)
        elif k == 'strong':
            wr.para(blk[1], 10 * s, True, before=4)
        elif k == 'footer':
            wr.y -= 8
            wr.room(30)
            wr.line(MARGIN, wr.y, PAGE_W - MARGIN)
            wr.para(blk[1], 7.5, color=GREY, before=2)
        elif k == 'table':
            _table(wr, blk[1], blk[2])
    n = len(wr.pages)
    title = clean(doc.get('title') or '')
    for i, ops in enumerate(wr.pages, 1):
        foot = f'{title}  -  page {i} of {n}'
        wr.ops = ops
        wr.text(PAGE_W - MARGIN - width(foot, 7.5), 28, foot, 7.5, False, GREY)
    return _serialize(wr.pages, title)


def _serialize(pages, title):
    """Write the PDF file: catalog, page tree, two fonts, one compressed content stream per page."""
    objs = []   # object number = index + 1

    def add(body):
        objs.append(body)
        return len(objs)
    catalog = add(None)
    tree = add(None)
    f1 = add(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>')
    f2 = add(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>')
    kids = []
    for ops in pages:
        data = zlib.compress('\n'.join(ops).encode('latin-1'))
        content = add(b'<< /Length %d /Filter /FlateDecode >>\nstream\n' % len(data) + data + b'\nendstream')
        kids.append(add(f'<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] '
                        f'/Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> >> /Contents {content} 0 R >>'.encode()))
    objs[catalog - 1] = f'<< /Type /Catalog /Pages {tree} 0 R >>'.encode()
    objs[tree - 1] = f'<< /Type /Pages /Kids [{" ".join(f"{k} 0 R" for k in kids)}] /Count {len(kids)} >>'.encode()
    info = add(b'<< /Title (' + _esc(title).encode('latin-1') + b') /Producer (healthdash) >>')
    out = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % i + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objs) + 1)
    out += b''.join(b'%010d 00000 n \n' % o for o in offsets)
    out += (b'trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n'
            % (len(objs) + 1, catalog, info, xref))
    return bytes(out)

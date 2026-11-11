"""Turn source files into text, locally.

PDFs: pdftotext per page; pages with no text layer are OCR'd with pdftoppm + tesseract.
TIF/JPG scans: tesseract.  .doc/.docx: LibreOffice headless.  .txt: read as is.
Imaging discs: the DICOMDIR is read for study date, description and modality (no images).

Results are cached by sha256 in a cache directory, so re-running only touches new files.
Needs poppler-utils, tesseract and (for .doc) libreoffice; install them on the machine that runs ingest
(the dashboard itself needs none of them).
"""

import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile

TEXT_KINDS = {'.pdf', '.tif', '.tiff', '.jpg', '.jpeg', '.png', '.doc', '.docx', '.txt', '.rtf'}
# Folders on imaging discs that hold viewer software, not records
SKIP_DIRS = {'reviewer', 'reviewersetup', 'reviewercdsetup', 'fscommand', 'label', 'language',
             'ihe_pdi', 'images', 'dicom', 'data', 'cdviewer', 'content', 'config'}
MIN_PAGE_CHARS = 25


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, check=False, **kw)


def pdf_pages(path):
    """Return (pages, ocr_pages): text for each page and which ones needed OCR."""
    info = _run(['pdfinfo', path]).stdout.decode('utf-8', 'replace')
    n = 0
    for line in info.splitlines():
        if line.startswith('Pages:'):
            n = int(line.split()[1])
    pages, ocr = [], []
    for p in range(1, n + 1):
        txt = _run(['pdftotext', '-layout', '-f', str(p), '-l', str(p), path, '-']).stdout
        txt = txt.decode('utf-8', 'replace')
        if len(txt.strip()) < MIN_PAGE_CHARS:
            txt = ocr_pdf_page(path, p)
            ocr.append(p)
        pages.append(txt)
    return pages, ocr


def ocr_pdf_page(path, page):
    with tempfile.TemporaryDirectory() as tmp:
        _run(['pdftoppm', '-r', '300', '-gray', '-png', '-f', str(page), '-l', str(page), path,
              os.path.join(tmp, 'p')])
        imgs = sorted(f for f in os.listdir(tmp) if f.endswith('.png'))
        return ''.join(ocr_image(os.path.join(tmp, f)) for f in imgs)


def ocr_image(path):
    return _run(['tesseract', path, '-', '--psm', '3']).stdout.decode('utf-8', 'replace')


def office_text(path):
    with tempfile.TemporaryDirectory() as tmp:
        _run(['soffice', '--headless', '--convert-to', 'txt:Text', '--outdir', tmp, path], timeout=120)
        out = [f for f in os.listdir(tmp) if f.endswith('.txt')]
        if not out:
            return ''
        with open(os.path.join(tmp, out[0]), encoding='utf-8', errors='replace') as f:
            return f.read()


def extract_file(path):
    ext = os.path.splitext(path)[1].lower()
    ocr = []
    if ext == '.pdf':
        pages, ocr = pdf_pages(path)
    elif ext in ('.tif', '.tiff', '.jpg', '.jpeg', '.png'):
        pages, ocr = [ocr_image(path)], [1]
    elif ext in ('.doc', '.docx', '.rtf'):
        pages = [office_text(path)]
    else:
        with open(path, encoding='utf-8', errors='replace') as f:
            pages = [f.read()]
    return {'pages': pages, 'ocr_pages': ocr}


# ---- DICOMDIR -------------------------------------------------------------------------

_DICOM_TAGS = {
    (0x0004, 0x1430): 'record_type',
    (0x0008, 0x0020): 'study_date',
    (0x0008, 0x1030): 'study_description',
    (0x0008, 0x0060): 'modality',
    (0x0008, 0x103E): 'series_description',
    (0x0008, 0x0080): 'institution',
    (0x0008, 0x0050): 'accession',
    (0x0020, 0x0011): 'series_number',
}
_LONG_VRS = {b'OB', b'OW', b'OF', b'SQ', b'UT', b'UN', b'OD', b'OL', b'UC', b'UR', b'OV'}


def read_dicomdir(path):
    """Minimal explicit-VR little-endian DICOMDIR reader. Returns a list of studies:
    {date, description, modalities, series: [..], institution}."""
    with open(path, 'rb') as f:
        data = f.read()
    if data[128:132] != b'DICM':
        return []
    pos = 132
    records, cur = [], None
    n = len(data)
    while pos + 8 <= n:
        group, elem = struct.unpack_from('<HH', data, pos)
        if group == 0xFFFE:  # item / delimiters (implicit length markers)
            if elem == 0xE000:
                cur = {}
                records.append(cur)
            pos += 8
            continue
        vr = data[pos + 4:pos + 6]
        if vr in _LONG_VRS:
            length = struct.unpack_from('<I', data, pos + 8)[0]
            hdr = 12
        else:
            length = struct.unpack_from('<H', data, pos + 6)[0]
            hdr = 8
        pos += hdr
        if vr == b'SQ' or length == 0xFFFFFFFF:
            continue  # descend into sequence items
        name = _DICOM_TAGS.get((group, elem))
        if name and cur is not None:
            cur[name] = data[pos:pos + length].decode('latin-1', 'replace').strip(' \x00')
        pos += length
    studies, study = [], None
    for r in records:
        rt = r.get('record_type', '')
        if rt == 'STUDY':
            study = {'date': r.get('study_date', ''), 'description': r.get('study_description', ''),
                     'accession': r.get('accession', ''), 'modalities': [], 'series': []}
            studies.append(study)
        elif rt == 'SERIES' and study is not None:
            m = r.get('modality', '')
            if m and m not in study['modalities']:
                study['modalities'].append(m)
            if r.get('series_description'):
                study['series'].append(r['series_description'])
            if r.get('institution'):
                study['institution'] = r['institution']
    return studies


# ---- folder walk ----------------------------------------------------------------------

def walk(root):
    """Yield ('file', relpath) for record files and ('disc', reldir) for imaging discs."""
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        lower = {f.lower(): f for f in filenames}
        if 'dicomdir' in lower:
            yield 'disc', rel
            # still pick up readable report text files on the disc
            for sub in ('REPORTS', 'reports'):
                rp = os.path.join(dirpath, sub)
                if os.path.isdir(rp):
                    for d2, _, f2 in os.walk(rp):
                        for f in sorted(f2):
                            if f.lower().endswith('.txt'):
                                yield 'file', os.path.relpath(os.path.join(d2, f), root)
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in SKIP_DIRS)
        for f in sorted(filenames):
            if f.startswith('.'):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in TEXT_KINDS or ext in ('.wav', '.mp3', '.m4a', '.iso'):
                yield 'file', os.path.normpath(os.path.join(rel, f))


def extract_all(root, cache_dir, log=print):
    """Extract every record under root into cache_dir/<sha>.json. Returns the manifest list."""
    os.makedirs(cache_dir, exist_ok=True)
    manifest = []
    for kind, rel in walk(root):
        full = os.path.join(root, rel)
        if kind == 'disc':
            dd = next(os.path.join(full, f) for f in os.listdir(full) if f.lower() == 'dicomdir')
            manifest.append({'kind': 'disc', 'path': rel, 'studies': read_dicomdir(dd)})
            continue
        ext = os.path.splitext(rel)[1].lower()
        st = os.stat(full)
        entry = {'kind': 'file', 'path': rel, 'size': st.st_size, 'ext': ext}
        digest = sha256(full)
        entry['sha256'] = digest
        if ext in ('.wav', '.mp3', '.m4a', '.iso'):  # no text to pull out; listed by name only
            entry['no_text'] = True
            manifest.append(entry)
            continue
        out = os.path.join(cache_dir, digest + '.json')
        if not os.path.exists(out):
            log(f'extracting {rel}')
            res = extract_file(full)
            res['path'] = rel
            tmp = out + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(res, f)
            os.replace(tmp, out)
        manifest.append(entry)
    with open(os.path.join(cache_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=1)
    return manifest


def have_tools():
    return {t: bool(shutil.which(t)) for t in ('pdftotext', 'pdftoppm', 'tesseract', 'soffice')}


if __name__ == '__main__':
    import sys
    extract_all(sys.argv[1], sys.argv[2])

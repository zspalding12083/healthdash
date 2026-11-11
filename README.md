# healthdash

A private, self-hosted dashboard for one person's medical history: visits, tests, lab results,
procedures, and when each medication was started and stopped, with bills and statements kept on a
separate tab. Every line links back to the original PDF or scan it came from, and the full text of
every document is searchable.

Built for a family member whose records were a folder of hospital printouts, portal exports,
scanned letters and imaging CDs. It runs on a small Linux box (a Raspberry Pi–class computer) or a
Mac on the home network.

> **Not medical advice. No warranty. Use at your own risk.** This is a personal record-keeping
> tool, not a medical device. It can misread, mis-sort or miss information. Always check the
> original documents and ask a doctor. See [Disclaimer](#disclaimer).

![Timeline tab, demo data](docs/screenshot.jpg)

## What it does

- **Timeline**: every visit, test, procedure and medication change in date order, filterable by type,
  place and year.
- **Medications**: current list plus a history chart of when each one started and stopped (one row
  per dose change).
- **Visits, Tests & imaging, Procedures, Conditions, Allergies, Doctors**: tables you can edit in place.
- **Lab trends**: the latest result of every test with its normal range and the change since last
  time, out-of-range results first, grouped by kind (blood counts, kidney, liver, sugar...). Click a
  test for a chart of every value, one hospital stay at a time or all years.
- **Sign-in**: a few accounts, all equal. The first account is created on the page the first time
  it's opened; after that, anyone signed in can add or remove accounts and reset passwords.
  Forgot every password? `python3 -m healthdash user passwd NAME --db <database>` on the server.
- **Documents**: every source file with date, kind and facility; opens the original. OCR'd text is
  searchable (SQLite FTS5).
- **Add records**: drop PDFs or phone photos onto the page. Each one is read (OCR for scans),
  sorted by kind, date and facility, its lab values and bill amount are pulled out, and it is
  filed. Anything it isn't sure about is put to you as a question, with its best guess filled in.
  Copies of documents already filed are caught and lab values are never added twice.
- **Needs review**: uploads with open questions, new files the catalog doesn't know yet.
- **Financial**: what was billed or quoted, by whom and for what, apart from the health tabs.
  Quotes are kept apart from bills.

Everything runs on your own machine. Nothing is sent to any outside service, API or cloud.

## What it does not do

- **It does not give medical advice** or interpret results. It shows what the documents say; the
  "H"/"L" flags are copied from the lab's own reference ranges. It is not a medical device and has
  not been reviewed or approved by any regulator.
- **It is not always right.** Sorting uploads, reading scans (OCR) and pulling out lab values are
  automatic and make mistakes: wrong dates, wrong kind, misread numbers, missed values. It asks
  when it's unsure, but it can be wrong without asking. The original document is the record.
- **Its sign-in is simple, not internet-grade.** There are a few accounts, all with the same access:
  anyone with an account can see and change everything and manage the other accounts. There are no
  roles, no two-factor codes, and the dashboard speaks plain HTTP, so passwords cross your local
  network unencrypted. It keeps casual visitors on a home network out. Do not expose it to the
  internet.
- **It does not encrypt** the database or the stored files. Protect the machine it runs on (disk
  encryption, a user account others can't use).
- **It is not a backup.** `deploy/sync.sh backup` copies the database and uploaded files to another
  machine, but keeping your original documents safe is up to you.
- **It does not track payments.** Bills record what was charged, not whether it was paid.
- **It is not built for clinics or businesses** and makes no claim of HIPAA or any other
  compliance. It is meant for a person or a family keeping their own records.
- **It does not fetch records** from patient portals, insurers or pharmacies. You download them
  and add them.
- **It does not read imaging** (the CT/MRI pictures themselves). It lists the studies on a disc;
  open them with the disc's viewer.
- **Windows is not supported.** Linux (Debian, Ubuntu, Raspberry Pi OS) and macOS are.

## Privacy design

Medical records are about as sensitive as data gets, so:

- Python 3 standard library + SQLite only. No dependencies, no CDN, no outside API or lookup.
- Text extraction and OCR (`pdftotext`, `tesseract`) run locally.
- Everything needs a signed-in account. Passwords are stored as salted PBKDF2 hashes; sessions use
  an HttpOnly, SameSite=Strict cookie; repeated wrong passwords are slowed down; changes are only
  accepted from the dashboard's own page.
- Without an account, only the configured ingest machine can bulk-import records or download
  backups, and the same computer can only send records in, not read them.
- Request logging is off, so search terms don't end up in system logs.
- On Linux the service runs as its own user with `ProtectSystem=strict`, `ProtectHome`, `UMask=0077`.
- Nothing personal lives in the code: names, facilities and paths go in `data/local.json` and
  `deploy/local.env`, both git-ignored along with every PDF, scan, text cache and database.

## Try it

```bash
python3 -m healthdash demo            # made-up sample data, http://127.0.0.1:9380
python3 -m unittest                   # tests (sample data only)
```

## Check an install

```bash
python3 -m healthdash check            # Python, SQLite FTS5, PDF + OCR tools
python3 -m healthdash check --ingest   # the machine with the records folder (adds LibreOffice)
```

It lists anything missing and the command that installs it on your system. The dashboard also
shows a warning on **Add records** when it can't read files yet; files added meanwhile are kept
and read automatically once the tools are installed.

## Install on a Mac

Everything (the dashboard and reading the records) runs on the one Mac.

```bash
brew install poppler tesseract         # https://brew.sh ; LibreOffice is optional, for old .doc files
python3 -m healthdash check
python3 -m healthdash service install  # starts at login; http://127.0.0.1:9380
```

- `service install --lan` lets other devices on your network open it (see the login warning above).
  `--port` and `--title` change the port and the name shown on the page.
- Data lives in `~/Library/Application Support/healthdash`, the log in `~/Library/Logs/healthdash.log`.
- `python3 -m healthdash service status` and `service uninstall` (your data is kept).
- The service runs the code from this folder, so after `git pull` run `service install` again to restart it.

## Install on Linux (a Raspberry Pi or other server)

The server needs `poppler-utils` and `tesseract-ocr` (`deploy/install.sh` installs them).

On the machine that has the records (needs the same, plus LibreOffice for `.doc`):

1. `cp sample/local.example.json data/local.json` and set `source` to the folder of records.
2. `python3 -m healthdash ingest --dry-run` extracts text (cached in `extracted/`) and lists what it found.
3. Describe files in `data/catalog.json` (title, date, category, facility) and put structured
   history in `data/records.json`; the format is the same as `sample/records.json`. Anything not in
   the catalog still shows up, flagged "needs review", and everything can be edited on the dashboard.
4. `python3 -m healthdash organize` previews a sorted, consistently named copy of the records
   (hard links, originals untouched); add `--apply` to create it.

On the server:

1. `cp deploy/local.env.example deploy/local.env` and fill in the server login, URL and ingest IP.
2. `deploy/install.sh` once (asks for sudo on the server), then `deploy/sync.sh push` for code
   changes, `deploy/sync.sh ingest` for new records, `deploy/sync.sh backup` for a database copy.
3. Open the dashboard right away and create the first account: until one exists, whoever opens
   the page first can create it.

Edits made on the dashboard are kept: re-importing never overwrites a record someone changed by hand.

## Firefox plugin: save records as you view them

Patient portals (MyChart, FollowMyHealth, HealtheLife and others) rarely offer a bulk download. The
plugin in `browser-extension/firefox` sends what you open to your dashboard instead.

- **Off until you switch it on** from its toolbar button. While off it runs on no website at all.
- Only works on the portal sites you list in its settings, and only sends to your dashboard's address.
- **Save this page** sends the page you're looking at; **Save every portal page I open** does it for you.
  PDFs you download from those sites are sent too (when the portal builds a PDF inside the page the
  plugin can't fetch it again; it says so and you drop that file on the dashboard).
- The dashboard reads and sorts each one like any other upload. Pages with almost no text, and pages
  it already has, are dropped instead of asking you about them.
- It uses a **plugin key** (dashboard: 👤 → Accounts → Browser plugin keys), which can only add files
  and check on them; it can't read or change records. Revoke it there at any time.

Install: `browser-extension/build.sh` makes `dist/healthdash-capture-firefox.zip`. Firefox only keeps
signed add-ons installed: upload the zip at https://addons.mozilla.org/developers/ as **"On your own"**
(unlisted, signed automatically, not published), then open the signed `.xpi` in Firefox. To just try it,
use `about:debugging` → *This Firefox* → *Load Temporary Add-on* (removed when Firefox restarts). Then open
the plugin's settings, enter the dashboard address and key, and allow the sites when Firefox asks.

## Layout

```
healthdash/
  extract.py    PDF text + OCR, DICOMDIR headers from imaging discs, cached by sha256
  catalog.py    per-file metadata; guesses dates/titles for new files
  parsers.py    lab printout parsers (portal lists, Cerner-style result blocks) + analyte names
  organize.py   sorted, renamed hard-link copy of the source folder
  ingest.py     extract -> describe -> POST to the dashboard -> upload missing files
  classify.py   what an uploaded document is (kind, date, facility, title, amount) + questions
  uploads.py    files added on the dashboard: save, read in the background, file or ask
  doctor.py     `check`: what's installed and how to install what isn't
  auth.py       accounts, password hashing, sessions
  macos.py      `service`: launchd setup on a Mac
  store.py      SQLite schema, FTS search, timeline, lab series, edit tracking
  server.py     JSON API + static page
  static/index.html   the whole UI, no build step
deploy/         Linux server: install.sh, sync.sh, systemd unit
sample/         made-up demo data and an example local.json
```

## Disclaimer

This software is provided **as is**, without warranty of any kind, express or implied, including
fitness for a particular purpose, accuracy, or that it will keep your data safe or private. The
author is **not liable** for any damages or losses of any kind arising from using it or being unable
to use it, including medical decisions, missed or wrong information, data loss, or disclosure of
personal or health information. It is not medical, legal or financial advice. You are responsible
for checking everything it shows against the original documents and for protecting your own
records. The [License](LICENSE.md) has the full "No Liability" terms, which apply as far as the law
allows.

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md). You may use, change and share this for any
noncommercial purpose: personal use, family use, hobby projects, study, and use by nonprofits,
schools, public health organizations and government. Selling it, or using it in or for a business,
is not allowed without written permission. For commercial use, open an issue to ask.

Required Notice: Copyright 2026 zspalding12083 (https://github.com/zspalding12083)

# healthdash: how the system works

This is the technical companion to the README: what the parts are, how records get in, how
imported data is kept tidy, and how to run and change it. The README covers installing and using it.

## Principles

- **Local only.** Python 3 standard library and SQLite. No dependencies, no CDN, no outside lookups.
  Text extraction and OCR (`pdftotext`, `tesseract`) run on your own machines.
- **The original document is the record.** Every entry points back to the documents it came from,
  and the dashboard can open them at the right page.
- **A person's edit always wins.** Anything changed or added by hand is marked `edited` and is never
  overwritten by a later import. Deleted entries and files are remembered and don't come back.
- **Ask, don't guess.** When something is unsure (a date, a duplicate, a stale medication) the
  dashboard asks on the Review tab instead of changing it silently.
- **Nothing personal in the code.** Names, places, hosts and a person's own wordings live in
  git-ignored files (`data/local.json`, `deploy/local.env`, `data/private_terms.txt`) and in each
  person's database. Tests and `sample/` use made-up data only.

## The parts

```
 records folder ──► ingest (extract, catalog, records.json) ──► POST /api/import ─┐
                                                                                   ▼
 patient-portal exports (.zip/.xml), PDFs, photos ──► upload ──► dashboard server (SQLite per person)
 Firefox plugin (saved portal pages)            ──►            ▲         │
                                                    browser ───┘         └─► files/ (originals by sha256)
```

| Module | What it does |
|---|---|
| `server.py` | HTTP server: the page, the JSON API, file downloads, uploads, sign-in. One handler serves every person on the dashboard; requests pick one with the `X-Person` header (`?p=` for links). |
| `store.py` | The database: schema, records of each kind, documents and their full-text index, timeline, lab series, review queue, settings, and the passes that keep imported records tidy. |
| `uploads.py` | Files added from the dashboard or plugin: checked, stored by sha256, read by a background worker, sorted and turned into entries. Imports health summaries. Runs the one-time passes. |
| `ccda.py` | Reads C-CDA documents (the standard "health summary" a portal exports, often as an IHE XDM zip): the visit, results, medications, problems, allergies, procedures, vaccines and a readable page. Also drug names (`drug_key`) and hospital-department places. |
| `fhir.py` | Reads FHIR bundles (some portals' "all my data" export) into the same shapes. |
| `parsers.py` | Lab results from printouts and portal pages (several layouts, including LabCorp reports), and one name per lab test (`standard_name`, `display_name`). |
| `classify.py` | What an uploaded document is: kind, date, place, title, amounts, and the questions to ask when unsure. |
| `codes.py` | Medical codes on conditions and procedures (ICD-10-CM, ICD-9-CM, SNOMED CT, CPT, HCPCS, ICD-10-PCS) and medications (RxNorm): read from C-CDA and FHIR (only the systems that fit the kind of entry, `FOR_KIND`), checked and written one way when typed, merged when entries merge. No code tables, no lookups. |
| `report.py` | The printouts: the health summary (`summary`: current conditions, allergies and medicines, procedures, life events, flagged labs, a dated history, past conditions and medicines) and the quick list of allergies and current medicines (`med_list`). Each is built once as a list of blocks, then drawn as a self-contained HTML page (`to_html`) or a PDF. |
| `pdf.py` | A small PDF writer (standard library only): report blocks on US Letter pages in the PDF base fonts (Helvetica), tables that continue across pages with their headings, "page N of M". Latin-1 text; common typographic characters are written plainly. |
| `checks.py` | Record checks: questions on the Review tab about likely mistakes (see below). |
| `extract.py` | Text of PDFs (with OCR for scanned pages), office files, web pages, and DICOMDIR headers of imaging discs; cached by sha256. |
| `catalog.py`, `organize.py`, `ingest.py` | The records folder: per-file details, a sorted copy, and sending everything to the dashboard. |
| `people.py` | Several people on one dashboard, each with their own database and files. |
| `auth.py` | Optional sign-in: whether it is on, accounts (salted PBKDF2), sessions (HttpOnly, SameSite=Strict cookie) and plugin keys. |
| `config.py` | Settings from `data/local.json`. |
| `doctor.py`, `macos.py`, `wipe.py`, `demo.py` | `check`, the macOS service, starting over, and the sample data. |
| `static/index.html` | The whole interface, no build step. |

## Data model

One SQLite database per person (`health.db` for the first, `people/<id>/health.db` for others).

- **documents**: one row per file (id = sha256), with title, date, kind, place, page count, which
  pages were read by OCR, the full text (in an FTS5 index), `needs_review` and `person_edited`.
  Imported health summaries also get one readable page per document they contain.
- **One table per kind of record** (`KINDS` in `store.py`): `encounters` (visits), `tests`, `labs`,
  `procedures`, `medications`, `conditions`, `allergies`, `imaging`, `immunizations`, `providers`,
  `life_events`.
  Every row also has `id`, `docs` (the documents it came from), `needs_review`, `edited`, `origin`
  (`import` or `manual`) and `updated`. Imported rows have stable ids (`ccda-...`), so importing
  the same file again updates instead of doubling.
- **medications**: one row per drug per period of use; a dose change closes one row and opens the
  next. Over-the-counter medicines are a single row without dates; drugs given during a procedure
  are shown apart.
- **codes** (on `conditions`, `procedures` and `medications`): the medical codes the records give, or a
  person types, in one text field written the same way everywhere: `ICD-10-CM M54.50; SNOMED CT 279039007`,
  `RxNorm 866924` (an RxNorm code names the drug, strength and form, so a dose change is a new code).
  Search finds an entry by its code. When two entries merge, the kept one gets every code. Codes from
  a document are added to an entry; a person's own codes are never replaced.
- **life_events**: things outside medical care that may bear on health (an accident, a death or
  illness in the family, a move, a stressful time): date (a year or month is fine), until, title,
  kind (`LIFE_CATEGORIES` are offered, any word is kept), how it may affect health, notes. Entered by
  hand only; shown on the Timeline, their own tab and the printed summary.
- **moved**: entries and files deleted on the dashboard (or moved to another person), so an import
  doesn't bring them back.
- **setting**: key/value settings, including what the record checks learned (below) and the
  version of each one-time pass.
- **drug_alias**, **place_alias**: brand and generic names that are one drug; other spellings of a place.
- In the first database only: **people**, **users**, **sessions**, **api_keys**.

## How records get in

1. **From the records folder** (`python3 -m healthdash ingest`): every file is read (text, OCR,
   DICOMDIR), described from the catalog, matched with hand-built records (`data/records.json`),
   sent to `/api/import` from the ingest host, and the files the server doesn't have are uploaded.
2. **Uploaded on the dashboard or by the plugin**: the file is stored under its sha256 and queued.
   The worker reads it, works out what it is (`classify.py`) and takes lab results from it
   (`parsers.py`). Unsure details are marked for review.
3. **Health summary exports** (`.zip`/`.xml`): each C-CDA document (or FHIR bundle) becomes a
   readable page plus its visit, results, medications, problems, allergies, procedures, vaccines and
   imaging reports. Things already on the dashboard (a visit entered by hand on the same day, the
   same result from another file) are linked rather than added twice.
4. **Many files at once**: on the dashboard, **Add records** takes many files or a whole folder (its
   subfolders too), and files or folders can be dragged onto the page. Files the dashboard can't
   read are skipped and listed; four or more files share one progress card. A **zip of loose
   records** (PDFs, pictures, summaries in any folders) is unpacked on the server and each file is
   read as if added on its own (`uploads.unpack_bundle`, up to 5,000 files and 4 GB unpacked); the
   zip stays listed with a note of what was in it. A zip is a portal export instead when it holds a
   FHIR `.json`, the IHE XDM layout, or only `.xml` documents (`uploads.is_export`).

### Reading summaries carefully

- **Plan sections** (treatment plan, upcoming appointments) describe the day a summary was
  exported, not its visit. When a summary was exported more than a week after its visit, those
  sections say so on the page.
- **Hospital departments** named as the place ("Sleep Lab Hospital", "Minor OR") at the issuing
  hospital's own street address are shown as that hospital.
- **Procedures** listed in a summary take its place only if they were done during its visit.
- **Codes**: a problem's codes come from its `value` element and the `translation`s under it (often
  SNOMED CT with an ICD-10-CM translation); a procedure's from its `code` element; a medication's
  RxNorm code from its `manufacturedMaterial` code (other drug vocabularies there are left out), kept
  through the merge into one entry per drug per period. Code systems are
  known by OID (`codes.OIDS`) or FHIR system URL (`codes.URLS`); others are left out.
- **Vaccines**: one entry per vaccine per day, however many documents list it; the same shot
  recorded by two systems a day apart (one without a lot number) is kept once.

## Keeping imported data tidy

After every import these passes run (they never touch an entry a person edited):

| What | One entry per | How names are matched |
|---|---|---|
| Medications (`store.consolidate_meds`, `merge_med_sources`) | drug per period of use (lines less than 60 days apart join) | `ccda.drug_key`: the drug a line is about; combinations written with `-` or `/` in any order are one drug; `DRUG_NAMES` (brands, abbreviations); brand/generic pairs learned from lines like "generic (Brand)" (`drug_alias`). An imported line for a drug also entered by hand over the same time is folded into the hand entry. |
| Conditions (`store.merge_conditions`) | condition | labels like "(Confirmed)" and "Hx of" ignored; `CONDITION_NAMES` (general synonyms); the person's own wordings (`condition_names` in `local.json`, sent with an import); "same" answers on the Review tab. A hand-made entry keeps its wording. |
| Lab tests (`parsers.normalize`, `uploads.tidy_lab_names`) | test | `parsers.SYNONYMS` (including long official names and urinalysis), LabCorp footnote letters removed, capitals ignored, learned names; device lines (serial numbers, operator names, draw times, comments) are not results. The same value on the same day under two names is kept once. |
| Places (`store.tidy_places`, `tidy_imported_places`) | spelling | `place_alias`, the department rule above, case and spacing. |

## The Review tab

- **Needs review**: entries and files with details that weren't certain, each with a plain question
  and "Looks right", "Change it", "See the source" and "Delete".
- **Things that look wrong** (`checks.py`), found by checking the records against each other:
  - a prescription still listed as current that looks stale: a short supply with no refills that ran
    out long ago, or one newer documents stopped listing (with a suggested stop date);
  - two conditions, lab tests or medications that look like one under two names (never when they
    differ by a number, a specimen or body site, IgG/IgM, RBC/WBC, left/right, or are opposites);
  - a place that looks like a hospital department.

  Each question shows both sides with their results or dates and a button to the source document.
  "Same" merges and is learned (`lab_alias` and `condition_alias` settings, `drug_alias`), so later
  imports group it without asking; "not the same" is remembered (`checks_dismissed`). Nothing
  changes until someone answers. The Review badge counts both lists.

## One-time passes

When the way something is read changes, data already saved is brought up to date once, by a pass
the upload worker runs on startup. Each pass records its version in the `setting` table; bump the
constant in the code to run it again.

| Setting | Constant | What it does |
|---|---|---|
| `page_version` | `ccda.PAGE_VERSION` | draw saved summary pages again |
| `vaccines_version` | `uploads.VACCINES_VERSION` | read vaccines from summaries loaded before |
| `places_version` | `uploads.PLACES_VERSION` | department places; procedure places that a visit that day contradicts |
| `labnames_version` | `uploads.LABNAMES_VERSION` | rename saved results to the standard names and merge duplicates |
| `conditions_version` | `uploads.CONDITIONS_VERSION` | merge saved conditions with the current synonyms |
| `meds_version` | `uploads.MEDS_VERSION` | merge saved medications with the current drug names |
| `codes_version` | `uploads.CODES_VERSION` | add the codes of problems, procedures and medications in summaries loaded before codes were kept: a condition gets the codes of every problem grouped with it (`store.condition_key`, the same grouping as the merge), a procedure those of a summary procedure with its name and date, or a related name a day either side (`store.procedures_related`), a medication the RxNorm codes of lines with its name, or else of the same drug and strength. Version 2 added medications; codes are only ever added, so running it again is safe. |

Passes are written to be safe to run twice and to leave hand-edited entries alone. Try a new one
on a copy of a backup before deploying (`deploy/sync.sh backup`, then run the method against the
copy and compare).

## API

All under `/api/`, JSON, for the signed-in user (anyone, when sign-in is off) unless noted. The person is chosen with `X-Person`.

| Route | |
|---|---|
| `GET summary`, `timeline`, `search?q=`, `places`, `labs/series`, `review`, `checks` | home page numbers, everything by date, full-text search, place names, lab trends, the two review lists |
| `GET/POST <kind>`, `GET/PUT/DELETE <kind>/<id>` | records of each kind; PUT marks an entry edited |
| `GET documents`, `GET/PUT/DELETE documents/<id>`, `POST documents/<id>/retry` | files |
| `POST upload?name=` | add a file (also with a plugin key, and from the ingest host) |
| `POST checks` `{key, action}` | answer a record check |
| `POST medications/current-list` | set a pasted medication list against the records |
| `GET/POST people`, `PUT people/<id>` | the people on the dashboard |
| `POST import`, `POST files/<sha>`, `GET backup`, `GET uploads`, `GET files/missing` | ingest host only |
| `GET auth`, `POST setup`, `login`, `logout`; `keys`, `users` | sign-in and accounts |
| `POST login-required` `{on, username, password}` / `{on: false}` | turn sign-in on (signs in with that account, or makes it) or off (only when signed in) |

`GET /file/<sha>` sends a stored file (`?download=1` to save it).

`GET /report` is the printable summary for the person in `?p=`. Options: `from`, `to` (YYYY,
YYYY-MM or YYYY-MM-DD), `sections` (comma list of `overview, procedures, life, labs, history, past`),
`kinds` (what the history lists: `encounters, tests, imaging, procedures, medications,
immunizations, life_events`), `order=oldest`, `download=1` (the same page as an `.html` file),
`format=pdf` (a PDF instead; with `download=1` saved as a file). `view=meds` gives the quick list of
allergies and current medicines instead (`otc=0` leaves out over-the-counter ones). The
page allows no outside content and only its own Print script (a per-page nonce, also in a meta tag
so a saved copy keeps the rule).

## Security and privacy

- **Sign-in is optional** and off on a new dashboard: anyone who reaches it on the network can use
  it. It is turned on in the Accounts dialog (which makes the first account) or with
  `python3 -m healthdash login on --db DB`, and off the same ways (`login off` is also the way back
  in after a lost password, with `user passwd`). The choice is the `login_required` setting; a
  dashboard with accounts and no saved choice (set up before sign-in was optional) keeps it on.
- With sign-in on, every route needs a signed-in account, except the page itself and sign-in.
  Plugin keys can only upload and list (a key made while sign-in was off stops working when it is
  turned on). Without an account only the configured ingest host can import or back up, and the
  server's own machine can only send records in.
- Changes are only accepted from the dashboard's own page (the `Origin` check), and every request
  must name the dashboard by an address, a name without dots or a local-only name (`.local`, `.lan`,
  `.home.arpa`, ...), or one given with `serve --allow-host NAME` (`server.host_ok`): a website
  can't point its own domain at the dashboard to read it (DNS rebinding). Responses can't be framed
  by other sites and send no referrer. Repeated wrong passwords are slowed down; request logging is off.
- Uploads are checked by their first bytes, stored under their sha256 and read by `pdftotext` and
  `tesseract` (never through a shell, five minutes at most per page or file). Health summaries with
  a DOCTYPE or entities are refused. Saved web pages are shown in a sandbox with no scripts.
- See `docs/SECURITY.md` for the latest review: what was checked, fixed and left as known limits.
- On Linux the service runs as its own user with `ProtectSystem=strict`, `ProtectHome` and `UMask=0077`.
- The public copy of the code is made by `tools/export_public.sh`, which copies an allowlist and
  stops if any file contains a private term (names, places, doctors, medications and dates of care
  from `data/`, plus `data/private_terms.txt`).

## Running it

- **Install**: `deploy/install.sh` (Linux server) or `python3 -m healthdash service install` (Mac).
- **Update**: `deploy/sync.sh push` runs the tests, copies the code, restarts every dashboard on the
  server and checks they're up.
- **Back up**: `deploy/sync.sh backup` keeps a copy of each database and the uploaded files.
- **More people**: the People menu on the dashboard, or `deploy/add-person.sh` for a separate dashboard.
- **Check a machine**: `python3 -m healthdash check`.

## Changing it

- **Tests**: `python3 -m unittest discover -s tests`, made-up data only.
- **Another name for a lab test**: add it to `parsers.SYNONYMS` (lower case → shown name), bump
  `LABNAMES_VERSION`.
- **Another wording of a condition**: general synonyms go in `store.CONDITION_NAMES` (bump
  `CONDITIONS_VERSION`); a person's own wording goes in their `local.json` `condition_names`.
- **A brand name**: `ccda.DRUG_NAMES` (bump `MEDS_VERSION`).
- **A new record check**: a function in `checks.py` returning questions with `key`, `kind`, `title`,
  `ask`, `note`, `sides` and `answers`, added to `issues()`; handle its answers in `answer()`.
- **A new kind of record**: add it to `KINDS` (the table is created on startup), to `FIELDS` and
  `KIND_LABEL` in `index.html`, and to the importer that fills it.
- **A new field on an existing kind**: add it to `KINDS` and to `ADDED` in `store.py` (so existing
  databases get the column), and to `FIELDS` in `index.html`.
- **Another code system**: its OID in `codes.OIDS`, FHIR URL in `codes.URLS`, name in `codes.SYSTEMS`,
  typed short forms in `codes.ALIASES` and code shape in `codes.SHAPE`; bump `CODES_VERSION` to read
  saved summaries again.
- **Another part of the printed summary**: a section in `report.SECTIONS` and `build()`, and its
  checkbox in `REPORT_SECTIONS` in `index.html`.

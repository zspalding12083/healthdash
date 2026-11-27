# Security review

Last review: 2026-11-25 (second pass, after RxNorm codes and PDF printouts), of the whole codebase
(server, sign-in, uploads, importers, page, deploy scripts). The first pass that day came with the
changes that made sign-in optional and added bulk import, medical codes and the printable summary.

**What this dashboard is built to withstand:** other people and devices on a home network, and
websites open in the browser of someone who uses the dashboard. **What it is not built for:** the
open internet, a hostile person with a shell on the server, or a compromised ingest machine. Within
that scope, nothing serious is open. Each finding is listed below with what was done about it.

## Fixed in this review

| # | Finding | Risk | Fix |
|---|---|---|---|
| 1 | **DNS rebinding.** A website could point its own domain at the dashboard's address and read every record from a visitor's browser: the `Origin` check only covers requests that change something, and same-origin rules see the look-alike domain as its own site. This gets worse once sign-in can be turned off. | High (with sign-in off) | Every request must name the dashboard by an IP address, a name without dots, a local-only name (`.local`, `.lan`, `.home.arpa`, `.internal`, `.localhost`, `.test`, `.home`) or a name given with `serve --allow-host`; anything else gets 421 (`server.host_ok`). |
| 2 | **Clickjacking.** Another site could show the dashboard in a hidden frame and trick a click (Delete, Turn off sign-in). | Medium | `X-Frame-Options: SAMEORIGIN` on every response (the page still frames its own files). Also `Referrer-Policy: no-referrer`. |
| 3 | **Unescaped dates and document ids in the page.** `fdate()` wrote the year part of a date into HTML as is, and document ids went into `data-` attributes unescaped. Records' dates are checked, but document dates and ids sent by the ingest host were not, so a bad import could have put script into the page. | Low (needs the ingest host) | `fdate()` escapes its output; the year headings on the Timeline are escaped; document ids must be letters, numbers, `.`, `_`, `-` (`store.SAFE_ID`), checked on import. |
| 4 | **Reading tools with no time limit.** `pdftotext`, `pdftoppm` and `tesseract` ran without a timeout on uploaded files, so one crafted or broken file could stall the single upload worker for good. | Medium | Five minutes per page or file (`extract.TOOL_TIMEOUT`); the file is marked "couldn't be read" and the worker moves on. |
| 5 | **Zip of records: size.** (New code.) A zip unpacked file by file could fill the disk (zip bomb). | Medium | At most 5,000 files and 4 GB unpacked per zip, 200 MB per file; reading stops at each file's stated size, so a zip can't claim small and unpack big. Hidden files and `__MACOSX` are skipped. |
| 6 | **Printed summary and scripts.** (New code.) The page needs one script for its Print button; a loose script policy would let anything written into a record run. | Low | Every value is escaped, and the page allows only its own script by a random per-page nonce, in the response header and in a `<meta>` tag so a downloaded copy keeps the rule. No outside fonts, images or links. |
| 7 | **Sign-in switch.** (New feature.) Turning sign-in on must not let someone take over an existing account, and turning it off must not be possible from outside. | — | Turning on with an existing account name checks its password; with a new name it makes the account (8+ characters). Turning off needs a signed-in user when sign-in is on, and signs everyone out. A dashboard that already had accounts keeps sign-in on after the update. Plugin keys made while sign-in was off stop working when it's turned on. |

## Fixed in the second pass

| # | Finding | Risk | Fix |
|---|---|---|---|
| 8 | **Script in stored `.xml` files.** Stored files were sent as they are, and only saved web pages had a sandbox policy. A browser shows an `.xml` file as a page, and one holding XHTML `<script>` runs as the dashboard: it could read every record and act as whoever opened it. Anyone who can upload could plant one, including a browser-plugin key, which is otherwise only allowed to add files. | High (with sign-in on: a plugin key could act as a full account) | Every stored file except a PDF is sent with `sandbox; default-src 'none'` (`server.FILE_CSP`), so nothing in it runs or loads. PDFs are left out because browsers refuse to show a sandboxed PDF; their viewers don't run PDF script with the page's rights. |
| 9 | **DOCTYPE check could be stepped around.** Health summaries were checked for a DOCTYPE only in their first 4 KB and for entities in their first 200 KB, so a long comment first got one past the check. Python's XML reader (expat 2.4 and later) caps entity expansion anyway, so this was a second line, not the only one. | Low | The whole file is searched (`ccda.DTD`). |
| 10 | **Export zips: total size.** A portal export zip was limited per file (50 MB) and by file count (2,000), but not in total, and its files are held in memory while it is read. A crafted zip could claim up to 100 GB and exhaust memory. | Medium | At most 2 GB unpacked per export (`ccda.MAX_UNPACKED`), checked before anything is read. |
| 11 | **Downloads of files with non-Latin names.** A file name with letters outside Latin-1 (Chinese, Greek, ...) broke the download: HTTP headers can't carry them, so the response came out garbled. | Low (a bug, not a hole) | Such letters become `_` in the download name. |
| 12 | **Whole files in memory on download.** Each download read the whole file (an imaging zip can be hundreds of MB) into memory first. | Low (home-network denial of service) | Files are sent in 1 MB pieces. |

## Choosing where files are kept (2026-11-26)

Reviewed with the feature (`storage.py`, `POST /api/storage`):

- **Who can change it**: anyone who can use the dashboard (a signed-in account, or anyone on the
  network with sign-in off, who can read everything anyway). Not plugin keys, and not the ingest host.
- **Which folders**: an existing absolute folder the dashboard can write to; `/`, system folders
  (`/etc`, `/usr`, `/System`, ...) and the program's own folder are refused. On Linux the service's
  systemd limits still apply: it can only write where `ReadWritePaths` allows.
- **Nothing else is deleted**: the readable copy only ever removes files listed in its own list,
  and an entry counts only if it is inside the folder (not the store, not through a symlink),
  points at a stored file, and is still that file. Someone who can write to a shared folder can't
  make the dashboard delete files elsewhere (such as its database) by editing the list (tested).
- **Originals stay unchanged**: stored files are made read-only, since the readable copy shares
  them; moves to another disk are checked by sha256 before the original is removed, and a failed
  move puts back what it moved.
- **Limit**: the readable copy is as private as the folder it's in. A shared folder shares the
  records with everyone who can open it; on a Mac its folders are created readable by other
  accounts on the computer unless the chosen folder isn't.

Tests for each are in `tests/test_auth.py`, `tests/test_features.py` and `tests/test_storage.py`.

## Checked and found sound

- **Passwords**: salted PBKDF2-SHA256, 310,000 rounds, compared in constant time; an unknown user
  name costs the same time as a wrong password. Five wrong passwords from one address lock it for a minute.
- **Sessions**: 32 random bytes, only the SHA-256 kept; HttpOnly, `SameSite=Strict`; changing a
  password signs that account out everywhere; 30-day expiry.
- **Cross-site requests**: anything that changes data must carry the dashboard's own `Origin` (plugin
  keys excepted, which are never sent automatically); `SameSite=Strict` cookies on top.
- **SQL**: values are always bound parameters. Table and column names come only from the fixed `KINDS`
  list and checked field names, never from a request. Search terms are quoted for FTS5.
- **Files**: stored and served by sha256 name only (`/file/<64 hex>`); the page's static route
  refuses `/` and dot-names. Saved portal pages are served with a sandbox policy that allows no
  scripts and no outside loads. Uploads are checked by their first bytes, not only their name.
- **XML**: health summaries with a DOCTYPE or entity declarations anywhere are refused (no XXE or
  "billion laughs"); Python's parser doesn't fetch outside entities and caps entity expansion anyway.
- **Sign-in, again** (second pass): turning sign-in off needs a signed-in browser session (a plugin
  key can't); setup is refused once any account exists; removing an account ends its sessions and
  keys; a session only counts while its account exists. Backups, imports and the uploads list
  still need the ingest host's address when sign-in is off.
- **Paths**: every file path is built from a checked sha256 or a document id from the database,
  never from a name in a request; `/static/` refuses `/` and names starting with `.`, and the path
  isn't URL-decoded, so `%2e%2e` is just a missing file.
- **Outside programs** run with an argument list, never a shell, so a file name can't become a command.
- **Logs**: request logging is off, so searches and names don't land in the system journal.
- **Service** (Linux): own system user, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`,
  `PrivateTmp`, data folder mode 0700, `UMask=0077`.
- **No outside calls**: no CDN, font, API or lookup from the server or the page; medical codes are
  stored and shown, never looked up.
- **Public copy**: `tools/export_public.sh` copies an allowlist and stops on any private term.

## Known limits (by design, or left for later)

| Limit | Why it is accepted / what would change it |
|---|---|
| **Sign-in off means open.** With sign-in off, anyone who can reach the dashboard on the network can read and change everything, and turn sign-in on with their own account. | This is the chosen default for a single household on a trusted home network. Turn sign-in on if anyone else uses the network (guests, roommates, smart devices you don't trust). |
| **Plain HTTP.** Passwords, session cookies and records cross the local network unencrypted; cookies aren't marked `Secure`. | Fine on a trusted home network; not on shared Wi-Fi. To change it, put a local TLS reverse proxy (Caddy, nginx) in front and add `--allow-host` for its name. Never expose the port to the internet. |
| **Trust by address for the ingest host.** `--import-from` addresses can import records and download a full backup (including password hashes) without an account. | It's how the scripts on the records machine work. Anyone who controls that machine, or can take over its address on the network, gets the data. Keep that machine's address fixed (DHCP reservation) and the machine itself locked. |
| **All accounts are equal.** Any account can read everything, manage other accounts and turn sign-in off. | Simple by design for a family. Roles would be needed before giving access to anyone you wouldn't give everything. |
| **No encryption at rest.** The database and stored files are plain on disk. | Use full-disk encryption on the server and keep backups on encrypted disks. |
| **Memory per request.** An upload of up to 200 MB is held in memory (downloads are now sent in pieces), and the server starts a thread per connection, so a device on the network could exhaust memory. | A home-network denial of service is accepted. A reverse proxy with request limits would close it. |
| **Lockout is per address and in memory.** It resets on restart and doesn't stop slow guessing from many addresses. | Acceptable with 8+ character passwords on a home network. |
| **The page itself has no script policy.** `index.html` uses inline scripts, so a Content-Security-Policy can't block injected script there; every value is escaped instead. | Moving the script into its own file would allow a strict policy. Worth doing if the page grows much further. |
| **PDFs open in the browser from the dashboard's own address**, without the sandbox other files get. | Browsers show PDFs in their own sandboxed viewer; PDF scripts don't get the page's rights. |
| **Error messages name the error.** A 500 reply includes the Python error text, which can show a path on the server. | Helps when something breaks; only people who can already use the dashboard see it. |
| **Very long PDFs** are read page by page with a limit per page, not per file; a huge (thousands of pages) PDF ties up the worker for a long time. | Rare for real records; a page cap could be added. |
| **Reading tools** (poppler, tesseract, LibreOffice) process untrusted files and have had security bugs. | Keep the server's packages updated (`apt upgrade`); they run as the unprivileged service user under the systemd limits above. |

## Reviewing again

Look again after changes to `server.py` (routes, `_allowed`), `auth.py`, anything that writes HTML
(`static/index.html`, `report.py`, `ccda.page`, `fhir.summary_page`), file handling in `uploads.py`,
or the deploy scripts. The tests that guard these are in `tests/test_auth.py`, `tests/test_uploads.py`
and `tests/test_features.py`.

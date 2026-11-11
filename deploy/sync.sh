#!/usr/bin/env bash
# Keep this folder (on the machine with the records) and the dashboard server in step.
#
#   deploy/sync.sh push      test locally, copy code to the server, restart, check it's up
#   deploy/sync.sh ingest    read new files in the source folder (OCR on this machine), refresh the
#                            organized copy, rebuild data/records.json, send everything to the server
#   deploy/sync.sh backup    copy the server's database into backups/ (git-ignored)
#
# Settings come from deploy/local.env (git-ignored; see local.env.example). Needs SSH keys for
# the server login, /opt/healthdash/healthdash owned by it, /etc/sudoers.d/healthdash-restart.
# First-time setup: deploy/install.sh
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
cd "$HERE"
[ -f deploy/local.env ] || { echo "copy deploy/local.env.example to deploy/local.env and fill it in"; exit 1; }
# shellcheck disable=SC1091
. deploy/local.env
: "${PI:?}" "${URL:?}"
CODE=/opt/healthdash/healthdash
KEEP=30

push() {
    python3 -m unittest discover -s tests -t . -q
    rsync -a --delete --exclude __pycache__ --exclude '*.pyc' healthdash/ "$PI:$CODE/"
    ssh "$PI" 'sudo -n /usr/bin/systemctl restart healthdash && sleep 2 && systemctl is-active healthdash'
    curl -fsS --max-time 10 "$URL/api/summary" >/dev/null && echo "dashboard is up: $URL"
}

ingest() {
    [ -f data/build_records.py ] && python3 data/build_records.py
    python3 -m healthdash organize --apply
    HEALTHDASH_PI="$URL" python3 -m healthdash ingest
}

backup() {
    mkdir -p backups && chmod 700 backups
    local out; out="backups/health-$(date +%Y%m%d-%H%M).db"
    curl -fsS --max-time 120 -o "$out" "$URL/api/backup"
    chmod 600 "$out"
    python3 -c "import sqlite3,sys; sqlite3.connect(sys.argv[1]).execute('select count(*) from documents')" "$out"
    ls -1t backups/health-*.db | tail -n +$((KEEP + 1)) | xargs -r rm --
    echo "backed up to $out ($(du -h "$out" | cut -f1))"
}

case "${1:-}" in
    push) push ;;
    ingest) ingest ;;
    backup) backup ;;
    *) sed -n '2,12p' "$0"; exit 1 ;;
esac

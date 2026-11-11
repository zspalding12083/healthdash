#!/usr/bin/env bash
# Keep this folder (on the machine with the records) and the dashboard server in step.
#
#   deploy/sync.sh push      test locally, copy code to the server, restart, check it's up
#   deploy/sync.sh ingest    read new files in the source folder (OCR on this machine), refresh the
#                            organized copy, rebuild data/records.json, send everything to the server
#   deploy/sync.sh backup    copy the server's database, and every file added from the dashboard,
#                            into backups/ (git-ignored); other people's dashboards into backups/NAME/
#
# Other dashboards (deploy/add-person.sh) are listed in deploy/local.env as OTHERS="name:port ...";
# push updates and restarts them too.
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
OTHERS=${OTHERS:-}
HOST=${URL#*://}; HOST=${HOST%%:*}; HOST=${HOST%%/*}

push() {
    python3 -m unittest discover -s tests -t . -q
    rsync -a --delete --exclude __pycache__ --exclude '*.pyc' healthdash/ "$PI:$CODE/"
    ssh "$PI" 'sudo -n /usr/bin/systemctl restart healthdash && sleep 2 && systemctl is-active healthdash'
    for entry in $OTHERS; do
        ssh "$PI" "sudo -n /usr/bin/systemctl restart healthdash-${entry%%:*} && sleep 1 && systemctl is-active healthdash-${entry%%:*}"
    done
    ssh "$PI" 'PYTHONPATH=/opt/healthdash python3 -m healthdash check >/tmp/healthdash-check.txt 2>&1' \
        || { echo "WARNING: the server is missing something:"; ssh "$PI" cat /tmp/healthdash-check.txt; }
    curl -fsS --max-time 10 "$URL/api/summary" >/dev/null && echo "dashboard is up: $URL"
    for entry in $OTHERS; do
        curl -fsS --max-time 10 "http://$HOST:${entry##*:}/api/auth" >/dev/null && echo "dashboard is up: http://$HOST:${entry##*:} (${entry%%:*})"
    done
}

ingest() {
    [ -f data/build_records.py ] && python3 data/build_records.py
    python3 -m healthdash organize --apply
    HEALTHDASH_PI="$URL" python3 -m healthdash ingest
}

backup() {
    backup_one "$URL" backups
    for entry in $OTHERS; do
        backup_one "http://$HOST:${entry##*:}" "backups/${entry%%:*}"
    done
}

backup_one() {   # URL DIR
    local URL=$1 DIR=$2
    mkdir -p "$DIR" && chmod 700 "$DIR"
    local out; out="$DIR/health-$(date +%Y%m%d-%H%M).db"
    curl -fsS --max-time 120 -o "$out" "$URL/api/backup"
    chmod 600 "$out"
    python3 -c "import sqlite3,sys; sqlite3.connect(sys.argv[1]).execute('select count(*) from documents')" "$out"
    ls -1t "$DIR"/health-*.db | tail -n +$((KEEP + 1)) | while IFS= read -r old; do rm -- "$old"; done
    echo "backed up to $out ($(du -h "$out" | cut -f1))"
    # files added from the dashboard only exist on the server until copied here
    mkdir -p "$DIR/uploads" && chmod 700 "$DIR/uploads"
    python3 - "$URL" "$DIR/uploads" <<'EOF'
import json, os, re, sys, urllib.request
url, dest = sys.argv[1], sys.argv[2]
docs = json.load(urllib.request.urlopen(url + '/api/uploads', timeout=60))
new = 0
for d in docs:
    if not d.get('has_file'):
        continue
    name = re.sub(r'[^\w.\- ()&,]+', '_', d.get('orig_path') or 'upload')
    out = os.path.join(dest, f"{d['id'][:12]}_{name}")
    if os.path.exists(out):
        continue
    data = urllib.request.urlopen(f"{url}/file/{d['id']}?download=1", timeout=300).read()
    with open(out + '.tmp', 'wb') as f:
        f.write(data)
    os.chmod(out + '.tmp', 0o600)
    os.replace(out + '.tmp', out)
    new += 1
print(f'uploaded files: {len(docs)} on the dashboard, {new} newly copied to {dest}/')
EOF
}

case "${1:-}" in
    push) push ;;
    ingest) ingest ;;
    backup) backup ;;
    *) sed -n '2,12p' "$0"; exit 1 ;;
esac

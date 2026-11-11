#!/usr/bin/env bash
# One-time setup of the dashboard on the server (a Pi or any Linux box with systemd and python3).
# Run from the machine that holds the records; asks for the server's sudo password once.
# Afterwards use deploy/sync.sh push (code) and deploy/sync.sh ingest (records).
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
cd "$HERE"
[ -f deploy/local.env ] || { echo "copy deploy/local.env.example to deploy/local.env and fill it in"; exit 1; }
# shellcheck disable=SC1091
. deploy/local.env
: "${PI:?}" "${URL:?}" "${IMPORT_FROM:?}" "${TITLE:=Health Records}"

python3 -m unittest discover -s tests -t . -q
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
python3 - "$TMP/healthdash.service" "$IMPORT_FROM" "$TITLE" <<'EOF'
import sys
out, imp, title = sys.argv[1:]
s = open('deploy/healthdash.service').read()
title = title.replace('\\', '').replace('"', "'").replace('%', '%%')
open(out, 'w').write(s.replace('__IMPORT_FROM__', imp).replace('__TITLE__', title))
EOF
ssh "$PI" 'rm -rf /tmp/healthdash-install && mkdir -p /tmp/healthdash-install'
rsync -a --exclude __pycache__ --exclude '*.pyc' healthdash "$TMP/healthdash.service" "$PI:/tmp/healthdash-install/"
ssh -t "$PI" 'set -e
    id healthdash >/dev/null 2>&1 || sudo useradd --system --no-create-home --shell /usr/sbin/nologin healthdash
    sudo mkdir -p /opt/healthdash
    sudo chown "$USER": /opt/healthdash
    rm -rf /opt/healthdash/healthdash && mv /tmp/healthdash-install/healthdash /opt/healthdash/
    echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart healthdash" | sudo tee /etc/sudoers.d/healthdash-restart >/dev/null
    sudo chmod 440 /etc/sudoers.d/healthdash-restart
    sudo visudo -cf /etc/sudoers.d/healthdash-restart
    # tools for reading uploaded PDFs and scans
    if ! command -v pdftotext >/dev/null || ! command -v tesseract >/dev/null; then
        if command -v apt-get >/dev/null; then
            sudo apt-get update -qq && sudo apt-get install -y -qq poppler-utils tesseract-ocr
        else
            echo "install poppler-utils and tesseract-ocr with your package manager"
        fi
    fi
    PYTHONPATH=/opt/healthdash python3 -m healthdash check || echo "WARNING: see the check above"
    sudo install -m 644 /tmp/healthdash-install/healthdash.service /etc/systemd/system/healthdash.service
    rm -rf /tmp/healthdash-install
    sudo systemctl daemon-reload
    sudo systemctl enable --now healthdash
    sleep 2 && systemctl is-active healthdash'
curl -fsS --max-time 10 "$URL/api/summary" >/dev/null && echo "dashboard is up: $URL"
"$HERE/deploy/sync.sh" ingest

#!/usr/bin/env bash
# Add a separate dashboard for another person on the same server.
#
#   deploy/add-person.sh NAME PORT "Title shown on the page"
#   e.g. deploy/add-person.sh alex 9381 "Alex's Health"
#
# Each dashboard has its own service (healthdash-NAME), data folder (/var/lib/healthdash-NAME),
# sign-in accounts and backups; they share the program code in /opt/healthdash, so
# deploy/sync.sh push updates all of them. Asks for the server's sudo password once.
# Run deploy/install.sh first (it sets up the code and the first dashboard).
set -euo pipefail

NAME=${1:?usage: deploy/add-person.sh NAME PORT "Title"}
PORT=${2:?usage: deploy/add-person.sh NAME PORT "Title"}
TITLE=${3:-Health Records}
HERE=$(cd "$(dirname "$0")/.." && pwd)
cd "$HERE"
[ -f deploy/local.env ] || { echo "run deploy/install.sh first"; exit 1; }
# shellcheck disable=SC1091
. deploy/local.env
: "${PI:?}" "${URL:?}" "${IMPORT_FROM:?}"
OTHERS=${OTHERS:-}

[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || { echo "NAME: lowercase letters, numbers and - only"; exit 1; }
UNIT="healthdash-$NAME"
HOST=${URL#*://}; HOST=${HOST%%:*}; HOST=${HOST%%/*}
MAINPORT=${URL##*:}; MAINPORT=${MAINPORT%%/*}
for entry in "main:$MAINPORT" $OTHERS; do
    [ "${entry%%:*}" = "$NAME" ] && { echo "$NAME already has a dashboard"; exit 1; }
    [ "${entry##*:}" = "$PORT" ] && { echo "port $PORT is already used by ${entry%%:*}"; exit 1; }
done
if ssh "$PI" "ss -ltn | grep -q ':$PORT '"; then echo "port $PORT is already in use on the server"; exit 1; fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
python3 deploy/unit.py "$TMP/$UNIT.service" "$UNIT" "$PORT" "$IMPORT_FROM" "$TITLE"
scp -q "$TMP/$UNIT.service" "$PI:/tmp/$UNIT.service"
ssh -t "$PI" "set -e
    test -d /opt/healthdash/healthdash || { echo 'run deploy/install.sh first'; exit 1; }
    echo \"\$USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart $UNIT\" | sudo tee /etc/sudoers.d/$UNIT-restart >/dev/null
    sudo chmod 440 /etc/sudoers.d/$UNIT-restart
    sudo visudo -cf /etc/sudoers.d/$UNIT-restart
    sudo install -m 644 /tmp/$UNIT.service /etc/systemd/system/$UNIT.service
    rm -f /tmp/$UNIT.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now $UNIT
    sleep 2 && systemctl is-active $UNIT"

NEW="http://$HOST:$PORT"
curl -fsS --max-time 10 "$NEW/api/auth" >/dev/null && echo "dashboard is up: $NEW"
# remember it (deploy/local.env is git-ignored) so push and backup include it
if grep -q '^OTHERS=' deploy/local.env; then
    sed -i.bak "s/^OTHERS=\"\(.*\)\"/OTHERS=\"\1 $NAME:$PORT\"/; s/^OTHERS=\" /OTHERS=\"/" deploy/local.env && rm -f deploy/local.env.bak
else
    printf 'OTHERS="%s"   # other dashboards on the server: name:port\n' "$NAME:$PORT" >> deploy/local.env
fi
echo
echo "Next: open $NEW and create the first account (until then, whoever opens it first can)."
echo "For the Firefox plugin, create a key there and add it in the plugin's settings as another dashboard."

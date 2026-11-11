#!/usr/bin/env bash
# Package the Firefox plugin as dist/healthdash-capture-firefox.zip (upload it to
# addons.mozilla.org as "On your own" to get a signed .xpi, or load it for a session from
# about:debugging -> This Firefox -> Load Temporary Add-on).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/../dist"
mkdir -p "$OUT"
rm -f "$OUT/healthdash-capture-firefox.zip"
(cd "$HERE/firefox" && zip -q -r -X "$OUT/healthdash-capture-firefox.zip" manifest.json ./*.js ./*.html icon.svg icon-48.png icon-96.png)
echo "built $(cd "$OUT" && pwd)/healthdash-capture-firefox.zip"

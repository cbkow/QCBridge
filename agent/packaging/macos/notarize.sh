#!/bin/zsh
# notarize.sh — submit a .pkg (or the .app) to Apple's notary service with
# the keychain profile QCView's release flow already uses, wait, staple.
#
#   agent/packaging/macos/notarize.sh dist/QCBridge-Agent-0.2.1-arm64.pkg
#
# The profile holds an app-specific password (created once with
# `xcrun notarytool store-credentials QCView`); nothing secret is here.
set -euo pipefail
PROFILE="${QCB_NOTARY_PROFILE:-QCView}"
[ $# -eq 1 ] || { echo "usage: notarize.sh <file.pkg|App.app>" >&2; exit 2; }
TARGET="$1"
[ -e "$TARGET" ] || { echo "ERROR: not found: $TARGET" >&2; exit 1; }
xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1 \
    || { echo "ERROR: notarytool cannot use profile \"$PROFILE\"" >&2; exit 1; }
TMP="$(mktemp -d -t qcb-notary)"
trap 'rm -rf "$TMP"' EXIT
SUBMIT="$TARGET"
case "$TARGET" in
    *.app) SUBMIT="$TMP/app.zip"; ditto -c -k --keepParent "$TARGET" "$SUBMIT" ;;
esac
echo "[notary] submitting $(basename "$SUBMIT") with profile $PROFILE (Apple's queue: 2–15 min)"
OUT="$TMP/submit.json"
xcrun notarytool submit "$SUBMIT" --keychain-profile "$PROFILE" --wait --output-format json > "$OUT"
STATUS="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status",""))' "$OUT")"
ID="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("id",""))' "$OUT")"
if [ "$STATUS" != "Accepted" ]; then
    echo "ERROR: notarization $STATUS (id $ID); the log:" >&2
    xcrun notarytool log "$ID" --keychain-profile "$PROFILE" >&2 || true
    exit 1
fi
echo "[notary] accepted (id $ID); stapling"
xcrun stapler staple "$TARGET"
xcrun stapler validate "$TARGET"
echo "[notary] done: $TARGET"

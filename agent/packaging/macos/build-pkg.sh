#!/bin/zsh
# build-pkg.sh — the signed macOS installer for the agent.
#
#   dist/QCBridge-Agent-<version>-<arch>.pkg
#     └─ QCBridge-Agent-core.pkg (component, non-relocatable, preinstall)
#          └─ /Applications/QCBridge/
#               ├── QCBridge Agent.app
#               └── Uninstall QCBridge Agent.command
#
# Input: dist/QCBridge Agent.app from build-app.sh (Developer-ID signed,
# hardened runtime, timestamp). The pkg is signed with the Developer ID
# *Installer* identity — Installer refuses one signed with the app
# identity. Modelled on UFB's scripts/build-mac-pkg.sh. Then notarize.sh.
set -euo pipefail
AGENT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$AGENT/packaging/macos"
APP="$AGENT/dist/QCBridge Agent.app"
PKG_ID="com.qcbridge.agent"
[ -d "$APP" ] || { echo "ERROR: missing $APP — run build-app.sh first" >&2; exit 1; }
codesign --verify --strict "$APP" || { echo "ERROR: $APP is not validly signed" >&2; exit 1; }
VERSION="$(/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" "$APP/Contents/Info.plist")"
ARCH="$(uname -m)"
PKG="$AGENT/dist/QCBridge-Agent-$VERSION-$ARCH.pkg"

STAGE="$(mktemp -d -t qcb-pkg)"
trap 'rm -rf "$STAGE"' EXIT
ROOT="$STAGE/root/Applications/QCBridge"
mkdir -p "$ROOT" "$STAGE/pkgs" "$STAGE/scripts" "$STAGE/resources"
echo "[pkg] staging payload (v$VERSION, $ARCH)"
ditto "$APP" "$ROOT/QCBridge Agent.app"
cp "$SRC/uninstall.command" "$ROOT/Uninstall QCBridge Agent.command"
chmod 755 "$ROOT/Uninstall QCBridge Agent.command"
cp "$SRC/scripts/preinstall" "$STAGE/scripts/preinstall"
chmod 755 "$STAGE/scripts/preinstall"
cp "$SRC/welcome.txt" "$STAGE/resources/welcome.txt"

# Every bundle pkgbuild finds is pinned non-relocatable, so Installer can
# never redirect the payload onto a same-identifier bundle elsewhere (a
# dev copy under dist/, say).
pkgbuild --analyze --root "$STAGE/root" "$STAGE/components.plist" >/dev/null
i=0
while /usr/libexec/PlistBuddy -c "Print :$i" "$STAGE/components.plist" >/dev/null 2>&1; do
    /usr/libexec/PlistBuddy -c "Set :$i:BundleIsRelocatable false" "$STAGE/components.plist" 2>/dev/null \
        || /usr/libexec/PlistBuddy -c "Add :$i:BundleIsRelocatable bool false" "$STAGE/components.plist"
    i=$((i + 1))
done
echo "[pkg] $i bundle entries pinned non-relocatable"
pkgbuild --root "$STAGE/root" \
         --component-plist "$STAGE/components.plist" \
         --identifier "$PKG_ID" \
         --version "$VERSION" \
         --install-location / \
         --scripts "$STAGE/scripts" \
         "$STAGE/pkgs/QCBridge-Agent-core.pkg" >/dev/null
sed "s/@VERSION@/$VERSION/g" "$SRC/distribution.xml" > "$STAGE/distribution.xml"
rm -f "$PKG"
identity="$(security find-identity -v 2>/dev/null | grep -o '"Developer ID Installer: [^"]*"' | head -1 | tr -d '"')"
if [ -n "$identity" ]; then
    echo "[pkg] productbuild --sign \"$identity\""
    productbuild --distribution "$STAGE/distribution.xml" \
                 --package-path "$STAGE/pkgs" \
                 --resources "$STAGE/resources" \
                 --sign "$identity" --timestamp \
                 "$PKG" >/dev/null
    pkgutil --check-signature "$PKG" | sed 's/^/  /' | head -4
else
    echo "WARN: no Developer ID Installer identity — building an UNSIGNED pkg (local testing only)" >&2
    productbuild --distribution "$STAGE/distribution.xml" \
                 --package-path "$STAGE/pkgs" \
                 --resources "$STAGE/resources" \
                 "$PKG" >/dev/null
fi
echo "[pkg] done: $PKG ($(du -sh "$PKG" | cut -f1))"

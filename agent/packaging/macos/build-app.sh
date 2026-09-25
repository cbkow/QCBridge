#!/bin/zsh
# build-app.sh — build the agent and the capture helper and wrap them as
# dist/QCBridge Agent.app, Developer-ID signed with the hardened runtime
# and a secure timestamp (what notarization requires). The PKG script
# takes this bundle as its input.
#
#   agent/packaging/macos/build-app.sh            # release build + bundle + sign
#   QCB_SKIP_BUILD=1 agent/packaging/macos/build-app.sh   # bundle what target/release holds
set -euo pipefail
AGENT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$AGENT"
VERSION="$(grep -m1 '^version = ' Cargo.toml | sed -E 's/version = "(.*)"/\1/')"
if [ -z "${QCB_SKIP_BUILD:-}" ]; then
  cargo build --release
  swiftc -O capture-mac/qcb-capture-mac.swift -o target/release/qcb-capture-mac
fi
APP="$AGENT/dist/QCBridge Agent.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp target/release/qcbridge-agent "$APP/Contents/MacOS/qcbridge-agent"
cp target/release/qcb-capture-mac "$APP/Contents/MacOS/qcb-capture-mac"
cp assets/icons/qcbridge.icns "$APP/Contents/Resources/qcbridge.icns"
sed "s/@VERSION@/$VERSION/g" packaging/macos/Info.plist > "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

identity="$(security find-identity -v -p codesigning 2>/dev/null | grep -o '"Developer ID Application: [^"]*"' | head -1 | tr -d '"')"
if [ -z "$identity" ]; then
  echo "ERROR: no Developer ID Application identity on this Mac; the bundle must be signed to be notarized" >&2
  exit 1
fi
# Inside-out: the helper, then the agent, then the bundle. Hardened
# runtime + timestamp are what notarytool checks.
for bin in "$APP/Contents/MacOS/qcb-capture-mac" "$APP/Contents/MacOS/qcbridge-agent"; do
  codesign --force --sign "$identity" --options runtime --timestamp "$bin"
done
codesign --force --sign "$identity" --options runtime --timestamp "$APP"
codesign --verify --strict --deep "$APP"
echo "built: $APP (v$VERSION), signed with: $identity"

#!/bin/zsh
# Build the agent and the native macOS capture binary side by side.
set -eu
cd "${0:a:h}"
cargo build --release
swiftc -O capture-mac/qcb-capture-mac.swift -o target/release/qcb-capture-mac

# Sign with the Developer ID on this Mac when there is one. The token lives
# in the Keychain, and the Keychain identifies an app by its signature: an
# ad-hoc (linker-signed) binary changes identity on every build and is
# asked for permission every time; a Developer-ID signature with a fixed
# identifier is one app to the Keychain, so "Always Allow" sticks.
identity="$(security find-identity -v -p codesigning 2>/dev/null | grep -o '"Developer ID Application: [^"]*"' | head -1 | tr -d '"')"
if [ -n "$identity" ]; then
  for bin in target/release/qcbridge-agent target/release/qcb-capture-mac; do
    codesign --force --sign "$identity" --identifier "com.qcbridge.$(basename "$bin")" --timestamp=none "$bin"
  done
  echo "signed with: $identity"
else
  echo "no Developer ID on this Mac: ad-hoc signature (the Keychain will ask on each rebuild)"
fi
ls -la target/release/qcbridge-agent target/release/qcb-capture-mac

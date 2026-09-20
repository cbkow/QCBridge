#!/bin/zsh
# Build the agent and the native macOS capture binary side by side.
set -eu
cd "${0:a:h}"
cargo build --release
swiftc -O capture-mac/qcb-capture-mac.swift -o target/release/qcb-capture-mac
ls -la target/release/qcbridge-agent target/release/qcb-capture-mac

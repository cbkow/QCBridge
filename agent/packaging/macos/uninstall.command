#!/bin/bash
# Removes /Applications/QCBridge and the installer receipt. Settings, the
# token and the logs stay unless you say otherwise.
set -u
echo "QCBridge Agent uninstaller"
echo
echo "This removes /Applications/QCBridge (QCBridge Agent.app) and the"
echo "installer receipt. It will ask for your password."
echo
read -r -p "Continue? [y/N] " yn
case "$yn" in y|Y|yes|YES) ;; *) echo "Cancelled."; exit 0 ;; esac
pkill -x qcbridge-agent 2>/dev/null || true
sleep 1
sudo rm -rf "/Applications/QCBridge"
sudo pkgutil --forget com.qcbridge.agent >/dev/null 2>&1 || true
echo "Removed /Applications/QCBridge."
echo
echo "Your settings, certificate and logs are still at:"
echo "  ~/Library/Application Support/QCBridge"
echo "and the session token is in your login keychain (QCBridge Agent)."
read -r -p "Delete those too? [y/N] " yn2
case "$yn2" in
  y|Y|yes|YES)
    rm -rf "$HOME/Library/Application Support/QCBridge"
    security delete-generic-password -s "QCBridge Agent" >/dev/null 2>&1 || true
    echo "Deleted." ;;
  *) echo "Kept." ;;
esac

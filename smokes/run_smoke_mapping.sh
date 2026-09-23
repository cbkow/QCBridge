#!/bin/zsh
# Path-mapping smoke: host and replica name the same project root differently
# (the replica's root is a symlink to the host's, so files are shared and
# paths are not). Relative and mapped-absolute images must resolve and load
# on the replica; an image outside every mapping must be counted unmapped
# and reach the host's panel through the pong.
#   QCB_TRANSPORT=agent QCB_AGENT=spawn smokes/run_smoke_mapping.sh [work-dir]
set -u
HERE="${0:a:h}"
SCRATCH="${1:-$(mktemp -d /tmp/qcb-map.XXXXXX)}"
echo "work dir: $SCRATCH"
BL="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
rm -rf "$SCRATCH"/host.json "$SCRATCH"/replica.json "$SCRATCH/replicaside"
mkdir -p "$SCRATCH/bl_replica" "$SCRATCH/bl_host" "$SCRATCH/hostside/proj" "$SCRATCH/replicaside"
ln -s "$SCRATCH/hostside/proj" "$SCRATCH/replicaside/proj"

QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_replica" \
  "$BL" --factory-startup -noaudio --python "$HERE/smoke_mapping_replica.py" \
  -- "$SCRATCH" > "$SCRATCH/replica.log" 2>&1 &
REPLICA_PID=$!
sleep 4
QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_host" \
  "$BL" --factory-startup -noaudio --python "$HERE/smoke_mapping_host.py" \
  -- "$SCRATCH" > "$SCRATCH/host.log" 2>&1 &
HOST_PID=$!

python3 - "$SCRATCH" <<'EOF'
import json, os, sys, time
d = sys.argv[1]
def load(n):
    try: return json.load(open(f"{d}/{n}"))
    except Exception: return {}
t0 = time.time(); h = r = {}
while time.time() - t0 < 90:
    h, r = load("host.json"), load("replica.json")
    if h.get("done") and len(r.get("images", {})) == 3 and h.get("peer_status", {}).get("bootstraps"):
        break
    time.sleep(0.5)
time.sleep(2); h, r = load("host.json"), load("replica.json")
rr = r.get("replica_root", "")
im = r.get("images", {})
checks = {
    "relative_image_mapped_to_replica_root": im.get("RelTex", {}).get("filepath", "").startswith(rr) and im.get("RelTex", {}).get("loads") is True,
    # Same machine, same OS: the two-column table (win|mac) cannot express a
    # mac→mac remap, so the absolute host path is left as-is and loads only
    # because it is reachable here. The cross-OS translation of a host's
    # native absolute path is unit-tested (test_pathmap: localize_any).
    "absolute_image_loads_untranslated_same_os": im.get("AbsTex", {}).get("filepath", "").endswith("abs.png") and im.get("AbsTex", {}).get("loads") is True,
    "stray_image_left_alone": im.get("StrayTex", {}).get("filepath", "").endswith("stray.png") and not im.get("StrayTex", {}).get("filepath", "").startswith(rr),
    "unmapped_counted_and_on_host_panel": r.get("stats", {}).get("unmapped_paths", 0) >= 1 and h.get("peer_status", {}).get("unmapped", 0) >= 1,
    "replica_clean": r.get("stats", {}).get("apply_errors", 1) == 0 and r.get("stats", {}).get("gaps", 1) == 0,
}
print(json.dumps({"images": im, "unmapped": r.get("stats", {}).get("unmapped_paths"), "pong_unmapped": h.get("peer_status", {}).get("unmapped")}, indent=1))
for k, v in checks.items(): print(f"{k}:{'pass' if v else 'FAIL'}")
sys.exit(0 if all(checks.values()) else 1)
EOF
RESULT=$?
kill $HOST_PID $REPLICA_PID 2>/dev/null; sleep 2; kill -9 $HOST_PID $REPLICA_PID 2>/dev/null
exit $RESULT

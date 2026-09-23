#!/bin/zsh
# Cache smoke: a shared cache root makes a host bake a replica bake with no
# Force Resync — the point cache is externalized before baking, the path
# crosses in the settings resend, and the replica rescans the same frames.
#   QCB_TRANSPORT=agent QCB_AGENT=spawn smokes/run_smoke_cache.sh [work-dir]
set -u
HERE="${0:a:h}"
SCRATCH="${1:-$(mktemp -d /tmp/qcb-cache.XXXXXX)}"
echo "work dir: $SCRATCH"
BL="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
rm -f "$SCRATCH"/host.json "$SCRATCH"/replica.json
mkdir -p "$SCRATCH/bl_replica" "$SCRATCH/bl_host"

QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_replica" \
  "$BL" --factory-startup -noaudio --python "$HERE/smoke_cache_replica.py" \
  -- "$SCRATCH" > "$SCRATCH/replica.log" 2>&1 &
REPLICA_PID=$!
sleep 4
QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_host" \
  "$BL" --factory-startup -noaudio --python "$HERE/smoke_cache_host.py" \
  -- "$SCRATCH" > "$SCRATCH/host.log" 2>&1 &
HOST_PID=$!

python3 - "$SCRATCH" <<'EOF'
import json, os, sys, time
d = sys.argv[1]
def load(n):
    try: return json.load(open(f"{d}/{n}"))
    except Exception: return {}
t0 = time.time(); h = r = {}
while time.time() - t0 < 120:
    h, r = load("host.json"), load("replica.json")
    if h.get("done") and r.get("frame") == 20 and r.get("cloth_baked") and "mean_z" in r:
        break
    time.sleep(0.5)
time.sleep(2); h, r = load("host.json"), load("replica.json")
checks = {
    "externalized_before_bake": bool(h.get("externalized_before_bake")),
    "host_baked_to_root": bool(h.get("host_baked")) and h.get("host_cache_files", 0) > 0,
    "replica_external_same_path": r.get("cloth_external") is True and r.get("cloth_filepath") == h.get("host_filepath"),
    "replica_reads_bake_no_resync": (r.get("cloth_baked") is True and r.get("frame") == 20
        and abs((r.get("mean_z") or 0) - (h.get("host_mean_z_f20") or 1)) < 1e-3
        and r.get("stats", {}).get("bootstraps") == 1 and h.get("sent_boot") == 1),
    "replica_clean": r.get("stats", {}).get("gaps", 1) == 0 and r.get("stats", {}).get("apply_errors", 1) == 0
        and r.get("stats", {}).get("frozen_caches", 1) == 0,
}
print(json.dumps({"host": {k: h.get(k) for k in ("host_baked", "host_mean_z_f20", "host_cache_files", "cache_note", "externalized")},
                  "replica": {k: r.get(k) for k in ("frame", "cloth_baked", "cloth_external", "cloth_info", "mean_z")}}, indent=1))
for k, v in checks.items(): print(f"{k}:{'pass' if v else 'FAIL'}")
sys.exit(0 if all(checks.values()) else 1)
EOF
RESULT=$?
kill $HOST_PID $REPLICA_PID 2>/dev/null; sleep 2; kill -9 $HOST_PID $REPLICA_PID 2>/dev/null
exit $RESULT

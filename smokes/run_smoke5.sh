#!/bin/zsh
# Production-file validation: host opens the real project (read-only usage;
# never saved), replica runs the field config (kiosk on, starts camera-less).
set -u
HERE="${0:a:h}"
# Work dir: never the script dir, which is now in the repo.
SCRATCH="${1:-$(mktemp -d /tmp/qcb-smoke.XXXXXX)}"
echo "work dir: $SCRATCH"
BL="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
FILE="${QCB_SMOKE_FILE:?set QCB_SMOKE_FILE to the .blend to validate}"
rm -f "$SCRATCH"/host.json "$SCRATCH"/replica.json
mkdir -p "$SCRATCH/bl_replica" "$SCRATCH/bl_host"

QCB_DEBUG=1 QCB_SMOKE_KIOSK=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_replica" \
  "$BL" --factory-startup -noaudio --python "$HERE/smoke_replica.py" \
  -- "$SCRATCH" > "$SCRATCH/replica5.log" 2>&1 &
REPLICA_PID=$!
sleep 4

QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_host" \
  "$BL" --factory-startup -noaudio "$FILE" --python "$HERE/smoke5_host.py" \
  -- "$SCRATCH" > "$SCRATCH/host5.log" 2>&1 &
HOST_PID=$!

for i in $(seq 1 300); do
  sleep 1
  if python3 -c "import json,sys; d=json.load(open('$SCRATCH/host.json')); sys.exit(0 if d.get('done') else 1)" 2>/dev/null; then
    sleep 3
    break
  fi
done

python3 - "$SCRATCH" <<'EOF'
import json, sys
S = sys.argv[1]
h = json.load(open(S + "/host.json"))
r = json.load(open(S + "/replica.json"))
checks = {
    "host_done": bool(h.get("done")),
    "bootstrapped": r["stats"]["bootstraps"] >= 1,
    "startup_storm_free": h.get("t2_baseline") == 0,
    "object_count_parity": r.get("n_objects") == h.get("n_objects"),
    "scene_camera_matches": r.get("scenecam_name") == h.get("scenecam_name"),
    "camera_parent_matches": r.get("scenecam_parent") == h.get("scenecam_parent"),
    "replica_in_camera_view": r.get("view_persp") == "CAMERA",
    "camera_view_BOUND": bool(r.get("cam_bound")),
    "nudge_reverted_match": (r.get("scenecam_matrix") == h.get("scenecam_m_final")),
    "host_sync_errors_zero": h.get("sync_errors") == 0,
    "replica_clean": (r["stats"]["gaps"] == 0
                      and r["stats"]["apply_errors"] == 0
                      and r["stats"]["unknown_uuid"] == 0),
}
print(json.dumps({"pass": all(checks.values()), "checks": checks,
                  "file_facts": {k: h.get(k) for k in
                                 ("file", "n_objects", "n_meshes", "n_actions",
                                  "scenecam_name", "scenecam_parent",
                                  "scenecam_parent_type", "prestamped",
                                  "t2_final", "t2_unsupported")},
                  "persp_log": r.get("persp_log"),
                  "replica_stats": r.get("stats"),
                  "last_error": r.get("last_error")}, indent=1))
sys.exit(0 if all(checks.values()) else 1)
EOF
RESULT=$?
echo "--- boot size ---"; grep -o "boot queued.*" "$SCRATCH/host5.log" | head -2
echo "--- t2 sends ---"; grep "qcb t2 send" "$SCRATCH/host5.log" | sort | uniq -c | sort -rn | head -10

kill $HOST_PID $REPLICA_PID 2>/dev/null
sleep 2
kill -9 $HOST_PID $REPLICA_PID 2>/dev/null
exit $RESULT

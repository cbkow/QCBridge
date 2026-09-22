#!/bin/zsh
# Field-setup repro: kiosk replica (camera-less at start) + host in camera
# view through a spline-parented camera. Pass = replica reaches BOUND camera
# view with zero manual input and follows the rail orbit.
set -u
HERE="${0:a:h}"
# Work dir: never the script dir, which is now in the repo.
SCRATCH="${1:-$(mktemp -d /tmp/qcb-smoke.XXXXXX)}"
echo "work dir: $SCRATCH"
BL="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
rm -f "$SCRATCH"/host.json "$SCRATCH"/replica.json
mkdir -p "$SCRATCH/bl_replica" "$SCRATCH/bl_host"

QCB_DEBUG=1 QCB_SMOKE_KIOSK=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_replica" \
  "$BL" --factory-startup -noaudio --python "$HERE/smoke_replica.py" \
  -- "$SCRATCH" > "$SCRATCH/replica4.log" 2>&1 &
REPLICA_PID=$!
sleep 4

QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_host" \
  "$BL" --factory-startup -noaudio --python "$HERE/smoke4_host.py" \
  -- "$SCRATCH" > "$SCRATCH/host4.log" 2>&1 &
HOST_PID=$!

for i in $(seq 1 240); do
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
    "replica_in_camera_view": r.get("view_persp") == "CAMERA",
    "camera_view_BOUND": bool(r.get("cam_bound")),
    "follows_rail_orbit": r.get("shotcam_matrix") == h.get("shotcam_m2"),
    "replica_clean": (r["stats"]["gaps"] == 0
                      and r["stats"]["apply_errors"] == 0
                      and r["stats"]["unknown_uuid"] == 0),
}
print(json.dumps({"pass": all(checks.values()), "checks": checks,
                  "persp_log": r.get("persp_log"),
                  "stats": r.get("stats")}, indent=1))
sys.exit(0 if all(checks.values()) else 1)
EOF
RESULT=$?

kill $HOST_PID $REPLICA_PID 2>/dev/null
sleep 2
kill -9 $HOST_PID $REPLICA_PID 2>/dev/null
exit $RESULT

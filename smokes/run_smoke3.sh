#!/bin/zsh
# Regression repro: scaled-spline camera rig. Runs the pair, waits for host
# done, dumps both JSONs + t2-send summary from the host log.
set -u
HERE="${0:a:h}"
# Work dir: never the script dir, which is now in the repo.
SCRATCH="${1:-$(mktemp -d /tmp/qcb-smoke.XXXXXX)}"
echo "work dir: $SCRATCH"
BL="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
rm -f "$SCRATCH"/host.json "$SCRATCH"/replica.json
mkdir -p "$SCRATCH/bl_replica" "$SCRATCH/bl_host"

QCB_DEBUG=1 QCB_SMOKE_DISTURB=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_replica" \
  "$BL" --factory-startup -noaudio --python "$HERE/smoke_replica.py" \
  -- "$SCRATCH" > "$SCRATCH/replica3.log" 2>&1 &
REPLICA_PID=$!
sleep 4

QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_host" \
  "$BL" --factory-startup -noaudio --python "$HERE/smoke3_host.py" \
  -- "$SCRATCH" > "$SCRATCH/host3.log" 2>&1 &
HOST_PID=$!

for i in $(seq 1 240); do
  sleep 1
  if python3 -c "import json,sys; d=json.load(open('$SCRATCH/host.json')); sys.exit(0 if d.get('done') else 1)" 2>/dev/null; then
    sleep 2
    break
  fi
done

echo "=== HOST RESULTS ==="; cat "$SCRATCH/host.json"
echo; echo "=== REPLICA FINAL ==="; cat "$SCRATCH/replica.json"
echo; echo "=== HOST T2 SENDS ==="; grep "qcb t2 send" "$SCRATCH/host3.log" | sort | uniq -c | sort -rn
echo "=== REPLICA VIEW EVENTS ==="; grep -cE "applying" "$SCRATCH/replica3.log" || true

kill $HOST_PID $REPLICA_PID 2>/dev/null
sleep 2
kill -9 $HOST_PID $REPLICA_PID 2>/dev/null
exit 0

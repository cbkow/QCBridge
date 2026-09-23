#!/bin/zsh
# Edit→visible latency, host Blender to replica Blender, per lane/tier.
#   smokes/bench_latency.sh zmq|agent [work-dir]
# agent mode spawns the sidecar (needs agent/ built --release); zmq mode needs
# pyzmq unzipped into <work-dir>/pysite (see README.md).
set -u
HERE="${0:a:h}"
MODE="${1:-agent}"
SCRATCH="${2:-$(mktemp -d /tmp/qcb-lat.XXXXXX)}"
echo "work dir: $SCRATCH  mode: $MODE"
BL="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
rm -f "$SCRATCH"/host_lat.json "$SCRATCH"/replica_lat.json "$SCRATCH"/latency.json
mkdir -p "$SCRATCH/bl_replica" "$SCRATCH/bl_host"
if [[ "$MODE" == "agent" ]]; then
  export QCB_TRANSPORT=agent QCB_AGENT=spawn
else
  unset QCB_TRANSPORT QCB_AGENT
fi

BLENDER_USER_RESOURCES="$SCRATCH/bl_replica" \
  "$BL" --factory-startup -noaudio --python "$HERE/bench_latency_replica.py" \
  -- "$SCRATCH" > "$SCRATCH/replica.log" 2>&1 &
REPLICA_PID=$!
sleep 4
BLENDER_USER_RESOURCES="$SCRATCH/bl_host" \
  "$BL" --factory-startup -noaudio --python "$HERE/bench_latency_host.py" \
  -- "$SCRATCH" > "$SCRATCH/host.log" 2>&1 &
HOST_PID=$!

START=$(date +%s)
while true; do
  if [[ -f "$SCRATCH/host_lat.json" ]] && grep -q '"done": true' "$SCRATCH/host_lat.json"; then break; fi
  if (( $(date +%s) - START > 300 )); then echo "TIMEOUT"; break; fi
  if ! kill -0 $HOST_PID 2>/dev/null; then echo "host exited early"; break; fi
  sleep 1
done
sleep 4  # let the last phase drain and the replica dump once more
python3 "$HERE/bench_latency_report.py" "$SCRATCH"
RESULT=$?
kill $HOST_PID $REPLICA_PID 2>/dev/null
sleep 2
kill -9 $HOST_PID $REPLICA_PID 2>/dev/null
exit $RESULT

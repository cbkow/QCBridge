#!/bin/zsh
# Two-instance smoke: replica listens, host drives the scenario, poller judges.
set -u
HERE="${0:a:h}"
# Work dir: never the script dir, which is now in the repo.
SCRATCH="${1:-$(mktemp -d /tmp/qcb-smoke.XXXXXX)}"
echo "work dir: $SCRATCH"
BL="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
rm -f "$SCRATCH"/host.json "$SCRATCH"/replica.json "$SCRATCH"/verdict.json
mkdir -p "$SCRATCH/bl_replica" "$SCRATCH/bl_host"

QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_replica" \
  "$BL" --factory-startup -noaudio --python "$HERE/smoke_replica.py" \
  -- "$SCRATCH" > "$SCRATCH/replica.log" 2>&1 &
REPLICA_PID=$!
sleep 4

QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_host" \
  "$BL" --factory-startup -noaudio --python "$HERE/smoke_host.py" \
  -- "$SCRATCH" > "$SCRATCH/host.log" 2>&1 &
HOST_PID=$!

python3 "$HERE/smoke_poller.py" "$SCRATCH"
RESULT=$?

kill $HOST_PID $REPLICA_PID 2>/dev/null
sleep 2
kill -9 $HOST_PID $REPLICA_PID 2>/dev/null
exit $RESULT

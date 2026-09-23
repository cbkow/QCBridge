#!/bin/zsh
# Coverage survey: which user actions reach the replica, and how fast.
#   smokes/coverage/run_coverage.sh [work-dir]
# Env: QCB_TRANSPORT=agent QCB_AGENT=spawn for QUIC (default zmq, needs
# pysite); QCB_COV_ONLY=key1,key2 to run a subset; QCB_COV_SETTLE seconds.
set -u
HERE="${0:a:h}"
SCRATCH="${1:-$(mktemp -d /tmp/qcb-cov.XXXXXX)}"
echo "work dir: $SCRATCH"
BL="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
rm -f "$SCRATCH"/host.json "$SCRATCH"/replica.json "$SCRATCH"/coverage.json
mkdir -p "$SCRATCH/bl_replica" "$SCRATCH/bl_host"

QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_replica" \
  "$BL" --factory-startup -noaudio --python "$HERE/cov_replica.py" \
  -- "$SCRATCH" > "$SCRATCH/replica.log" 2>&1 &
REPLICA_PID=$!
sleep 4
QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_host" \
  "$BL" --factory-startup -noaudio --python "$HERE/cov_host.py" \
  -- "$SCRATCH" > "$SCRATCH/host.log" 2>&1 &
HOST_PID=$!

python3 "$HERE/cov_poller.py" "$SCRATCH"
RESULT=$?
kill $HOST_PID $REPLICA_PID 2>/dev/null
sleep 2
kill -9 $HOST_PID $REPLICA_PID 2>/dev/null
exit $RESULT

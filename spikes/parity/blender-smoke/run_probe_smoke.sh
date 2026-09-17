#!/bin/zsh
# Probe smoke (macOS): replica window left, host window right, same machine.
# Replica streams SRT on 127.0.0.1:19998 (latency 60, token smoketok).
# Usage: run_probe_smoke.sh <work-dir>   (work-dir gets pysite/, logs, json)
# Env: QCB_HOT_HZ (60), QCB_CAPTURE_FPS (60), QCB_PROBE_ORBIT (45), BLENDER.
set -u
HERE="${0:a:h}"
W="${1:?work dir}"
BL="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
REPO="${HERE:h:h:h}"
mkdir -p "$W/pysite" "$W/bl_host" "$W/bl_replica"
[[ -d "$W/pysite/zmq" ]] || unzip -q -o "$REPO"/qcbridge/wheels/pyzmq-*-cp313-*macosx*.whl -d "$W/pysite"
rm -f "$W"/host.json "$W"/replica.json
QCB_PROBE=1 QCB_CAPTURE_FPS=${QCB_CAPTURE_FPS:-60} BLENDER_USER_RESOURCES="$W/bl_replica" \
  "$BL" --factory-startup -noaudio -p 0 0 1400 900 --python "$HERE/probe_replica.py" -- "$W" \
  > "$W/replica.log" 2>&1 &
echo $! > "$W/replica.pid"
sleep 5
QCB_PROBE=1 QCB_HOT_HZ=${QCB_HOT_HZ:-60} QCB_PROBE_ORBIT=${QCB_PROBE_ORBIT:-45} \
  BLENDER_USER_RESOURCES="$W/bl_host" \
  "$BL" --factory-startup -noaudio -p 1450 0 1000 700 --python "$HERE/probe_host.py" -- "$W" \
  > "$W/host.log" 2>&1 &
echo $! > "$W/host.pid"
echo "started; stop with: kill \$(cat $W/host.pid) \$(cat $W/replica.pid)"

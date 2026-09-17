#!/bin/zsh
# Bootstrap bench (macOS, one machine): time from host session start to the
# replica reporting its first bootstrap applied. Usage:
#   bootstrap_bench.sh <work-dir> <zmq|kyber> <million-verts>
set -u
HERE="${0:a:h}"; W="${1:?work dir}"; KIND="${2:?zmq|kyber}"; HEAVY="${3:-4}"
BL="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
REPO="${HERE:h:h:h}"
mkdir -p "$W/pysite" "$W/bl_host" "$W/bl_replica"
[[ -d "$W/pysite/zmq" ]] || unzip -q -o "$REPO"/qcbridge/wheels/pyzmq-*-cp313-*macosx*.whl -d "$W/pysite"
rm -f "$W"/host.json "$W"/replica.json
export QCB_TRANSPORT=$KIND QCB_SMOKE_STREAM=0 QCB_SMOKE_DUMP=0.1
BLENDER_USER_RESOURCES="$W/bl_replica" "$BL" --factory-startup -noaudio -p 0 0 900 600 \
  --python "$HERE/probe_replica.py" -- "$W" > "$W/replica.log" 2>&1 &
RP=$!
sleep 5
QCB_SMOKE_HEAVY=$HEAVY BLENDER_USER_RESOURCES="$W/bl_host" "$BL" --factory-startup -noaudio \
  -p 950 0 900 600 --python "$HERE/probe_host.py" -- "$W" > "$W/host.log" 2>&1 &
HP=$!
python3 - "$W" "$KIND" "$HEAVY" <<'PY'
import json, sys, time
w, kind, heavy = sys.argv[1:4]
deadline = time.time() + 180
t_start = None
while time.time() < deadline:
    try:
        h = json.load(open(f"{w}/host.json")); r = json.load(open(f"{w}/replica.json"))
    except (OSError, ValueError):
        time.sleep(0.05); continue
    t_start = h.get("t_start")
    if r["stats"]["bootstraps"] >= 1 and t_start:
        print(f"{kind} heavy={heavy}M: bootstrap applied {r['t'] - t_start:.2f} s after host session start "
              f"(errors={r['stats']['apply_errors']})")
        break
    time.sleep(0.05)
else:
    print(f"{kind}: TIMEOUT")
PY
kill $HP $RP 2>/dev/null; sleep 2; kill -9 $HP $RP 2>/dev/null

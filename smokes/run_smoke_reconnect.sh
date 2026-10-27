#!/bin/zsh
# Reconnect smoke: the replica dies and comes back mid-session; the host must
# re-handshake and re-bootstrap on its own, then repair a dropped fast-lane
# frame with a re-assert instead of a bootstrap. Agent transport only (the zmq path is frozen).
#   QCB_TRANSPORT=agent QCB_AGENT=spawn smokes/run_smoke_reconnect.sh [work-dir]
set -u
HERE="${0:a:h}"
SCRATCH="${1:-$(mktemp -d /tmp/qcb-reconnect.XXXXXX)}"
echo "work dir: $SCRATCH"
BL="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
rm -f "$SCRATCH"/host.json "$SCRATCH"/replica.json "$SCRATCH"/verdict.json
mkdir -p "$SCRATCH/bl_replica" "$SCRATCH/bl_host"

start_replica() {  # $1 = tag, rest = extra env
  # ${@:2} unquoted: an empty extra-env list must not become an empty word
  env QCB_DEBUG=1 QCB_SMOKE_TAG="$1" ${@:2} BLENDER_USER_RESOURCES="$SCRATCH/bl_replica" \
    "$BL" --factory-startup -noaudio --python "$HERE/smoke_reconnect_replica.py" \
    -- "$SCRATCH" > "$SCRATCH/replica-$1.log" 2>&1 &
  echo $!
}
wait_for() {  # $1 = python expr over host (h) and replica (r) dicts, $2 = timeout s
  local t0=$(date +%s)
  while true; do
    if python3 - "$SCRATCH" "$1" <<'EOF' 2>/dev/null
import json, sys
d = sys.argv[1]
def load(n):
    try: return json.load(open(f"{d}/{n}"))
    except Exception: return {}
h, r = load("host.json"), load("replica.json")
sys.exit(0 if eval(sys.argv[2]) else 1)
EOF
    then return 0; fi
    if (( $(date +%s) - t0 > $2 )); then return 1; fi
    sleep 0.5
  done
}

R1=$(start_replica a)
sleep 4
QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_host" \
  "$BL" --factory-startup -noaudio --python "$HERE/smoke_reconnect_host.py" \
  -- "$SCRATCH" > "$SCRATCH/host.log" 2>&1 &
HOST_PID=$!

checks=()
if wait_for 'r.get("tag")=="a" and r.get("stats",{}).get("bootstraps",0)>=1 and r.get("probe") is not None' 60; then
  checks+=("first_boot:pass"); else checks+=("first_boot:FAIL"); fi

echo "killing replica a"
kill -9 $R1 2>/dev/null; sleep 3
rm -f "$SCRATCH/replica.json"
R2=$(start_replica b QCB_TEST_DROP_FIRST_T1=1 QCB_TEST_DROP_FIRST_T2=1 QCB_SMOKE_LOCAL_EDIT=1)

if wait_for 'r.get("tag")=="b" and r.get("stats",{}).get("bootstraps",0)>=1 and r.get("probe") is not None and h.get("sent_boot",0)>=2' 60; then
  checks+=("rebootstrap_after_restart:pass"); else checks+=("rebootstrap_after_restart:FAIL"); fi

# the host now edits x then y; the replica drops the first t1 → fast-lane hole →
# want_resync with gap_lane=fast → the host re-asserts the cube from the hole on
# (a full tier-1 snapshot), NO bootstrap (DESIGN-NOTES sync fast-ack)
if wait_for 'r.get("stats",{}).get("bootstraps",0)==1 and r.get("stats",{}).get("resyncs",0)>=1 and r.get("probe")==[7.0,3.0,0.0]' 60; then
  checks+=("fast_reassert_after_gap:pass"); else checks+=("fast_reassert_after_gap:FAIL"); fi
if wait_for 'h.get("fast_resyncs",0)>=1 and h.get("auto_resyncs",0)==0' 5; then checks+=("host_counted_fast_resync:pass"); else checks+=("host_counted_fast_resync:FAIL"); fi
if wait_for 'not r.get("stats",{}).get("want_resync") and r.get("stats",{}).get("gaps",0)>=1' 10; then
  checks+=("flag_cleared_gap_kept:pass"); else checks+=("flag_cleared_gap_kept:FAIL"); fi
# the host then adds two modifiers; the replica drops the first blob → cold
# hole → want_resync → the host answers with a DELTA (the cube's blob again),
# no bootstrap, and the replica ends with both modifiers
if wait_for 'h.get("done") and r.get("stats",{}).get("bootstraps",0)==1 and h.get("delta_resyncs",0)>=1 and h.get("delta_t2",0)>=1' 60; then
  checks+=("delta_resync_no_boot:pass"); else checks+=("delta_resync_no_boot:FAIL"); fi
if wait_for 'r.get("probe_mods")==["Disp","Disp2"] and not r.get("stats",{}).get("want_resync")' 30; then
  checks+=("delta_modifier_present:pass"); else checks+=("delta_modifier_present:FAIL"); fi
# replica b edits itself 4 s after the re-assert; the host's pong must carry it
if wait_for 'h.get("peer_status",{}).get("local_edits",0)>=1 and "Probe" in h.get("peer_status",{}).get("last_local_edit","")' 30; then
  checks+=("local_edit_reported_to_host:pass"); else checks+=("local_edit_reported_to_host:FAIL"); fi

kill $HOST_PID $R2 2>/dev/null; sleep 2; kill -9 $HOST_PID $R2 2>/dev/null
echo "--- host notes:"; python3 -c "import json;print(json.load(open('$SCRATCH/host.json'))['notes'])" 2>/dev/null
echo "--- replica b:"; python3 -c "import json;d=json.load(open('$SCRATCH/replica.json'));print(d['stats'], d['probe'], d['overlay'])" 2>/dev/null
FAIL=0
for c in $checks; do echo "$c"; [[ "$c" == *FAIL ]] && FAIL=1; done
exit $FAIL

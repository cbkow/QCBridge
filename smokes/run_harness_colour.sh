#!/bin/zsh
# Colour harness (2026-10-27, DESIGN-NOTES pixels colour state): the still
# harness scene streamed through the real pixel path and compared, stage by
# stage, with Blender's own renders of the same scene — the wire against the
# scene-linear render, QCView's picture against the host's view for the
# host's display device. Needs: the helper built, QCView built
# (QCV_APP = its binary; QCView's ffmpeg is used for SRT), Blender.
#   QCB_TRANSPORT=agent QCB_AGENT=spawn smokes/run_harness_colour.sh [work-dir]
#   QCB_HARNESS_DISPLAY=Rec.2020 QCB_HARNESS_VIEW=Standard … for other host settings
#   OCIO=<config> exported in the shell puts BOTH Blenders on that config.
#   QCB_HARNESS_REPLICA=<address> runs against a replica already up elsewhere
#   (then no replica is started here; the box's own agent must be listening).
set -u
HERE="${0:a:h}"
SCRATCH="${1:-$(mktemp -d /tmp/qcb-harness.XXXXXX)}"
echo "work dir: $SCRATCH"
BL="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
QCV_APP="${QCV_APP:-$HOME/Documents/GitHub/QCView-Player/build/src/app/qcview.app/Contents/MacOS/qcview}"
FF="${QCB_HARNESS_FFMPEG:-$(dirname "$QCV_APP")/../Helpers/ffmpeg}"
export QCB_HARNESS_FFMPEG="$FF"
PYNUMPY="${BLENDER_PYTHON:-$(ls -d /Applications/Blender.app/Contents/Resources/*/python/bin/python3* 2>/dev/null | head -1)}"
rm -f "$SCRATCH"/host.json "$SCRATCH"/replica.json "$SCRATCH"/harness.json "$SCRATCH"/wire.yuv "$SCRATCH"/qcv_capture.png
mkdir -p "$SCRATCH/bl_replica" "$SCRATCH/bl_host" "$SCRATCH/qcv-settings"
pkill -f 'srt://127.0.0.1:19998\?mode=listener' 2>/dev/null
pkill -f 'qcb-encode-(mac|win)(\.exe)? --shm qcb-' 2>/dev/null

# 1. the reference renders, in the host's colour settings and config
QCB_DEBUG=0 "$BL" -b --factory-startup --python "$HERE/harness_colour_ref.py" -- "$SCRATCH" > "$SCRATCH/ref.log" 2>&1
grep -q 'REF wrote' "$SCRATCH/ref.log" || { echo "reference render failed (see $SCRATCH/ref.log)"; exit 2; }

# 2. the rig
REPLICA_PID=""
if [ -z "${QCB_HARNESS_REPLICA:-}" ]; then
  QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_replica" \
    "$BL" --factory-startup -noaudio --window-geometry 0 0 1600 1000 --python "$HERE/smoke_pixels_replica.py" \
    -- "$SCRATCH" > "$SCRATCH/replica.log" 2>&1 &
  REPLICA_PID=$!
  sleep 4
fi
QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_host" \
  "$BL" --factory-startup -noaudio --window-geometry 0 0 1200 800 --python "$HERE/harness_colour_host.py" \
  -- "$SCRATCH" > "$SCRATCH/host.log" 2>&1 &
HOST_PID=$!

python3 - "$SCRATCH" <<'EOF'
import json, sys, time
S = sys.argv[1]
def load(n):
    try: return json.load(open(f"{S}/{n}"))
    except Exception: return {}
import os
remote = bool(os.environ.get("QCB_HARNESS_REPLICA"))
def replica_view():
    """What the replica reports: its own dump on one machine, the pong
    (host.json peer_status) when it runs elsewhere."""
    if remote:
        return (load("host.json").get("peer_status") or {})
    return load("replica.json")
t0 = time.time()
while time.time() - t0 < 180:
    h, r = load("host.json"), replica_view()
    if (h.get("deep_link") and h.get("shot") and "streaming" in (r.get("pixel") or "")
            and (r.get("readback") or {}).get("published", 0) >= 5):   # a STILL scene: the hash skip keeps this low
        break
    time.sleep(0.5)
else:
    print("rig did not come up:", load("host.json").get("note"), (replica_view().get("pixel") or "?")); sys.exit(2)
# let the viewport converge and denoise (a still scene: publishes stop when it has)
last, same = -1, 0
t1 = time.time()
while time.time() - t1 < 90 and same < 3:
    n = (replica_view().get("readback") or {}).get("published", 0)
    same = same + 1 if n == last else 0
    last = n
    time.sleep(1.0)
h = load("host.json")
open(f"{S}/link.txt", "w").write(h["deep_link"])
import urllib.parse
q = urllib.parse.parse_qs(urllib.parse.urlsplit(h["deep_link"]).query)
open(f"{S}/url.txt", "w").write(q["url"][0])
print("rig up:", h.get("display"), h.get("view"), "| mismatches:", h.get("cm_mismatches"))
EOF
RC=$?
if [ $RC -ne 0 ]; then kill $HOST_PID $REPLICA_PID 2>/dev/null; exit 2; fi

# 3. one frame off the wire (the listener takes one viewer at a time: before QCView)
URL="$(cat "$SCRATCH/url.txt")"
# The listener relistens between viewers (and after its idle timeout), so a
# caller can land in a gap: try a few times, 20 s each (an idle stream is
# ~0.3 Mbps, the probe alone takes ~8 s).
for attempt in 1 2 3 4 5; do
  "$FF" -hide_banner -loglevel error -y -probesize 300000 -analyzeduration 1000000 -i "$URL" \
    -frames:v 1 -pix_fmt yuv420p10le -f rawvideo "$SCRATCH/wire.yuv" > "$SCRATCH/wiregrab.log" 2>&1 &
  G=$!
  for i in $(seq 1 20); do sleep 1; kill -0 $G 2>/dev/null || break; done
  kill -0 $G 2>/dev/null && kill $G
  sleep 2
  if [ -s "$SCRATCH/wire.yuv" ]; then echo "wire frame on attempt $attempt"; break; fi
  sleep 3
done

# 4. QCView through the link: the capture is the host's view for the host's
#    display, as shown (captureScreenshot runs the SDR capture chain).
LINK="$(cat "$SCRATCH/link.txt")"
sleep 5   # the listener relistens after the grab's viewer left
# QCView runs on its bundled configs whatever the shell's OCIO says (the
# Blenders keep it): the decision of 2026-10-27. The capture waits for the
# decoder to be live; the connect itself can take a retry or two.
env -u OCIO QCV_SETTINGS_DIR="$SCRATCH/qcv-settings" QCV_CAPTURE_PNG="$SCRATCH/qcv_capture.png@40" \
  QCV_QML_EVAL='38:("engine " + WindowManager.ocio.engine + " engaged " + WindowManager.ocio.engaged + " host " + JSON.stringify(WindowManager.ocio.clipHostDisplay(WindowManager.project.activeItemId)))' \
  "$QCV_APP" "$LINK" > "$SCRATCH/qcview.log" 2>&1 &
Q=$!
sleep 46
kill $Q 2>/dev/null
grep -E 'QCV_QML_EVAL|QCV_CAPTURE|openProjectLink: (host display|stream colour)|LiveStreamDecoder: LIVE' "$SCRATCH/qcview.log" | cut -c1-200

# 5. compare
"$PYNUMPY" "$HERE/harness_colour.py" "$SCRATCH"
RESULT=$?
kill $HOST_PID $REPLICA_PID 2>/dev/null; sleep 3; kill -9 $HOST_PID $REPLICA_PID 2>/dev/null
pkill -f 'srt://127.0.0.1:19998\?mode=listener' 2>/dev/null
exit $RESULT

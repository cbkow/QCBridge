#!/bin/zsh
# Pixel smoke (0.3, DESIGN-NOTES pixels): the replica streams through the
# readback path — Blender's engine subclass reads the viewport into the
# shared-memory ring, the encode helper turns it into PQ/BT.2020 HEVC
# Main10, ffmpeg muxes it to SRT. Pass = the ring advances, the frame is
# the camera rectangle, and ffprobe on the SRT stream reports
# yuv420p10le / smpte2084 / bt2020. Needs the helper built
# (agent/build-mac.sh or build-win.ps1) and ffmpeg + ffprobe on PATH.
#   QCB_TRANSPORT=agent QCB_AGENT=spawn smokes/run_smoke_pixels.sh [work-dir]
#   QCB_SMOKE_ENGINE=BLENDER_EEVEE_NEXT ... for the offscreen path
set -u
HERE="${0:a:h}"
SCRATCH="${1:-$(mktemp -d /tmp/qcb-pixels.XXXXXX)}"
echo "work dir: $SCRATCH"
BL="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
rm -f "$SCRATCH"/host.json "$SCRATCH"/replica.json "$SCRATCH"/verdict.json
mkdir -p "$SCRATCH/bl_replica" "$SCRATCH/bl_host"
# A mux or helper left over from an earlier run of THIS smoke (a killed
# Blender orphans them until the listen times out) would hold port 19998.
cleanup_children() {
  pkill -f 'srt://127.0.0.1:19998\?mode=listener' 2>/dev/null
  pkill -f 'qcb-encode-(mac|win)(\.exe)? --shm qcb-' 2>/dev/null
}
cleanup_children

QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_replica" \
  "$BL" --factory-startup -noaudio --window-geometry 0 0 1600 1000 --python "$HERE/smoke_pixels_replica.py" \
  -- "$SCRATCH" > "$SCRATCH/replica.log" 2>&1 &
REPLICA_PID=$!
sleep 4
QCB_DEBUG=1 BLENDER_USER_RESOURCES="$SCRATCH/bl_host" \
  "$BL" --factory-startup -noaudio --window-geometry 0 0 1200 800 --python "$HERE/smoke_pixels_host.py" \
  -- "$SCRATCH" > "$SCRATCH/host.log" 2>&1 &
HOST_PID=$!

python3 - "$SCRATCH" <<'EOF'
import json, os, subprocess, sys, time, struct
S = sys.argv[1]

def load(n):
    try:
        return json.load(open(f"{S}/{n}"))
    except Exception:
        return {}

def wait(pred, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        h, r = load("host.json"), load("replica.json")
        try:
            if pred(h, r):
                return h, r
        except Exception:
            pass
        time.sleep(0.5)
    return load("host.json"), load("replica.json")

checks = {}
h, r = wait(lambda h, r: r.get("stats", {}).get("bootstraps", 0) >= 1 and h.get("note") == "connected", 60)
checks["boot"] = r.get("stats", {}).get("bootstraps", 0) >= 1
engine = os.environ.get("QCB_SMOKE_ENGINE", "CYCLES")
want_engine = "QCB_CYCLES" if engine == "CYCLES" else engine
h, r = wait(lambda h, r: r.get("engine") == want_engine, 20)
checks["engine_mapped"] = r.get("engine") == want_engine
h, r = wait(lambda h, r: r.get("readback", {}).get("published", 0) >= 5, 60)
checks["frames_published"] = r.get("readback", {}).get("published", 0) >= 5
# Shot Mode: the published frame becomes the camera rectangle, fitted to the
# stream width (1280 here) — 16:9 like the scene, within rounding
def shot_frame(h, r):
    size = (r.get("readback") or {}).get("size") or [0, 0]
    return (h.get("shot") and r.get("view_persp") == "CAMERA" and 16 <= size[0] <= 1280
            and size[0] % 2 == 0 and size[1] % 2 == 0 and abs(size[0] / max(1, size[1]) - 16 / 9) < 0.02)
h, r = wait(shot_frame, 40)
rb = r.get("readback", {})
size = rb.get("size") or [0, 0]
checks["replica_in_camera_view"] = r.get("view_persp") == "CAMERA"
checks["shot_frame_is_camera_rect"] = shot_frame(h, r)
checks["no_readback_errors"] = rb.get("errors", 0) == 0

# the ring itself: attach by its CURRENT name (a regrow renames it) and watch `latest` move
ring_ok = False
try:
    from multiprocessing import shared_memory
    name = (load("replica.json").get("readback") or {}).get("ring")
    try:
        shm = shared_memory.SharedMemory(name=name, track=False)
    except TypeError:
        shm = shared_memory.SharedMemory(name=name)
    magic = bytes(shm.buf[:4])
    l0 = struct.unpack_from("<Q", shm.buf, 48)[0]
    time.sleep(2.0)
    l1 = struct.unpack_from("<Q", shm.buf, 48)[0]
    ring_ok = magic == b"QCBF" and l1 > l0
    shm.close()
except Exception as exc:
    print("ring attach failed:", exc)
checks["ring_advances"] = ring_ok

# the stream: one SRT viewer reads the tags the mux pinned. Homebrew's
# ffmpeg has no SRT; the replica resolved QCView's, use the same one.
h, r = wait(lambda h, r: "streaming" in (r.get("pixel") or ""), 30)
checks["helper_streaming"] = "readback" in (r.get("pixel") or "")
url = h.get("viewer_url", "")
probe = {}

def ffmpeg_with_srt():
    cands = []
    tb = os.path.expanduser("~/Library/Application Support/QCView/toolbox.json")
    if os.path.exists(tb):
        try:
            cands.append(json.load(open(tb)).get("ffmpeg") or "")
        except Exception:
            pass
    cands.append("ffmpeg")
    for c in cands:
        if not c:
            continue
        try:
            out = subprocess.run([c, "-hide_banner", "-protocols"], capture_output=True, text=True, timeout=20).stdout
            if "srt" in out:
                return c
        except Exception:
            pass
    return None

ff = ffmpeg_with_srt()
if url and ff:
    # ffprobe beside that ffmpeg; a small probe, because an idle viewport
    # streams a few hundred kbit/s and the 5 MB default would take a minute
    ffprobe = os.path.join(os.path.dirname(ff), "ffprobe") if os.path.dirname(ff) else "ffprobe"
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-rw_timeout", "15000000",
                              "-probesize", "300000", "-analyzeduration", "1000000", "-select_streams", "v:0",
                              "-show_entries", "stream=codec_name,profile,pix_fmt,width,height,color_transfer,color_primaries,color_space",
                              "-of", "json", url], capture_output=True, text=True, timeout=45)
        probe = (json.loads(out.stdout or "{}").get("streams") or [{}])[0]
        if not probe:
            print("ffprobe:", out.stderr[-400:])
    except Exception as exc:
        print("ffprobe failed:", exc)
else:
    print("no SRT-capable ffmpeg found" if not ff else "no viewer url")
checks["srt_hevc_main10"] = probe.get("codec_name") == "hevc" and "Main 10" in str(probe.get("profile", ""))
checks["srt_yuv420p10"] = probe.get("pix_fmt") == "yuv420p10le"
checks["srt_pq_bt2020_tags"] = (probe.get("color_transfer") == "smpte2084" and probe.get("color_primaries") == "bt2020"
                                and probe.get("color_space") == "bt2020nc")
r = load("replica.json")
size_now = (r.get("readback") or {}).get("size") or [0, 0]
# the stream's size is the published frame's; a size switch between the
# status read and the probe (shot mode engaging) is not a mismatch
checks["srt_size_matches_frame"] = ([probe.get("width"), probe.get("height")] in (size, size_now)
                                    or (probe.get("width") == size_now[0] and 0 < (probe.get("height") or 0) <= size_now[0]))
checks["replica_clean"] = r.get("stats", {}).get("apply_errors", 0) == 0 and r.get("stats", {}).get("gaps", 0) == 0
deep = h.get("deep_link", "")
# 2026-10-27 colour awareness: the link names the wire, the working space the
# wire was converted FROM, the host's config kind, display and view — by name.
checks["deep_link_carries_view"] = ("wire=pq-bt2020-100" in deep and "view=AgX" in deep
                                    and "input=Linear%20Rec.709" in deep and "display=sRGB" in deep
                                    and "config=blender" in deep and "cm_ok=1" in deep)
cm_h, cm_r = h.get("cm_host") or {}, h.get("cm_replica") or {}
checks["cm_exchanged"] = (bool(cm_h.get("digest")) and bool(cm_r.get("digest"))
                          and cm_h.get("role") == "HOST" and cm_r.get("role") == "REPLICA"
                          and not [m for m in (h.get("cm_mismatches") or []) if m[0] == "error"])
fmt_w = ((h.get("peer_stream") or {}).get("format") or {}).get("working") or {}
m709 = [[0.6274, 0.3293, 0.0433], [0.0691, 0.9195, 0.0114], [0.0164, 0.0880, 0.8956]]
checks["wire_matrix_advertised"] = (fmt_w.get("name") == "Linear Rec.709" and fmt_w.get("assumed") is False
                                    and all(abs(fmt_w["to_bt2020"][a][b] - m709[a][b]) < 2e-3 for a in range(3) for b in range(3)))
argv = r.get("helper_argv") or []
checks["helper_argv_has_matrix"] = "--matrix" in argv and argv[argv.index("--matrix") + 1].count(",") == 8

# Viewer rejoin (2026-10-27, the "dead stream"): a viewer reads 3 s and dies
# hard; the next viewer must connect within 25 s. Then the listener must
# still take a viewer after sitting idle past the old 10 s listen timeout.
def srt_caller_ok(secs):
    try:
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-probesize", "300000",
                        "-analyzeduration", "1000000", "-i", url, "-frames:v", "1", "-f", "null", "-"],
                       timeout=secs, check=True, capture_output=True)
        return True
    except Exception:
        return False
ffmpeg = ff
rejoin = False
if url and ffmpeg:
    reader = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-probesize", "300000",
                               "-analyzeduration", "1000000", "-i", url, "-t", "3", "-f", "null", "-"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3.0)
    reader.kill(); reader.wait()
    # An idle, converged stream is ~0.3 Mbps, so a caller's 300 KB probe
    # alone takes ~8 s: give each attempt 20 s, the rejoin 45 s in all.
    t0 = time.time()
    while time.time() - t0 < 45 and not rejoin:
        rejoin = srt_caller_ok(20)
        if not rejoin:
            time.sleep(1.0)
    checks["viewer_rejoin"] = rejoin
    time.sleep(16.0)   # idle past the old listen timeout
    checks["listener_survives_idle"] = srt_caller_ok(20)

verdict = {"pass": all(checks.values()), "checks": checks, "readback": rb, "probe": probe,
           "pixel": r.get("pixel"), "ffmpeg": r.get("ffmpeg"), "deep_link": deep}
json.dump(verdict, open(f"{S}/verdict.json", "w"), indent=1)
print(json.dumps({"pass": verdict["pass"], "checks": checks, "readback": rb, "probe": probe, "pixel": r.get("pixel")}, indent=1))
sys.exit(0 if verdict["pass"] else 1)
EOF
RESULT=$?

kill $HOST_PID $REPLICA_PID 2>/dev/null; sleep 3; kill -9 $HOST_PID $REPLICA_PID 2>/dev/null
cleanup_children
exit $RESULT

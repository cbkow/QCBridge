<#
.SYNOPSIS
  The pixel smoke on Windows (the twin of run_smoke_pixels.sh): replica and
  host Blenders on one machine, the readback pixel path on, ffprobe on the
  SRT stream. Pass = the ring advances, the frame is the camera rectangle,
  the stream is HEVC Main10 yuv420p10le tagged bt2020/smpte2084.

  Run it from an INTERACTIVE session (Blender and the D3D11 encoder need a
  desktop): on the lab box, `C:\qcb-lab\run.ps1 -Name pixels -Command
  "powershell -ExecutionPolicy Bypass -File <repo>\smokes\run_smoke_pixels.ps1 <workdir>"`.
  Needs the agent and the encode helper built (agent\build-win.ps1) and an
  ffmpeg with SRT on PATH.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File smokes\run_smoke_pixels.ps1 C:\qcb-lab\pixels
  $env:QCB_SMOKE_ENGINE = "BLENDER_EEVEE"; ... (the offscreen path)
#>
param([string] $Scratch = (Join-Path $env:TEMP ("qcb-pixels-" + (Get-Date -Format "HHmmss"))))
$ErrorActionPreference = "Continue"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = Split-Path -Parent $Here
$BL = if ($env:BLENDER) { $env:BLENDER } else { "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" }
New-Item -ItemType Directory -Force -Path "$Scratch\bl_replica", "$Scratch\bl_host" | Out-Null
Remove-Item "$Scratch\host.json", "$Scratch\replica.json", "$Scratch\verdict.json" -ErrorAction SilentlyContinue
"work dir: $Scratch"
$env:QCB_TRANSPORT = "agent"; $env:QCB_AGENT = "spawn"; $env:QCB_DEBUG = "1"

# leftovers from an earlier run of THIS smoke would hold port 19998
Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -match 'srt://127.0.0.1:19998\?mode=listener') -or ($_.CommandLine -match 'qcb-encode-win.exe.*--shm qcb-') } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

function startb($userdir, $script, $log, $geo) {
  $env:BLENDER_USER_RESOURCES = $userdir
  $p = Start-Process -FilePath $BL -ArgumentList (@("--factory-startup", "-noaudio", "--window-geometry") + $geo + @("--python", "$Here\$script", "--", $Scratch)) `
       -RedirectStandardOutput $log -RedirectStandardError "$log.err" -PassThru -WorkingDirectory $Repo
  $null = $p.Handle
  return $p
}
$replica = startb "$Scratch\bl_replica" "smoke_pixels_replica.py" "$Scratch\replica.log" @("0", "0", "1600", "1000")
Start-Sleep 4
$host_ = startb "$Scratch\bl_host" "smoke_pixels_host.py" "$Scratch\host.log" @("0", "0", "1200", "800")

$py = @'
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
h, r = wait(lambda h, r: r.get("stats", {}).get("bootstraps", 0) >= 1 and h.get("note") == "connected", 90)
checks["boot"] = r.get("stats", {}).get("bootstraps", 0) >= 1
engine = os.environ.get("QCB_SMOKE_ENGINE", "CYCLES")
want_engine = "QCB_CYCLES" if engine == "CYCLES" else engine
h, r = wait(lambda h, r: r.get("engine") == want_engine, 20)
checks["engine_mapped"] = r.get("engine") == want_engine
h, r = wait(lambda h, r: r.get("readback", {}).get("published", 0) >= 5, 90)
checks["frames_published"] = r.get("readback", {}).get("published", 0) >= 5
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
ring_ok = False
try:
    from multiprocessing import shared_memory
    name = (load("replica.json").get("readback") or {}).get("ring")
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
h, r = wait(lambda h, r: "streaming" in (r.get("pixel") or ""), 30)
checks["helper_streaming"] = "readback" in (r.get("pixel") or "")
url = h.get("viewer_url", "")
probe = {}
if url:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-rw_timeout", "15000000",
                              "-fpsprobesize", "0", "-probesize", "300000", "-analyzeduration", "1000000", "-select_streams", "v:0",
                              "-show_entries", "stream=codec_name,profile,pix_fmt,width,height,color_transfer,color_primaries,color_space",
                              "-of", "json", url], capture_output=True, text=True, timeout=45)
        probe = (json.loads(out.stdout or "{}").get("streams") or [{}])[0]
        if not probe:
            print("ffprobe:", out.stderr[-400:])
    except Exception as exc:
        print("ffprobe failed:", exc)
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
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-fpsprobesize", "0", "-probesize", "300000",
                        "-analyzeduration", "1000000", "-i", url, "-frames:v", "1", "-f", "null", "-"],
                       timeout=secs, check=True, capture_output=True)
        return True
    except Exception:
        return False
ffmpeg = "ffmpeg"   # on PATH, as the probe's ffprobe
rejoin = False
if url and ffmpeg:
    reader = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-fpsprobesize", "0", "-probesize", "300000",
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
    # A viewer must get a picture within seconds of asking (2026-10-28: an
    # idle stream used to leave the muxer in its input probe for a minute,
    # and callers with "Input/output error").
    # A real viewer retries (QCView: 250 ms → 3 s backoff); the gate is the
    # time until a retrying caller has a frame, not one attempt's luck.
    t_join = time.time()
    numbers_join = None
    while time.time() - t_join < 8.0:
        if srt_caller_ok(6):
            numbers_join = round(time.time() - t_join, 2)
            break
        time.sleep(0.5)
    checks["viewer_connects_fast"] = numbers_join is not None and numbers_join < 5.0
verdict = {"pass": all(checks.values()), "checks": checks, "readback": rb, "probe": probe, "join_s": numbers_join,
           "pixel": r.get("pixel"), "ffmpeg": r.get("ffmpeg"), "deep_link": deep}
json.dump(verdict, open(f"{S}/verdict.json", "w"), indent=1)
print(json.dumps({"pass": verdict["pass"], "checks": checks, "readback": rb, "probe": probe, "pixel": r.get("pixel"), "join_s": numbers_join}, indent=1))
sys.exit(0 if verdict["pass"] else 1)
'@
$pyfile = Join-Path $Scratch "poll.py"
Set-Content -Path $pyfile -Value $py -Encoding ascii
$PY = "C:\Program Files\Blender Foundation\Blender 5.2\5.2\python\bin\python.exe"
if (-not (Test-Path $PY)) { $PY = "python" }
& $PY -I $pyfile $Scratch
$result = $LASTEXITCODE

foreach ($p in @($host_, $replica)) { if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
Start-Sleep 2
Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -match 'srt://127.0.0.1:19998\?mode=listener') -or ($_.CommandLine -match 'qcb-encode-win.exe.*--shm qcb-') } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
"pixels exit=$result"
exit $result

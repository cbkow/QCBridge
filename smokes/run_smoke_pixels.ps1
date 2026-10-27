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
                              "-probesize", "300000", "-analyzeduration", "1000000", "-select_streams", "v:0",
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
checks["deep_link_carries_view"] = "wire=pq-bt2020-100" in deep and "view=AgX" in deep and "input=linear-rec709" in deep
verdict = {"pass": all(checks.values()), "checks": checks, "readback": rb, "probe": probe,
           "pixel": r.get("pixel"), "ffmpeg": r.get("ffmpeg"), "deep_link": deep}
json.dump(verdict, open(f"{S}/verdict.json", "w"), indent=1)
print(json.dumps({"pass": verdict["pass"], "checks": checks, "readback": rb, "probe": probe, "pixel": r.get("pixel")}, indent=1))
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

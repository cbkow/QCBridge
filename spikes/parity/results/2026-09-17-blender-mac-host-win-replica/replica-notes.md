# Replica side — Windows 4K replica for the Mac-host Blender VPN runs

Companion to `notes.md` (latency, written by the Mac session). Times UTC
unless marked local (Windows local = UTC−4).

## Machine and setup

- **GPU**: NVIDIA GeForce RTX 5090, driver 610.88. **CPU**: AMD Threadripper
  PRO 7955WX. Windows 11 Pro 26200.
- **Blender**: 5.2.0 LTS (build 2026-07-14), bundled Python 3.13.13.
  `--factory-startup -noaudio`, `BLENDER_USER_RESOURCES=C:\qcb-smoke\bl_replica`.
  pyzmq 26.2.1 (libzmq 4.3.5) from the cp313 win_amd64 wheel unzipped to
  `C:\qcb-smoke\pysite`.
- **Display**: kiosk on the primary display = `ddagrab output_idx=0`
  (checked by grabbing a frame from each output), 3840x2160 physical, 125%
  Windows scaling (desktop reports 3072x1728), monitor reports **59 Hz**. A
  second 3840x2160 display is output 1. Kiosk viewport region was
  0,0,3840,2160 in every `replica.json` dump.
- **Firewall**: Windows Firewall disabled on the Private and Public profiles;
  the VPN adapter is Private. No rules needed (and the session wasn't
  elevated to add any).
- **ffmpeg**: QCView n9.0.1 (`%LOCALAPPDATA%\QCView\bin\ffmpeg.exe`).
- **Cycles backend**: `QCB_SMOKE_CYCLES_DEVICE=OPTIX` (only OptiX devices
  enabled); host sets `scene.cycles.device = GPU`. **Backend requested
  OptiX, not verified in the UI.** GPU rendering confirmed indirectly: GPU
  64–68% / 226–231 W and Blender at ~1.8 CPU cores during Cycles (EEVEE:
  GPU 16–17%).
- Blender console, both launches: `WARNING HIPEW initialization failed`
  (AMD HIP backend, irrelevant on NVIDIA); OCIO loaded from the machine's
  `OCIO` env (`C:\Volumes\union-ny-gfx\...\config.ocio`), not factory.
  No other warnings or errors.

## SRT runs (addon stream, `QCB_SMOKE_STREAM=1`, latency 20) — R1 EEVEE, R3 Cycles

Replica launched 16:14:57. `replica.json`:

| When | pixel | session_note | bootstraps | seq | gaps / apply_errors / unknown_uuid / unmapped | last_error |
| --- | --- | --- | ---: | ---: | --- | --- |
| before host (16:15) | streaming | listening | 0 | 0 | 0 / 0 / 0 / 0 | — |
| R1 EEVEE (16:19) | streaming | host connected | 1 | 1 | 0 / 0 / 0 / 0 | — |
| R3 Cycles (16:23) | streaming | host connected | 2 | 1 | 0 / 0 / 0 / 0 | — |
| end (16:28) | streaming | host connected | 2 | 1 | 0 / 0 / 0 / 0 | — |

Bootstrap `.blend` reads at 16:19:02 (EEVEE host) and 16:21:24 (Cycles host).
Orbit visibly followed the host (chris, by eye). Two 1280x720 thumbnails of
the Cycles kiosk 3 s apart showed the cube moving with the orbit, the probe
strip bottom-left and the `live · seq 1` badge, with Cycles grain on the
shadow side that varied between grabs. A constant orbit resets accumulation
every redraw, so the viewport never converges during these runs.

GPU (`nvidia-smi`, 1 s samples): EEVEE during a read 16–17% GPU, NVENC
22–25%, 6.28 GB. Cycles between reads 64–68% GPU, 226–231 W, NVENC 0% (the
SRT listener doesn't encode until a caller connects), 8.09 GB.

### Respawn check (`%TEMP%\qcbridge-pixelpath.log`) — pass

Every exit logs the same caller-disconnect block (`Error submitting a packet
to the muxer: I/O error` … `Error closing file: I/O error`); nothing else.
Spawn times converted from local.

| Connection (Mac, UTC) | Served by spawn | Next spawn | Listener back after disconnect |
| --- | --- | --- | --- |
| frame grab + ffprobe ~16:19:25–35 | 16:15:02 | 16:19:31 | ≤ ~4 s (overlapping connects) |
| (short connect) | 16:19:31 | 16:19:35 | 4 s spawn → spawn |
| R1 r1 16:19:47–~16:20:18 | 16:19:35 | 16:20:26 | ~8 s |
| R1 r2 16:20:25–~16:20:56 | 16:20:26 | 16:21:04 | ~8 s |
| two ffprobes ~16:21:00–10 | 16:21:04 | 16:21:08 | ~4 s |
| R3 r1 16:26:47–16:27:19 | 16:21:08 (idle ~5.5 min, no hang) | 16:27:26 | ~7 s |
| R3 r2 16:27:25–16:27:57 | 16:27:26 | 16:28:05 | ~8 s |

Windows ffmpeg exits by itself on caller disconnect and the addon respawns
the listener in 4–8 s; no hang (unlike macOS). Callers that connect before
the respawn (R1 r2, R3 r2: 1 s early) retry and get in.

Shutdown: Blender and its ffmpeg ended by process kill (no keyboard access
from the session); all ports released.

## Kyber runs (`QCB_SMOKE_STREAM=0` + external pipeline, cap 400) — R2 EEVEE, R4 Cycles

Replica relaunched 16:32:42 (`pixel: off`, `session_note: listening`). Final
`replica.json` (16:38): pixel off, host connected, bootstraps 2, seq 1, all
counters 0, no last_error. Bootstrap reads at ~16:33:57 and ~16:35:49.

Pipeline (started 16:33:13, under `cmd /c`):

    ffmpeg -hide_banner -loglevel warning -filter_complex ddagrab=output_idx=0:framerate=60:draw_mouse=0
      -c:v hevc_nvenc -profile:v main10 -preset p4 -tune ull -delay 0 -bf 0 -b:v 50M -g 60
      -bsf:v hevc_metadata=aud=insert -f hevc -
    | kyber-send --listen 0.0.0.0:19999 --token spike --bitrate-cap-mbps 400 --fps 60

ffmpeg logged nothing at warning level for the whole run. ~57 AUs/s before
a client connected. Fingerprint `dae9ba8e…d936`.

Sender stats per session. Times come from the per-second stat line index
from 16:33:13, ±2 s. Gop skips are per session.

| Run | Session (UTC) | Sent AUs / keys (cumulative) | Queue drops | Max queue | Gop skips | quic_lost | Dwell avg / max ms | video Mbps | est wire Mbps | QUIC rtt ms | Session end |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- | ---: | --- | --- |
| R2 r1 EEVEE | ~16:34:15–16:34:50 | 2028 / 34 | 0 | 0 | 29 | 0 | 0.6–0.9 / 3.3 | 34.3–35.9 | ≤48.5 | 7.6–10.1 | idle timeout |
| R2 r2 EEVEE | ~16:34:53–16:35:28 | 2029 / 35 | 0 | 0 | 49 | 0 | 0.6–0.8 / 3.4 | 34.3–35.9 | ≤48.5 | 7.8–10.0 | idle timeout |
| R4 r1 Cycles | ~16:36:12–16:36:47 | 2028 / 35 | 0 | 0 | 46 | 0 | 0.5–0.7 / 5.7 | 31.7–36.1 | ≤48.7 | 7.8–10.5 | connection lost |
| R4 r2 Cycles | ~16:36:50–16:37:25 | 2028 / 35 | 0 | 0 | 49 | 0 | 0.5–0.7 / 4.0 | 35.0–35.5 | ≤47.9 | 7.7–10.5 | idle timeout |

- 59–61 AUs sent per second once running, in every session.
- **Dwell is tiny (≤0.9 ms avg, ≤5.7 max)**: the 4K desktop capture
  encodes to only ~35 Mbps at the 50M target, so cap 400 never holds a frame.
  The synthetic noise source ran at ~50 Mbps with 2.1–2.6 ms avg dwell.
- Gop skips of 29–49 per join = 0.5–0.8 s waiting for the next keyframe
  (GOP 60), with no IDR request on join.
- 3 of 4 sessions ended on QUIC idle timeout (reader killed without a close),
  one on `connection lost`. The next client was always accepted.
- Zero loss on both ends (Mac receiver: dropped / FEC missing / unrecovered /
  QUIC lost / holes all 0), so FEC recovery still wasn't exercised.
- GPU load during the Kyber runs was not sampled.

Shutdown: pipeline tree and Blender ended by process kill at ~16:38:50; all
ports released.

## Other observations

- **10-bit rung is 8-bit here** (receiver: Main 10, yuv420p). Replica side
  agrees: ddagrab outputs 8-bit BGRA D3D11 frames and the NVENC command has
  no p010 conversion. The external Kyber pipeline copies the addon's flags,
  so it has the same issue. S3.
- **Stale ~36%** (≈38 new stamps/s into 60 fps capture) in both engines.
  The display reports 59 Hz, so DDA can't deliver more than ~59 new desktop
  frames/s anyway. The binding limit is the 4K viewport redraw rate, which
  is below that.
- No visual stutter was reported during the runs. Cycles refinement isn't
  observable under a constant orbit (see thumbnails above).

# 2026-09-17 — Mac → Windows over the VPN, SRT (synthetic sender, no Blender)

Replica (sender) = chris's Mac (Apple Silicon), `probe_testsrc.py --srt-listen
0.0.0.0:19998`, hevc_videotoolbox 1920x1080 / 60 / 50M, QCView ffmpeg.
Host (reader) = Windows workstation (Threadripper PRO 7955WX, RTX 5090),
`probe_reader.py --srt 192.168.80.2:19998`, d3d11va decode, QCView ffmpeg
n9.0.1. Code at 6c6edab (srt120) / 9547989 (srt20; probe scripts unchanged).
Path: Windows 192.168.40.199 → 192.168.40.1 → Mac 192.168.80.2, ping RTT
7–9 ms. Reader 30 s per run. Raw per-frame data in `runs.jsonl`.

## Clock correction

The sender stamps with the Mac clock and the reader subtracts on the Windows
clock, so raw = true + (windows − mac), i.e.
**true = raw + (apple − windows) − (apple − mac)**, both offsets written as
server − local, which is what `w32tm /stripchart` and `sntp` print.

- Sign verified at 16:03 UTC with a hand-rolled SNTP query from Windows
  (offset = ((T2−T1)+(T3−T4))/2): +9.3…+9.4 ms, matching w32tm's +8.7 ms.
  **The Windows clock is ~9 ms behind time.apple.com.**
- Windows (`w32tm`, 3 samples right before each run): +8.8 ms at 15:33 UTC
  (srt120), +9.1 ms at 15:52 UTC (srt20). Stable ±0.1 ms.
- Mac (`sntp`, relayed): about −1 ms (last two readings: +0.3, −2.1), taken
  as apple − mac = −1 (Mac ~1 ms ahead). Mac sntp scatter is ±5 ms, and it
  dominates the uncertainty.

| Label | SRT latency | Raw p50 | p95 | p99 | apple − win | apple − mac | Corrected p50 / p95 | Frames scored / failed / stale |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| mac2win-srt120 | 120 | 260 | 273 | 275 | +8.8 | −1 | **270 / 283** (±5) | 1802 / 0 / 0 |
| mac2win-srt20 | 20 | 155 | 163 | 164 | +9.1 | −1 | **165 / 173** (±5) | 1802 / 0 / 0 |

For the reverse direction (Windows sender, Mac reader) the sign flips:
true = raw − (apple − windows) + (apple − mac).

## Readings

- **Over the VPN vs Mac loopback (both corrected):** srt120 270 vs 251 (+19),
  srt20 165 vs 151 (+14). That covers the ~4 ms one-way network path plus a
  different decoder (d3d11va on the Windows reader vs VT on the Mac). The
  VideoToolbox encoder's ~70 ms still dominates a Mac replica.
- **srt120 step at ~18 s:** p50 by 6 s window: 256, 256, 258, **269, 269**.
  The minimums stayed flat (243–245), so it isn't queue buildup; the jump is
  a bit under one frame. Cause not identified (possibly a Mac sender pacing
  phase slip, like the 90 vs 107 bimodality in the Mac loopback, or a clock
  adjustment). srt20 drifted less: 153, 154, 154, 158, 158.
- **srt20 first attempt failed:** at 15:52:47 the reader got
  `Error opening input: I/O error` (SRT caller couldn't open) and exited
  with "no video stream within 30 s". Ping was fine. A debug `ffmpeg -i
  srt://…` 1 s later connected (it logged `RCV-DROPPED 4 packet(s) … delayed
  for 75.896 ms` on join) and disconnected after 1 s. The scored reader run
  started at 15:53:35 against the same Mac listener. Cause of the first
  refusal unknown.
- **Decoder startup:** ~57 `Error constructing the frame RPS.` lines per run
  while probing, before the first decoded frame. Harmless.

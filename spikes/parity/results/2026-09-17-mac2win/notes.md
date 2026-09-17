# 2026-09-17 — Mac → Windows over the VPN, SRT + Kyber (synthetic sender, no Blender)

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

## Kyber (kyber-pipe @ 9547989, Windows build: rustc 1.89.0, release)

Mac: `probe_testsrc.py --stdout` (VT HEVC 1080p60 50M) | `kyber-send --listen
0.0.0.0:19999 --token spike --fps 60 --bitrate-cap-mbps N`. Windows, under
`cmd /c`: `kyber-recv --connect 192.168.80.2:19999 … | probe_reader.py --stdin
--seconds 30`. Same correction as above.

| Label | Raw p50 | p95 | p99 | max | apple − win | apple − mac | Corrected p50 / p95 / p99 | Frames scored / failed / stale |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| mac2win-kyber-cap150 | 127 | 129 | 136 | 168 | +9.4 (16:05:32) | −1.0 (16:05:06) | **137 / 139 / 146** (±5) | 1803 / 0 / 0 |
| mac2win-kyber-cap400 | 125 | 127 | 131 | 163 | +9.5 (16:07:43) | −0.7 (16:06:59) | **135 / 137 / 141** (±5) | 1802 / 0 / 0 |

Receiver (`kyber-recv`, 31 one-second samples each, 1 connection each):
holes, dropped_packets, fec_missing, fec_unrecovered and quic_lost were 0 in
every sample of both runs, so loss recovery wasn't exercised. 60 frames/s at
~50.2 Mbps, 31 keyframes. `max_gap_ms` per second was usually 19–27 ms, with
spikes of 49.5 / 49.6 / 57.0 (cap 150) and 46.7 / 48.3 / 53.2 / 55.2 (cap 400).
The spikes did not go away at cap 400, so sender dwell isn't the main cause.
Frames ≥145 ms raw: 8 (cap 150), 7 (cap 400). `rtt_ms` was constant within
each run (12.11, 11.69), so it looks like a cached value and shouldn't be
relied on.

Mac sender, cap 150 (relayed): 0 queue drops, 6 gop skips on join, ~67.4 Mbps
estimated on the wire, dwell 6.3 ms avg / 20.6 ms max.

- **Kyber vs SRT latency 20, Mac → Windows (corrected p50 / p95):** 137 / 139
  (cap 150) and 135 / 137 (cap 400) vs 165 / 173. Kyber is 28–30 ms faster
  at p50 and 34–36 ms at p95.
- **Cap 400 vs 150:** −2 ms p50, −2 ms p95, −5 ms p99.
- **Against Mac loopback Kyber (cap 400, 129 / 131):** +6 ms over the VPN, about
  the one-way network path plus a different decoder.

# 2026-09-17 — Why is the Mac encode path slow? Native VideoToolbox vs ffmpeg

Machine: chris's MacBook Pro, Apple M5 Max, macOS 27.0. Question from the
parity runs: the Mac pipe (ffmpeg `hevc_videotoolbox` → decode) sits ~65–70 ms
above NVENC and libx265 on the same harness. Is that Apple's encoder or
ffmpeg's wrapper?

## Native encoder (no ffmpeg)

`spikes/parity/vt-latency/vtlat.swift`: VTCompressionSession, hardware
required, IOSurface-backed CVPixelBufferPool, AllowFrameReordering=false,
ExpectedFrameRate, MaxKeyFrameInterval = fps, AverageBitRate. Frames paced in
real time, random-noise luma (worst case for rate control). Latency = submit
(`VTCompressionSessionEncodeFrame`) → output callback, 240 frames after 1 s
warmup.

| Config | p50 | p95 | p99 | Out Mbps | Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| HEVC 1080p60 nv12 50M, RealTime, PrioritizeSpeed | 4.9 | 11.7 | 12.0 | 52 | baseline |
| same, p010 (Main10) | 5.3 | 12.0 | 12.4 | 52 | 10-bit costs nothing |
| same, RealTime **off** | 2.9 | 3.2 | 3.3 | 64 | fastest + steadiest; overshoots bitrate |
| same, PrioritizeSpeed off | 6.8 | 15.9 | 16.1 | 54 | |
| same, MaxFrameDelayCount=0 | 4.5 | 7.9 | 11.1 | 52 | property rejected (-12900) — noise |
| same, EnableLowLatencyRateControl | 5.8 | 6.3 | 6.5 | 51 | **drops ~2/3 of frames** on noise (80/240 out); PrioritizeSpeed rejected |
| HEVC 4K60 nv12 50M, RealTime | 9.3 | 23.1 | 23.6 | 110 | overshoots on noise |
| HEVC 4K60, RealTime off | 8.9 | 9.1 | 9.2 | 110 | |
| HEVC 4K60, low-latency RC | 15.1 | 15.7 | 16.0 | 57 | drops frames (69/240) |
| H.264 1080p60 50M, RealTime | 7.7 | 17.2 | 17.6 | 76 | |
| H.264 1080p60, low-latency RC | 6.5 | 7.9 | 8.1 | 50 | drops frames (77/240) |
| HEVC 1080p30 50M, RealTime | 12.4 | 13.3 | 13.4 | 51 | |

**Apple's hardware encoder is fast: ~3–12 ms at 1080p60, ~9–23 ms at 4K60.**
No reordering in any config. Low-latency rate control holds bitrate by dropping
frames on pathological content — not what we want.

## ffmpeg `hevc_videotoolbox` through the probe pipe

Same synthetic stamped source (Python generator via process substitution —
this harness variant reads ~30 ms higher than `probe_testsrc.py --stdout`, so
compare rows to each other, not to earlier tables). 1080p60 50M, Main profile.

| ffmpeg input pix_fmt / flags | p50 | p95 |
| --- | ---: | ---: |
| p010le, -realtime 1 -prio_speed 1 (the addon's Mac flags) | 146 | 147 |
| p010le, -realtime 0 -prio_speed 1 | 146 | 147 |
| nv12, -realtime 1 -prio_speed 1 | 129 | 130 |
| nv12, -realtime 0 | 129 | 131 |
| nv12, -realtime 1 -constant_bit_rate 1 | 96 | 97 |

## Readings

- **The ~65–70 ms Mac penalty is ffmpeg's wrapper/pipeline, not VideoToolbox.**
  Native encode is single-digit ms; ffmpeg's path is ~100 ms+ on the same machine.
- Inside ffmpeg, `nv12` input saves ~17 ms (p010le frames take a slower
  conversion/copy path) and `-constant_bit_rate 1` saves another ~33 ms. That
  still leaves tens of ms over native — the remainder was not isolated
  (candidates: CPU frame copy into non-shared buffers, ffmpeg's encode queue).
- **Fix direction:** don't route Mac capture+encode through ffmpeg. A native
  helper — ScreenCaptureKit → IOSurface → VTCompressionSession → Annex-B —
  is exactly Plank's macOS host design, and fits the Kyber helper process.
  Expected Mac replica encode cost ≈ NVENC-class.
- Quick interim win if ffmpeg stays: `-pix_fmt nv12 -constant_bit_rate 1` on
  the Mac branch of `pixel_path.py` (8-bit accepted for the experiment).

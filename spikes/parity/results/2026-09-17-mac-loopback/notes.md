# 2026-09-17 — Mac loopback (synthetic sender, no Blender)

Machine: chris's Mac (Apple Silicon), QCView-bundled ffmpeg
(`/Applications/qcview.app/Contents/Helpers/ffmpeg`). `probe_testsrc.py` →
transport → `probe_reader.py`, all on 127.0.0.1. Latency = stamp at frame
generation → decoded frame in the reader (no present). Noise on unless noted.

| Label | Transport | Encoder | Size / fps / bitrate | p50 | p95 | Frames scored / failed |
| --- | --- | --- | --- | ---: | ---: | --- |
| mac-loop-srt120 | SRT latency 120 | hevc_videotoolbox | 1920x1080 / 60 / 50M | 251 | 253 | 1201 / 0 |
| mac-loop-srt20 | SRT latency 20 | hevc_videotoolbox | 1920x1080 / 60 / 50M | 151 | 153 | 721 / 0 |
| mac-pipe | stdout pipe | hevc_videotoolbox | 1920x1080 / 60 / 50M | 112 | 113 | 721 / 0 |
| pipe-vt-swdec | stdout pipe, SW decode | hevc_videotoolbox | 1920x1080 / 60 / 50M | 111 | 113 | 361 / 0 |
| pipe-vt-nonoise | stdout pipe | hevc_videotoolbox | 1920x1080 / 60 / 50M, no noise | 104 | 107 | 481 / 0 |
| pipe-vt-720 | stdout pipe | hevc_videotoolbox | 1280x720 / 60 / 10M | 107 | 108 | 361 / 0 |
| pipe-vt-720-30fps | stdout pipe | hevc_videotoolbox | 1280x720 / 30 / 10M | 142 | 146 | 181 / 0 |
| vt[-bf 0] (3 runs) | stdout pipe | hevc_videotoolbox -bf 0 | 1280x720 / 60 / 10M | 90 / 107 / 107 | — | 361 / 0 |
| pipe-x265-720 | stdout pipe | libx265 ultrafast zerolatency | 1280x720 / 60 / 5M | 43 | 46 | 362 / 0 |

## Readings

- **SRT costs its latency setting + ~30–40 ms** on loopback (251 vs 151 vs 112 pipe).
- **VideoToolbox HEVC holds ~70 ms + ~2 frames** regardless of resolution,
  bitrate or decode path (60 fps ≈ 107 ms, 30 fps ≈ 142 ms). libx265
  zerolatency does the whole pipe in 43 ms, so it is the encoder, not the
  harness. Run-to-run bimodality of one frame (90 vs 107) is pacing phase.
- Plank's macOS host sets the same VT flags ffmpeg exposes (RealTime,
  AllowFrameReordering=NO, PrioritizeEncodingSpeedOverQuality) — so this is
  not a missing ffmpeg flag. Open: native VT harness, VT H.264 low-latency
  rate control, or ask Plank what latency their mac host measures.
- Matters for any **Mac replica** (pixel_path.py mac branch uses this exact
  encoder) — the Windows NVENC path was tuned, the Mac path never was.

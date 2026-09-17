# 2026-09-17 — Windows loopback (synthetic sender, no Blender)

Machine: chris's Windows workstation — AMD Threadripper PRO 7955WX, RTX 5090
(driver 610.88), Windows 11 Pro 26200, Python 3.13.14. QCView-bundled ffmpeg
n9.0.1-27 (`%LOCALAPPDATA%\QCView\bin\ffmpeg.exe`, resolved via toolbox.json —
spawned fine, no WinError 5; has libsrt, hevc_nvenc, libx265). Reader decode
is `-hwaccel d3d11va` unless noted. `probe_testsrc.py` → transport →
`probe_reader.py`, all on 127.0.0.1. Latency = stamp at frame generation →
decoded frame in the reader (no present). Noise on. Pipe runs: sender 20 s,
reader 15 s; SRT runs: reader 20 s. Raw per-frame data in `runs.jsonl`.

`python -m pytest -q` (throwaway venv with pytest + pyzmq): 74 passed, 2 skipped.

| Label | Transport | Encoder | Size / fps / bitrate | p50 | p95 | p99 | Frames scored / failed |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| win-loop-srt120 | SRT latency 120 | hevc_nvenc | 1920x1080 / 60 / 50M | 191 | 196 | 198 | 1203 / 0 |
| win-loop-srt20 | SRT latency 20 | hevc_nvenc | 1920x1080 / 60 / 50M | 91 | 96 | 98 | 1202 / 0 |
| win-pipe-nvenc-1080p60 (3 runs) | stdout pipe | hevc_nvenc | 1920x1080 / 60 / 50M | 43 / 43 / 43 | 45 / 45 / 45 | 46 / 46 / 46 | 903 / 0 each |
| win-pipe-nvenc-1080p60-swdec | stdout pipe, SW decode | hevc_nvenc | 1920x1080 / 60 / 50M | 55 | 60 | 63 | 901 / 0 |
| win-pipe-nvenc-720p60 (3 runs) | stdout pipe | hevc_nvenc | 1280x720 / 60 / 10M | 38 / 38 / 38 | 39 / 39 / 39 | 40 / 40 / 40 | 903 / 0 each |
| win-pipe-nvenc-720p30 | stdout pipe | hevc_nvenc | 1280x720 / 30 / 10M | 72 | 73 | 73 | 451 / 0 |
| win-pipe-x265-720p60 | stdout pipe | libx265 ultrafast zerolatency | 1280x720 / 60 / 5M | 41 | 43 | 47 | 903 / 0 |

NVENC flags (from `probe_testsrc.py`): main10 p010le, `-preset p4 -tune ull
-delay 0 -bf 0`, GOP = fps. Stale = 0 in every run. Sender reported 1–3 late
frames per run (≤0.3%).

## Readings

- **NVENC HEVC 1080p60 pipe: 43 ms, vs 112 ms for Mac VideoToolbox**, same
  harness and settings. That's ~69 ms less, and identical across 3 runs (no
  bimodality). NVENC matches libx265 zerolatency (Win 41 ms, Mac 43 ms), so
  on Windows the encoder adds nothing measurable over the harness floor.
- **The floor is about 2 frames + ~5 ms and scales with fps:** 720p60 38 ms,
  720p30 72 ms (+34 ms ≈ one extra 33 ms frame period). The harness/ffmpeg
  pipe holds frames by count, not by time. The Mac numbers have the same
  shape (107 → 142) with VT's ~70 ms added on top.
- **Resolution/bitrate cost is small:** 1080p/50M is 5 ms slower than 720p/10M.
- **SW decode costs +12 ms p50 (+15 ms p95)** at 1080p60 (55 vs 43), so
  d3d11va decode is worth keeping.
- **SRT costs its latency setting + ~28 ms over the pipe** on loopback
  (191 = 43 + 120 + 28; 91 = 43 + 20 + 28), steady at both settings. For
  comparison, the same arithmetic on the Mac table gives +19 ms
  (251 = 112 + 120 + 19; 151 = 112 + 20 + 19).
- **Decoder startup noise:** every run logs ~30–60 `[hevc] Error constructing
  the frame RPS.` lines, all during ffmpeg's input probe before the first
  decoded frame (it joins the Annex-B stream mid-GOP). Harmless: 0 failed
  frames.
- **Implication for pairings:** a Windows replica gets ~43 ms glass-to-decode
  before transport; a Mac replica ~112 ms. So encoder latency on the replica
  OS matters more than host OS. With SRT at latency 20, Win-replica loopback is
  91 ms end to end.

## Kyber Windows build check — PASS

Clone: `github.com/instinctual/plank-kymux` @ `912ece5` (2026-09-08), outside
the repo (`Documents\GitHub\plank-kymux`). MSVC toolchain, VS 2022 Community
build tools. The clone itself has no rust-toolchain file or rust-version;
checked on both 1.96.0 (installed stable) and 1.89.0 (Kyber's pin), each in a
clean target dir.

| Command | rustc 1.96.0 | rustc 1.89.0 |
| --- | --- | --- |
| `cargo check` | pass, 0 warnings (13.4 s) | pass, 0 warnings (9.2 s) |
| `cargo check -p kymux --features backend-quinn,backend-wtransport` | pass, 0 warnings (13.4 s) | pass, 0 warnings (11.0 s) |

The plain workspace check doesn't pull in `ring`. The quinn/wtransport feature
check compiles `ring v0.17.14` natively with MSVC without issue, so the Mac →
Windows cross-check failure (missing Windows C headers) is cross-compile only.
No Windows blocker for Kyber at the `cargo check` level.

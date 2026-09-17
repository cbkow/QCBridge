# 2026-09-17: Mac loopback, Kyber pipe vs stdout pipe vs SRT (synthetic sender, no Blender)

Machine: chris's Mac (Apple Silicon, 18 cores), QCView-bundled ffmpeg.
`probe_testsrc.py` → transport → `probe_reader.py`, all on 127.0.0.1, 30 s of
source and 20 s scored per run. Latency = stamp at frame generation → decoded
frame in the reader. Noise on. The harness is the current working tree: strip
at bottom-left, AUDs inserted on `--stdout`, false-lock rejection added. The
pipe baseline was re-measured in the same batch, so the columns compare
directly. Earlier notes put the strip at the top.

Kyber = `spikes/parity/kyber-pipe` release build: KyProto
`VideoProtocol::UnreliableFec` (RaptorQ, 30 % repair) over QUIC datagrams,
MTU 1344, a fixed-rate window, and datagrams paced at `--bitrate-cap-mbps`
(wire rate, FEC included).

Conditions: the main batch ran 11:23:02–11:30:32 with no cargo or rustc
running. The parent session's Blender smokes had stopped by 11:23. Blender
was still at 45 % CPU when `mac-pipe-again-r1` started at 11:23:02, but that
run matches r2 and r3. Load average fell from 5.0 to 2.3 during the batch
(Jump Desktop was running throughout). Runs marked *contended* came after
11:30:57, once the parent's cross-machine Windows runs may have started on
this Mac (port 19998). Two earlier batches overlapped the parent's Blender
smokes. Those were discarded and are not in `runs.jsonl`. SRT used port 19997
to stay clear of the parent's 19998 listener.

| Label | Transport | Encoder | Size / fps / bitrate | p50 | p95 | Frames scored / failed |
| --- | --- | --- | --- | ---: | ---: | --- |
| mac-pipe-again-r1 / r2 / r3 | stdout pipe | hevc_videotoolbox | 1920x1080 / 60 / 50M | 112 / 112 / 112 | 113 / 113 / 113 | 1199, 1201, 1201 / 0 |
| mac-kyber-loop-r1 / r2 | Kyber, cap 80 | hevc_videotoolbox | 1920x1080 / 60 / 50M | 137 / 137 | 152 / 151 | 1208, 1207 / 0 |
| mac-pipe-720-r1 / r2 | stdout pipe | hevc_videotoolbox | 1280x720 / 60 / 10M | 107 / 107 | 108 / 108 | 1201, 1201 / 0 |
| mac-kyber-720-r1 / r2 | Kyber, cap 80 | hevc_videotoolbox | 1280x720 / 60 / 10M | 125 / 107 | 127 / 109 | 1204, 1204 / 0 |
| mac-pipe-100M-r1 / r2 | stdout pipe | hevc_videotoolbox | 1920x1080 / 60 / 100M | 113 / 113 | 114 / 114 | 1201, 1201 / 0 |
| mac-kyber-100M-cap150-r1 / r2 | Kyber, cap 150 | hevc_videotoolbox | 1920x1080 / 60 / 100M | 139 / 139 | 147 / 148 | 1208, 1208 / 0 |
| mac-srt20-again-r1 / r2 | SRT latency 20 | hevc_videotoolbox | 1920x1080 / 60 / 50M | 152 / 151 | 153 / 153 | 1201, 1201 / 0 |
| mac-kyber-cap400-contended r1 / r2 (11:30:57, 11:31:33) | Kyber, cap 400 | hevc_videotoolbox | 1920x1080 / 60 / 50M | 129 / 129 | 131 / 131 | 1201, 1201 / 0 |
| mac-pipe-again-contended (≈11:32) | stdout pipe | hevc_videotoolbox | 1920x1080 / 60 / 50M | 112 | 113 | 1201 / 0 |

p99: Kyber at cap 80 hit 157–158, cap 150 hit 152–154, cap 400 hit 133. The
pipe stayed at 113–115 and SRT 20 at 154.

## Sender / receiver stats (steady state, per second)

| Run | video Mbps | est. wire Mbps (x1.35) | sender dwell avg / max ms | queue drops | recv dropped_packets / fec_missing / fec_unrecovered / quic_lost | recv max AU gap ms |
| --- | ---: | ---: | --- | --- | --- | ---: |
| 1080p 50M cap 80 | 50 | 67–68 | 14 / 34–41 | 7 at startup, 0 after | 0 / 0 / 0 / 0 | 34 |
| 720p 10M cap 80 | 10 | 13.6 | 1.5 / 10 | 7 at startup, 0 after | 0 / 0 / 0 / 0 | 23 |
| 1080p 100M cap 150 | 100 | 135 | 14.5 / 26–31 | 7 at startup, 0 after | 0 / 0 / 0 / 0 | 26 |
| 1080p 50M cap 400 | 50 | 67 | 1.6 / 6.6 | 0 | 0 / 0 / 0 / 0 | n/a |

"Dwell" is the time from the AU arriving on stdin to KyProto `send()`
returning, so it covers queueing plus pacing. Stats Kyber exposes:
`ProtocolStats{dropped_packets, video_fec_source_symbols,
video_fec_source_symbols_missing, video_fec_source_symbols_unrecovered}` and
`ConnectionStats{rtt, packets_lost}`. There are no byte counters, so wire
Mbps is estimated, not measured.

## Readings

- **Kyber on loopback adds ~25 ms p50 over the pipe at 1080p60 50M (137 vs
  112), and its p95 is ~15 ms wider (151 vs 113).** It still beats SRT
  latency=20 at p50 (137 vs 151), but not at p95 (151–152 vs 153 is a tie).
  At 100M with a 150 cap the overhead is the same (+26 ms).
- **Most of that is the pacer, not QUIC or FEC.** At an 80 Mbps cap, a
  ~140 kB wire-size AU takes ~14 ms to leave, which matches the sender dwell.
  Raising the cap to 400 cut dwell to 1.6 ms, p50 to 129 and p95 to 131, so
  jitter nearly matches the pipe. Those runs were possibly contended, yet
  both repeats were identical. The remaining ~17 ms is about one 60 fps frame
  interval. My guess is that it is decoder/presentation phase, as in the
  720p bimodality below, not transport cost. Plank's budget (video x 1.35 +
  1 Mbps) is a floor for rate, not for latency. On a real link the cap has to
  sit below the path capacity, so this pacing cost is real and grows with AU
  size divided by cap.
- **720p 10M is bimodal between runs: 125 then 107**, where the pipe gave
  107/107. The 18 ms step is one frame, the same one-frame phase
  bimodality seen with VT before (90 vs 107). Sender dwell was 1.5 ms both
  times, so Kyber itself adds ~0–2 ms at low bitrate.
- **Loopback lost nothing.** dropped_packets, fec_missing, fec_unrecovered
  and quic_lost were 0 in every run. FEC repair was never exercised, so
  this measures overhead only. The loss behaviour needs a lossy link or a
  netem-style shaper.
- **Startup is surprising.** VideoToolbox's first IDR is large enough
  (~1.5 MB) that at cap 80 or 150 it spends ~200 ms in the pacer. The 6-AU
  queue then overflows, the sender flushes and waits for the next keyframe,
  and the first picture arrives about 1–2 s after connect. At cap 400 there
  were no startup drops. Real use needs either keyframe-aware pacing
  headroom or an IDR request on connect.
- No run-to-run variance except the 720p one-frame step. The pipe and SRT
  numbers match the morning notes: pipe 112 (107 at 720p), SRT 20 151.

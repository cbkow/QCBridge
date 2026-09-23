# What does putting native-encoded video on the wire cost?

2026-09-22, one Mac, loopback. Run with
`spikes/parity/mux-tax/build.sh && spikes/parity/mux-tax/run_mux_tax.sh <dir> --seconds 30`.

The question behind it: if native capture replaces the ffmpeg capture+encode
leg, and Kyber goes away, is plain SRT still the right carrier for video —
and how should the encoded frames get onto it?

## Method

`qcb-stamp` (`mux-tax/qcb-stamp.swift`) draws the ring1 probe strip into a
1080p60 noise frame and encodes it with VideoToolbox, using the settings
`vt-latency/vtlat.swift` measured. The stamp is written immediately before
the encode submit. With `--mux-url` it also muxes mpegts and opens the SRT
itself (`mux-tax/muxsend.c`, libavformat from QCView's vendored prefix —
the same build the ffmpeg-child rungs use, so "in-process" versus "child"
is genuinely the only difference between them).

Every rung ends at the unmodified `probe_reader.py`, so `lat_ms` means
encode → transport → decode throughout, and the numbers land in the same
`runs.jsonl` shape as the sibling result dirs.

`probe_reader.py` was deliberately **not** touched: the 2026-09-17 baselines
were read with those exact decoder flags, and changing them would forfeit the
comparison. (Its `-flags low_delay` makes the HEVC decoder log "Error
constructing the frame RPS" on this bitstream. The same stream decodes with
zero errors from a file; it is a decoder-flag artifact, constant across
rungs, and no frame failed to score in any run.)

Capture and the hot lane are outside the measurement on purpose — this
isolates the wire. These are not motion-to-photon numbers.

## Results (p50, ~1620 scored frames per rung after a 3 s trim, 0 fails)

| rung | p50 | p95 |
|---|---|---|
| floor — encode + decode, no wire | 41 | 49 |
| one ffmpeg hop, Annex-B over TCP | 63 | 67 |
| mpegts/SRT via ffmpeg child, latency 5 | 90 | 93 |
| mpegts/SRT via ffmpeg child, latency 20 | 102 | 107 |
| mpegts/SRT via ffmpeg child, latency 120 | 201 | 206 |
| **in-process mpegts/SRT, latency 20** | **84** | **88** |
| ffmpeg child sender + host demux + local TCP | 118 | 123 |
| in-process sender + host demux + local TCP | 99 | 105 |

Pairwise, which is what reproduces:

```
sender process hop      18 ms   (102 - 84), and 19 ms (118 - 99)
host demux hop          15 ms   (99 - 84)
SRT latency             0.97 ms per ms of setting
mpegts+SRT over a bare socket   ~39 ms at latency 20, ~27 at latency 5
```

## Findings

**1. SRT's latency setting is 1:1 additive, confirmed five times.** Here,
20 → 120 costs +111 ms across a 115 ms change. In the 2026-09-17 runs, which
nobody re-ran for this: `win-loopback` +100, `win2mac` +103, `mac2win` +104.
TSBPD holds every packet for the configured budget whether or not anything
was lost.

**2. Kyber's measured advantage over SRT was approximately SRT's latency
buffer.** Recomputed from the recorded runs: mac2win 125 vs 156 (−31),
win2mac 83 vs 105 (−22), win-loopback 63 vs 91 (−28), VPN 126–128 vs 147–160
(−21 to −32). Every one of those SRT runs was at latency 20. The residual
after accounting for the buffer matches what mpegts costs over a bare
socket. The pivot's headline number was mostly a tunable and a container.

**3. A process boundary costs ~18 ms, about a frame at 60 — and it is
recoverable.** Measured twice on the sender (18, 19) and once on the host
(15). In-process muxing took SRT@20 from 102 to **84 ms**. `muxsend.c` is
120 lines; this is not an expensive thing to own.

**4. The host fan-out hop should not exist at all.** QCView's
`LiveStreamDecoder` opens `srt://` directly — it needs no agent-side
demux-and-republish. That leg only existed because Kyber carried video
inside the QUIC connection and had to re-expose it locally. Dropping Kyber
deletes the reason for it, and 15 ms with it.

**5. Native encode is worth ~66 ms.** The 2026-09-17 mac pipe baseline with
ffmpeg capture+encode (`mac-pipe-100M`) is 113 ms, same reader, same probe;
the floor here is 41–47. For scale, `win-pipe-nvenc-1080p60` was 43–44 ms,
within noise of this floor — the synthetic source behaves like the real
capture harness.

## The shape this argues for

Native encode → mux in-process → SRT → QCView opens the `srt://` URL.
**84 ms** here, against 118 for the two-ffmpeg-child version. Both hops are
avoidable and together they are 33 ms.

Worth noting: that 84 ms is lower than the 113 ms the old pipeline cost with
**no network at all**. It is not yet an end-to-end claim — ScreenCaptureKit
still has to be added on the front, and S6 measured that as substantial on a
large display — but the encode-and-transport half of the new design is
cheaper than the encode half of the old one.

## Caveats

- **Loopback on one machine**: no loss, jitter or reordering. That is exactly
  the condition under which SRT's buffer looks like pure cost; on a lossy
  link it is what keeps the picture intact, and a QUIC design would pay for
  loss per-frame rather than as a fixed budget. Nothing here measures that.
  The cross-machine rungs in the 2026-09-17 dirs are the ones to re-run
  against a native-capture sender before treating any of this as final.
- **The floor is the least stable rung**: 47 ms in one run, 41 in the next,
  while every wire rung reproduced to ±1 ms. It runs first, on a cold
  machine. Quote the pairwise deltas, not "over the floor".
- **The two-child full path bursts at startup** — p95 of 705 ms before the
  3 s trim, against 123 after. The host ffmpeg buffers and then dumps. The
  trimmed steady state is sound, but a real host leg would need the
  drain-to-live-edge discipline `LiveStreamDecoder` already implements.

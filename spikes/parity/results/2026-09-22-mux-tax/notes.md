# What does putting native-encoded video on the wire cost?

2026-09-22, one Mac, loopback. Run with
`spikes/parity/mux-tax/run_mux_tax.sh <dir> --seconds 30`.

The question behind it: if native capture replaces the ffmpeg capture+encode
leg, and Kyber goes away, is plain SRT still the right carrier for video —
and how should the encoded frames get onto it?

## Method

`qcb-stamp` (new, `mux-tax/qcb-stamp.swift`) draws the ring1 probe strip into
a 1080p60 noise frame and encodes it with VideoToolbox, using the settings
`vt-latency/vtlat.swift` measured. The stamp is written immediately before
the encode submit. Every rung ends at the unmodified `probe_reader.py`, so
`lat_ms` means encode → transport → decode throughout, and the numbers land
in the same `runs.jsonl` shape as the sibling result dirs.

`probe_reader.py` was deliberately **not** touched: the 2026-09-17 baselines
were read with those exact decoder flags, and changing them would forfeit the
comparison. (Its `-flags low_delay` makes the HEVC decoder log "Error
constructing the frame RPS" on this bitstream. The same stream decodes with
zero errors from a file; it is a decoder-flag artifact, constant across
rungs, and no frame failed to score in any run.)

Capture and the hot lane are outside the measurement on purpose — this
isolates the wire. These are not motion-to-photon numbers.

## Results (p50, 1620 scored frames per rung, 0 fails, 0 stale)

| rung | p50 | p95 | over floor |
|---|---|---|---|
| floor — encode + decode, no wire | 47 | 51 | — |
| one ffmpeg hop, Annex-B over TCP | 64 | 67 | +17 |
| + mpegts/SRT, latency 5 | 90 | 94 | +43 |
| + mpegts/SRT, latency 20 | 102 | 107 | +55 |
| + mpegts/SRT, latency 120 | 202 | 207 | +155 |
| full: SRT 20 + host demux + local TCP | 119 | 124 | +72 |

The +72 of the full path decomposes exactly:

```
+17  sender ffmpeg hop  (measured by raw-tcp)
+38  mpegts + SRT at latency 20, over a bare socket
+17  host demux hop     (full - srt-L20)
---
 72
```

## Three findings

**1. SRT's latency setting is 1:1 additive, and that is confirmed four
times.** Here, 20 → 120 costs +100 ms. In the 2026-09-17 runs, which nobody
re-ran for this: `win-loopback` 91 → 191 (+100), `win2mac` 105 → 208 (+103),
`mac2win` 156 → 260 (+104). Four machines and pairings, one answer. TSBPD
holds every packet for the configured budget whether or not anything was
lost.

**2. Kyber's measured advantage over SRT is approximately SRT's latency
buffer.** Recomputed from the recorded runs: mac2win 125 vs 156 (−31),
win2mac 83 vs 105 (−22), win-loopback 63 vs 91 (−28), VPN 126–128 vs 147–160
(−21 to −32). Every one of those SRT runs was at latency 20. Dropping to
latency 5 here bought 12 ms of the ~20, and the residual (~16–18 ms) matches
what mpegts + SRT costs over a bare socket. So the pivot's headline number
was mostly a tunable and a container, not QUIC beating SRT on merit.

**3. Native encode is worth ~66 ms, which dwarfs every transport question.**
The floor here is 47 ms. The 2026-09-17 mac pipe baseline with ffmpeg
capture+encode (`mac-pipe-100M`) is 113 ms, same reader, same probe. That
−66 ms independently confirms the "~65–70 ms ffmpeg penalty" the spike
claimed, and it is transport-independent. For scale, `win-pipe-nvenc-1080p60`
was 43–44 ms, within noise of this floor — the synthetic source behaves like
the real capture harness.

## What this says about the build

- **Each process hop costs ~17 ms**, about a frame at 60. The two-ffmpeg
  option (mux/send on the replica, demux/fan-out on the host) spends 34 ms on
  process boundaries alone. In-process mux and demux (libavformat + libsrt,
  or a hand-rolled mpegts muxer) would buy that back. That is the strongest
  argument found here for not shipping the two-process version permanently.
- **The SRT latency setting is the biggest single lever** and it is a loss
  budget, not overhead: low is free on a clean link and reckless on a bad
  one. Scope is "good connections only", so this is tunable — but it must be
  a setting, not a constant.
- **mpegts + SRT costs ~18 ms beyond a bare socket** even with the buffer
  near zero. That is the only part a different protocol would actually
  reclaim, and it is small next to the 66 ms native capture already won.

## Caveats

Loopback on one machine: no loss, no jitter, no reordering. That is exactly
the condition under which SRT's buffer looks like pure cost — on a lossy link
it is what keeps the picture intact, and a QUIC design would pay for loss
per-frame instead of as a fixed budget. Nothing here measures that. The
cross-machine rungs in the 2026-09-17 dirs are the ones to re-run against a
native-capture sender before treating any of this as final.

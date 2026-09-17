# 2026-09-17 — Windows → Mac over the VPN (synthetic sender, no Blender)

Windows workstation (RTX 5090, NVENC) runs `probe_testsrc.py` (1920x1080 /
60 / 50M, hevc_nvenc) → SRT listener or `kyber-send`; chris's Mac runs the
reader (SRT caller or `kyber-recv` → `probe_reader.py --stdin`, VideoToolbox
decode). VPN RTT 7–11 ms (ping 9.1 avg; Kyber RTT 9.3–10.4). Code at
`6c6edab` (harness) / `9547989` (kyber-pipe). 30 s scored per run. Raw rows:
`runs.jsonl`.

**Clock correction.** Stamps are the Windows clock, the reader is on the Mac
clock. Both sntp tools print server − local, so
`true = raw − (apple − windows) + (apple − mac)`. Windows read +8.7 (15:20
UTC), +8.8 (15:33), +9.3–9.4 (≈16:04) ms; the Mac's own readings are per run
below. Uncertainty ≈ ±6 ms, mostly the Mac's sntp scatter. (An earlier
relay applied this with the wrong sign — corrected here.)

| Label | Transport | Time (UTC) | Raw p50 / p95 / p99 | apple − win | apple − mac | Corrected p50 / p95 / p99 | Frames scored / failed |
| --- | --- | --- | --- | ---: | ---: | --- | --- |
| win2mac-srt120 | SRT latency 120 | 15:29 | 196 / 213 / 214 | +8.8 | +0.3 | **188 / 205 / 206** | 1800 / 0 |
| win2mac-srt20 | SRT latency 20 | 15:31 | 99 / 110 / 111 | +8.8 | +0.45 | **91 / 102 / 103** | 1800 / 0 |
| win2mac-kyber-cap150 | Kyber, cap 150 | 15:59 | 84 / 87 / 89 | +9.3 | −4.9 | **70 / 73 / 75** | 1801 / 0 |
| win2mac-kyber-cap400 | Kyber, cap 400 | 16:02 | 83 / 85 / 86 | +9.3 | −5.0 | **69 / 71 / 72** | 1801 / 0 |

An earlier `win2mac-srt120` attempt at 15:26 is not in `runs.jsonl`: version
mismatch (top vs bottom strip) made the reader decode noise, which led to the
false-lock hardening in `6c6edab`.

## Kyber link stats

| Run | Receiver (Mac) | Sender (Windows) |
| --- | --- | --- |
| cap 150 | 60 fps, 50 Mbps; dropped / fec_missing / fec_unrecovered / quic_lost / holes all 0; max AU gap 20–23 ms (34.5 first second) | dwell 6.7–7.1 ms avg, 13.9 max; 0 queue drops |
| cap 400 | same, all loss counters 0; max AU gap 18–21 ms, one 38.3 ms | 2,028 AUs, 34 keys, ~67.5 Mbps wire; dwell 2.1–2.6 avg, 7.4 max; 0 queue drops; `quic_lost` 0 → 10 once at ~16 s (receiver saw no loss — likely late ACKs declared lost) |

## Readings

- **Kyber over the VPN ≈ 69–70 ms p50, 71–73 p95** from NVENC frame to
  decoded frame on the Mac — vs SRT latency 20 at 91 / 102. Kyber wins by
  ~21 ms p50 and ~30 ms p95, and its tail is much tighter (p99 − p50: 3 ms vs 12).
- **The VPN itself costs little:** Windows loopback was SRT 191 / 91 and
  Kyber 62–66 ms; over the VPN it's 188 / 91 and 69–70 — a few ms, about
  half the RTT, as expected.
- **Cap 400 vs 150 barely matters for NVENC** (1 ms p50): its frames are
  small enough that the pacer rarely holds them. On a Mac sender (bigger
  VideoToolbox frames) the cap matters more.
- **Loss recovery still untested:** zero real loss over this VPN at 67 Mbps
  wire. Needs an impaired link.
- **Budget, Windows replica → Mac host:** ~43 ms encode+decode floor
  (loopback pipe) + ~27 ms Kyber transport and receive ≈ 70 ms. SRT at
  latency 20 adds ~48 ms instead.

# 2026-09-17 — Windows Kyber loopback (synthetic sender, no Blender)

Windows workstation (Threadripper PRO 7955WX, RTX 5090). First Windows build of
`spikes/parity/kyber-pipe` @ 9547989: `cargo build --release` on rustc 1.89.0
(rust-toolchain.toml) **passes** in 23 s; only warnings are 2 dead-code ones in
vendored quinn-proto. `cargo test --release --lib`: 2 passed.

Pipelines under `cmd /c`: `probe_testsrc.py --stdout` (hevc_nvenc 1920x1080 /
60 / 50M, AUD insert) | `kyber-send --listen 127.0.0.1:19997 --token spike --fps
60 --bitrate-cap-mbps N`, and `kyber-recv --connect 127.0.0.1:19997` |
`probe_reader.py --stdin --seconds 20` (d3d11va). Same machine, no clock
correction needed.

| Label | Transport | p50 | p95 | p99 | Frames scored / failed |
| --- | --- | ---: | ---: | ---: | --- |
| win-kyber-loop-cap150 | Kyber cap 150 Mbps | 66 | 70 | 71 | 1203 / 0 |
| win-kyber-loop-cap400 | Kyber cap 400 Mbps | 62 | 64 | 65 | 1203 / 0 |
| (win-pipe-nvenc-1080p60, earlier) | stdout pipe | 43 | 45 | 46 | 903 / 0 |

Sender stats: 0 queue drops in both. Dwell (stdin → send returned) 7.0–7.7 ms
avg / 13–18 ms max at cap 150, 1.8–2.4 ms avg / ≤6 ms max at cap 400. At cap
400, `quic_lost` reached 33 on loopback; the receiver counted fec_missing=28
and fec_unrecovered=0 (FEC repaired them). Cap 150: quic_lost 0, fec_missing 0.

- **Kyber adds 19–23 ms over the pipe on Windows** (Mac loopback: +17).
- **Cap 400 saves ~4 ms over cap 150 on loopback**, about the drop in dwell.
- The `cmd /c` pipes are binary-safe on Windows PowerShell 5.1.

## Windows → Mac over the VPN, Windows sender stats (Mac reader has the latency)

| Run | Session | Sent AUs / keys | Est. wire Mbps | Queue drops | Dwell avg / max ms | QUIC rtt / lost (sender) |
| --- | --- | --- | ---: | ---: | --- | --- |
| win2mac-kyber-cap150 | ~34 s, 1 client (after 6 connects rejected: client sent an all-zero fingerprint) | 2044 / 35 | ~67.5 | 0 | 6.7–7.1 / ≤13.9 | 7.0–8.6 / 0 |
| win2mac-kyber-cap400 | ~35 s, 1 client | 2028 / 34 | ~67.5 | 0 | 2.1–2.6 / ≤7.4 | 8.5–10.0 / 10 (one step at ~16 s) |

At cap 400 the Mac receiver reported 0 dropped / fec_missing / holes, so the
sender's 10 "lost" were probably spurious (late ACKs) rather than real loss.

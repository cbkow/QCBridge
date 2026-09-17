# 2026-09-17 — S5: one Kyber connection (first pass, Mac, one machine)

`qcb-helper` (Rust, `spikes/parity/kyber-pipe/src/bin/qcb-helper.rs`) +
`qcbridge/ring1/transport_kyber.py`, selected with `QCB_TRANSPORT=kyber`.
One QUIC connection per session: video (RaptorQ lane), control, hot (keyed
latest-wins) and cold (credit-acked) lanes; token auth + trust-on-first-use
certificate pinning. The addon talks to its helper over stdin/stdout frames;
no pyzmq on this path. ZMQ stays the default transport.

## Exit criteria

| Criterion | Result |
| --- | --- |
| Transport contract tests (`tests/test_transport_kyber.py`, mirrors the zmq file + TOFU/pin/token cases) | 10 / 10 pass |
| 21-check two-Blender sync smoke suite over Kyber | **21 / 21 pass**, replica clean, no tracebacks; both helpers confirmed in `qcbridge-helper.log` |
| Probe ≤ today's numbers (two Blenders on this Mac, EEVEE, avfoundation 60 fps → VideoToolbox 50M; video through the helpers, read from the host helper's local TCP port) | **217 / 223 ms p50 / p95** (r1), 216 / 223 (r2); 0 failed frames. Same test over ZMQ + SRT latency 60 this morning: 258–279 / 267–288 |
| Bootstrap time, heavy scene (host session start → replica bootstrap applied, loopback, `blender-smoke/bootstrap_bench.sh`) | 8 M verts: ZMQ 1.0 s, Kyber 1.1 s. 40 M verts: ZMQ 1.24 s, Kyber 3.1–3.2 s (2 runs each) |

## Readings

- **Sync is transport-agnostic as designed:** nothing in ring0 changed except
  transport construction and who owns the capture child.
- **Bootstrap:** on loopback ZMQ moves bytes at memory speed; the Kyber path
  (two stdio hops + QUIC crypto) adds ~2 s on a 40 M-vertex scene. Over a
  real link both are network-bound — measure on the VPN before tuning
  (candidates: larger cold chunks, bigger credit window).
- **Found and fixed:** with raw HEVC on a pipe, ffmpeg pads to constant rate
  with duplicate frames; at this Mac's 120 Hz capture the encoder fell behind
  and latency grew 1 s per second (90 s behind after 90 s). `-fps_mode
  passthrough` on the pipe output fixes it (`pixel_path.build_command`).
  Goes away with native capture (S6/S7).
- **Hot lane rides a reliable stream** with sender-side conflation per key:
  Kyber video lanes only flow server → client and there is no raw datagram
  lane (plan: Kyber patch 1). 60 Hz updates delivered 1:1 in the unit test.

## Not done yet

- Windows build + tests of `qcb-helper`; cross-machine run over the VPN
  (sync smoke, probe, bootstrap).
- QCView reading `tcp://127.0.0.1:<port>` (raw Annex-B HEVC). The host
  panel's viewer URL already reports it in Kyber mode; QCView's
  LiveStreamDecoder needs a check that it probes raw HEVC over TCP.
- Host stores the learned replica fingerprint (pref `replica_fingerprint`
  exists in code, no UI/persistence yet).
- Helper discovery for shipped builds (bundled binary under `qcbridge/bin/`).
- S5 design seams still open: helper owning Blender's lifecycle, N peers.

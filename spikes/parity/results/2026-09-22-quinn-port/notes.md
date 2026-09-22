# Kyber → quinn port: acceptance targets, carried forward

Written **before** the port, on purpose. The numbers this port must match were
recorded in `results/2026-09-17-s5-one-connection/notes.md`, which is one of
the seven Kyber-bearing result directories being deleted in this change. They
are quoted here verbatim so the target outlives the data.

Everything below remains recoverable from git history on `spike/parity`
(the directories are removed from the tree, not from the repository).

## Why the transport is changing

The 2026-09-22 mux-tax bench (`results/2026-09-22-mux-tax/`) found Kyber's
measured advantage over SRT was approximately SRT's own latency buffer: every
recorded SRT comparison ran at `latency=20`, and SRT's setting is a ~1:1 add to
glass-to-glass, confirmed across four machine pairings. The wins that held up —
native encode (~66 ms) and removing process hops (~18 ms each) — are
transport-independent. Kyber is a thin layer over quinn, and two of the three
vendored `quinn-proto` patches existed only because Kyber hides Quinn's knobs.

## The S5 exit criteria, as recorded 2026-09-17

| Criterion | Kyber result to match |
| --- | --- |
| Transport contract tests (`tests/test_transport_kyber.py`) | **10 / 10 pass** |
| 21-check two-Blender sync smoke suite | **21 / 21 pass**, replica clean, no tracebacks |
| Probe, two Blenders on one Mac, EEVEE, 60 fps capture | **217 / 223 ms p50/p95** (r1), 216 / 223 (r2), 0 failed frames. ZMQ + SRT latency 60 the same morning: 258–279 / 267–288 |
| Bootstrap, `blender-smoke/bootstrap_bench.sh`, loopback | 8 M verts: ZMQ 1.0 s, Kyber 1.1 s. 40 M verts: ZMQ 1.24 s, Kyber 3.1–3.2 s |

Also recorded there and worth keeping: on loopback ZMQ moves bytes at memory
speed, and the Kyber path (two stdio hops + QUIC crypto) added ~2 s on a 40 M
vertex scene; over a real link both were network-bound (the VPN run measured
441 MB at Kyber 11.6 s = ZMQ 11.65 s). Measure on the VPN before tuning.

## How the comparison changes

**The probe number is not a like-for-like target any more.** 217/223 was
measured with video riding the Kyber connection and read from the host
helper's local TCP port. This port deletes the video lane entirely: the replica
muxes SRT and QCView opens `srt://` directly, which the mux-tax bench showed
removes a ~15 ms host fan-out hop. So the probe must be run with `--srt`, not
`--tcp`, and judged against the mux-tax ladder rather than against 217/223.

**The transport gate is the sync side**, where the comparison is exact:

1. 10 / 10 transport contract tests — the file is being rewritten to drive the
   agent instead of the deleted helper, but all ten assertions stay, including
   keyed-hot, credit-window backpressure on an 8 MB blob, TOFU/pinning, and
   QUIC-level token rejection.
2. 21 / 21 sync smoke.
3. `bootstrap_bench.sh` at 8 M and 40 M against `zmq` as the control, which
   carries no video and is therefore a clean transport A/B.

## Result (2026-09-22, one Mac)

| Criterion | Kyber, 2026-09-17 | quinn, 2026-09-22 |
| --- | --- | --- |
| Transport contract tests | 10 / 10 | **10 / 10** |
| Full Python suite | 85 passed, 2 skipped | **87 passed, 0 skipped** — same tests, both transport suites now actually run |
| 21-check sync smoke | 21 / 21 | **21 / 21**, `verdict.json "pass": true`, exit 0 |
| `cargo build` / `cargo test` | — | clean / 2 passed |

The smoke ran with `QCB_TRANSPORT=agent QCB_AGENT=spawn`, and it was checked
to be the QUIC path rather than a silent fall back to zmq: the spawned
replica agent logged `listening on 127.0.0.1:19990 fingerprint e41cd04e...`
then `host connected`, which it only says once the token is accepted and all
three lanes are open, and the host agent logged `pinned replica certificate`.
`replica_clean` passed, so gaps, apply errors and unknown uuids were all
zero, and `bake_crossed_via_resync` passed, which needs a second bootstrap
mid-session.

Also proved by hand, two agents with no Blender: a wrong token is rejected
**immediately** — five rejections in the four seconds where the old Kyber
path managed one five-second timeout — and reports
`rejected by replica: closed by peer: token rejected (code 1)` instead of the
old `video ready timeout (token rejected?)`. A wrong pin fails the TLS
handshake with `replica certificate changed: pinned ..., got ...`.

**Not run yet:** `bootstrap_bench.sh` at 8 M and 40 M verts against zmq, and
everything cross-machine. Those are the remaining gates.

## Salvaged from the deleted kyber-pipe README

Two things in it were statements about the design rather than about Kyber, so
they outlive the crate:

- **The TLS server name is not verified** — the certificate fingerprint is
  what pins the peer. Still true of `TofuVerifier` after the port: it ignores
  `_server_name` and compares SHA-256 over the end-entity DER. `"localhost"`
  is passed only because a name is required.
- **What a paced media lane costs**, if one is ever built again: pacing per
  datagram at the cap means every access unit waits roughly
  `AU size x 1.35 / cap` before it is fully on the wire, so keyframes wait
  longest, and the first VideoToolbox IDR was large enough to overflow the
  queue — a new client usually started at the *second* keyframe, 1–2 s in.
  That is the behaviour `FixedRateController` (kept in `agent/src/lib.rs`)
  was shaped around.

The rest of that README was about vendoring `quinn-proto` to avoid a 4.1 GB
submodule checkout, and about AGPL obligations. Both are moot.

## Baselines that survive in the tree

`win-loopback` (SRT 20 → 120 costing 91 → 191 ms, plus the NVENC pipe numbers),
`mac-loopback`, `mac-vt-latency`, `mac-blender-loopback`, `s6-native-mac`, and
`mux-tax`. The SRT 1:1 finding stays recomputable from `win-loopback` alone.

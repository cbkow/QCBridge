# QCBridge: one QUIC connection + native capture/encode plan

Committed 2026-09-17 (chris). The living, commentable version is the Claude Doc
"QCBridge — Responsiveness Parity Exploration"
(https://claude.ai/code/artifact/e1d99609-52ad-49e1-bd70-d2949709c00b), section
"Committed plan". This file is the repo copy for whoever picks up the work.

**Status 2026-09-23:** done and merged to `main`. What followed — the audit
of the sync, its measurements and the rework — is in `SYNC-AUDIT.md`,
`COVERAGE.md` and `CACHES.md` at the repo root.

## Decision

QCBridge moves to **one QUIC connection** for sync, and to **native capture
+ encode on both platforms**. A Rust agent on each machine runs both. ffmpeg
leaves the send side entirely. QCView keeps libavcodec for decode.

**Amended 2026-09-22 (chris):** the transport is plain `quinn`, not Kyber,
and **video does not ride the connection** — the replica sends SRT and QCView
opens the `srt://` URL itself. See
`results/2026-09-22-mux-tax/` for the measurements that decided it and
`results/2026-09-22-quinn-port/` for what the port had to match.

Scope decisions (chris):
- **Good connections only.** Lossy or outlier links are out of scope; the
  workflow already depends on a good connection.
- **8-bit is fine** for this phase.
- **Feel is confirmed by eye.** No further viewing test is needed.

## Evidence (all on branch `spike/parity`, `spikes/parity/results/`)

| Finding | Number | Where |
| --- | --- | --- |
| SRT's latency setting is a ~1:1 add to glass-to-glass | 20 → 120 costs +100 ms, on four independent pairings | `2026-09-22-mux-tax/`, `2026-09-17-win-loopback/` |
| A process hop costs about a frame | ~18 ms; in-process mux took SRT@20 from 102 to 84 ms | `2026-09-22-mux-tax/` |
| Native VideoToolbox HEVC 1080p60 / 4K60 | ~5 / ~9 ms p50 | `2026-09-17-mac-vt-latency/` |
| ffmpeg `hevc_videotoolbox` on the same Mac | ~100+ ms | same |
| NVENC via ffmpeg encode + decode floor | 43 ms (1080p60) | `2026-09-17-win-loopback/` |
| Native encode is worth ~66 ms against the ffmpeg pipe baseline | 113 → 47 ms, same reader and probe | `2026-09-22-mux-tax/` |

## Architecture

```
Host Blender addon <-local socket-> Host agent (Rust+quinn) <==QUIC: control/hot/cold==> Replica agent (Rust+quinn) <-local socket-> Replica Blender addon
                                                                                                     |
                                                                                   native capture + encode (SCK/VT | DDA-WGC/NVENC)
                                                                                                     |
                                                        QCView (libavcodec) <================ SRT ===+
```

- **Addon ↔ agent:** length-prefixed frames over a loopback socket, through
  `qcbridge/ring1/transport_agent.py`, behind the existing
  `qcbridge/ring1/transport.py` interface (HostTransport / ReplicaTransport).
  The addon never learns which protocol the agent speaks, which is why the
  Kyber → quinn change did not touch it beyond its name. zmq stays confined
  to `transport_zmq.py` and is **frozen**: supported, not extended.
- **QCView:** opens the replica's `srt://` URL directly. There is no
  host-side re-serve; the mux-tax bench measured that leg at ~15 ms of pure
  cost, and `LiveStreamDecoder` already handles SRT, reconnect and
  drain-to-live-edge.

| QCBridge channel | ZMQ (frozen) | quinn lane |
| --- | --- | --- |
| Control request/reply + heartbeats | DEALER/ROUTER | bidi stream, host ⇄ replica |
| Hot state (last value wins) | PUB/SUB conflated | uni stream, sender-side conflation. Quinn has datagrams, so a real unreliable lane is available whenever it is wanted — parity first |
| Cold sync blobs | ordered | uni stream, seq-numbered, credit-acked |
| Video | ffmpeg → SRT | **not on the connection**: ffmpeg → SRT → QCView |

## Phases

| Phase | Deliverable | Exit criteria |
| --- | --- | --- |
| **S5 One connection** (DONE over Kyber 2026-09-17, re-landed on quinn 2026-09-22 — `results/2026-09-22-quinn-port/`; open: Windows build of the agent, cross-machine VPN runs) | Agent with all lanes; `transport_agent.py`; addon↔agent framing | 21-check sync smoke suite passes over the agent transport (`smokes/`); 10/10 transport contract tests; bootstrap time on a large .blend vs ZMQ |
| **S6 Native Mac capture + encode** | ScreenCaptureKit (x420 IOSurface, minimumFrameInterval 0, queueDepth 3) → VTCompressionSession (RealTime, AllowFrameReordering=NO, PrioritizeEncodingSpeedOverQuality, matched color tags, long GOP, DataRateLimits 2×); max 3 frames in flight, skip don't queue | Mac replica probe within ~15 ms of a Windows replica |
| **S7 Native Windows capture + encode** | Desktop Duplication or Windows.Graphics.Capture (D3D11) → NVENC SDK: preset P1, CBR with one-frame VBV, zero reorder delay, infinite GOP + IDR on request, split-frame encoding auto at 4K, latest-wins slot | Encode p95 ≤ ~15 ms at 4K60; no periodic-keyframe gaps |
| **S8 Replica + viewer latency** | Capture the viewport region at stream resolution; apply hot state on arrival (not the 15 ms tick); raise replica redraw rate; QCView newest-frame pacer; converged indicator from sequence labels; pristine keyframe on settle | Motion → decoded frame ~70–85 ms (estimate) |

Plank references for S6/S7:
- **macOS:** `apps/host/macos/media/screen-capture.m`, `native-video.m`.
- **Linux NVENC:** `apps/host/linux/src/nvenc/nvenc_base.cpp`.
- **Measurements:** `docs/hardware/rocky-hardware-test-host-2026-08-19.md` and
  `docs/development/investigations/macos-*`.
- **Repo:** github.com/instinctual/plank. Prior art for capture and encode
  only — check the licence before copying anything. QCBridge no longer
  depends on it.

## The QCBridge Agent (added 2026-09-18, chris: "make it part of the plan")

The per-machine helper becomes a **tray app** (macOS menu bar / Windows
system tray): one binary, both platforms, both roles, running at login.
It owns everything that isn't scene reading; the Blender addon stays the
only thing that understands Blender data and shrinks to deltas + a panel.

**Replica, set and forget:** agent listens with no Blender running (GPU
idle) → a paired host connects → agent launches Blender (`--python`
startup script: enable addon, start replica session, kiosk, chosen Blender
version) → host goodbye / gone N min → agent closes Blender → crash →
relaunch, host re-bootstraps → version mismatch → host offers the addon
zip over the connection, agent installs (`install-file` keeps prefs) and
restarts. Capture + encode live in the agent (S6/S7): on macOS the Screen
Recording permission attaches to the agent bundle, granted once.

**Host:** addon still runs in Blender; agent holds pairing + pinned
fingerprint, the connection, the local stream port for QCView, "Open in
QCView", status/stats. Sync-only mode unchanged.

**Addon ↔ agent:** the S5 stdio framing over a local socket (agent is
already running). **Constraints:** Windows agent runs in the logged-in
user's session (logon task, not a service); macOS login item, signed +
notarized (same pipeline as QCView); idle policy = close on goodbye or
keep warm N min (Cycles kernels + bootstrap reload cost). Never touches
the project tree. UI: `tray-icon` + `muda`, minimal settings (pairing
token, fingerprint confirm, port, Blender path, idle timeout).

**Sequence (closes S5):** (1) agent crate: tray, settings, listener,
Blender launch/close, local socket — **DONE 2026-09-19 (045f69c)** → (2) host
role: pairing, connection, QCView hand-off — **DONE 2026-09-19 (e1808c6)**;
QCView auto-detects the agent's raw HEVC `tcp://127.0.0.1:19997` → (3) remote
addon update (pairing-gated; last, it's the security-sensitive piece). Still
open before (3): Windows build + logon-task autostart, packaging/signing,
bundling the agent with the addon, agent-config UI (token/peer currently in
agent.toml AND addon prefs). Then S6/S7 put capture inside the agent.
Resist scope creep: lifecycle + transport only.

## S5 design requirements (build the seams now, features later)

S5 must leave room for the capabilities below, even though it ships none of
them:

1. **Hot lane = keyed latest-wins map**, not a single camera slot. Key =
   property path (`obj.matrix`, `light.energy`); the replica applies the
   newest value per key. Enables live in-progress drags.
2. **Generic request/reply RPC on the reliable lane**, replica answers only —
   never initiates. Used by health reporting, supervision and still capture.
   **Requests originate in host Blender, never in QCView.**
3. **Peer model is not hardcoded to one replica.** Per-peer dirty-set `sent`
   tracking, epoch and bootstrap state, so several replicas remain possible
   later (shelved, see below).
4. **The helper owns replica Blender's lifecycle** (launch, kiosk, restart,
   later addon update). On Windows it must run in the interactive user
   session, not a service in session 0, or GUI + capture fail.
5. **TLS pairing with trust-on-first-use fingerprints.** Pairing creates each
   machine's certificate once and shows its fingerprint for confirmation. No
   pin-or-error as in the spike.

## Feature candidates after S8 (agreed worth building)

| Feature | What it is | Notes |
| --- | --- | --- |
| **Live in-progress drags** | Host streams what is being manipulated at 60 Hz on the hot lane; the reliable tier-1 delta on release stays the source of truth | Today a drag crosses at ~20 Hz via the 50 ms flush tick (`host_handlers.py:32`). Worth up to ~50 ms plus smoothness |
| **Encryption of sync traffic** | TLS + pinned cert for control/hot/cold, not just the video | Today ZMQ is token-auth only, unencrypted — scene data protected by the VPN alone. Client-IP relevant |
| **Replica supervision** | Helper launches/restarts Blender, exits kiosk without a keyboard, pushes addon updates (`install-file` keeps prefs), reports GPU/VRAM/encoder health to the host panel | Ends the "check the PC's version first" failure class. Remote update is an RCE surface: paired host only (cert+token), consider signed packages |
| **Pristine still on demand** | On settle (or a host-panel button) the replica renders at viewport resolution, writes a float EXR to its **temp dir** (never the project tree) and sends it over the reliable lane; QCView shows it beside the live stream | Python cannot read the Cycles viewport buffer (stage-0 finding), so this is a real render: seconds on Cycles. Makes the 8-bit stream a non-issue for exposure judgment |
| **Remote F12 render** | Same mechanism at production settings, progress over the stream, results land in QCView only | Bigger scope; decide separately |

## Shelved (revisit, do not design away)

- **Several replicas from one host** (live A/B between EEVEE/Cycles, rig
  variants, two cameras). Chris: likely part of a larger QCView upgrade that
  is not roadmapped yet. Keep requirement 3 above so it stays possible. It
  would also need a replica-local override layer ("replica profile": engine,
  samples, view layer) that sync skips and re-asserts, like the camera-view
  re-assert in `replica_apply`.
- **Read-only remote viewers** (a supervisor watching the same stream).
  Easy with the fan-out, same QCView upgrade question.

## Ruled out (chris, 2026-09-17)

- **QCView controlling Blender in any way** — click-to-select, focus-at-click,
  pixel queries driven from the viewer. Too close to remote desktop, and then
  the viewer has no reason to exist. The viewer receives; it never drives.
- **Forwarding raw keyboard/mouse to the replica** — same reason.
- **Transporting missing media/caches over the wire** — the assumption that
  all media is available on both machines still holds.

## What the quinn port retired

Four of the six patches this plan once wanted from Kyber were only needed
because Kyber hid Quinn:

- a **raw latest-wins datagram lane** for hot state — quinn has bidirectional
  datagrams; the lane is available whenever parity has been proven;
- **keyframe / reference-invalidation requests** — ours to define now;
- a **tunable FEC ratio and receiver deadline** (Kyber fixed them at 30 % and
  50 ms) — moot with video off the connection;
- a **receiver RTT stat** that read a constant — `quinn::Connection::stats()`
  reports the real one.

The vendored `quinn-proto` went with them: its DATAGRAM send-buffer fix is
upstream in 0.11.18, which is what Cargo now resolves.

## Carry-overs and risks

- **Land on `main` now:** the macOS ffmpeg hang fix (`pixel_path.py` reaper,
  `70c0fa8` on the spike branch). It protects current users until S6 replaces
  that path.
- **Windows vendor coverage:** NVENC-native means NVIDIA only until AMF or
  QSV exists. Accepted.
- **Transport ownership:** the lane protocol is ours now. Nobody else fixes
  a bug in it, and nothing else exercises it — the contract tests in
  `tests/test_transport_agent.py` are the only thing standing between a
  regression and a silent one.

## Tools already built (reuse them)

- `probe_testsrc.py` / `probe_reader.py` plus `qcbridge/ring1/probe.py`: the
  motion-to-decode probe, which works in every OS pairing. **It's the
  acceptance tool for every phase.**
- `blender-smoke/`: two-Blender probe runs; `QCB_SMOKE_*` settings are
  documented in the script docstrings.
- `vt-latency/vtlat.swift`: native VideoToolbox timing probe.

## Working setup

- **Cross-machine tests:** this Mac and a Windows Claude session share the
  repo on branch `spike/parity`. chris relays messages between them. Both may
  push to `spike/parity` only.
- **Addresses:** Windows 192.168.40.199, Mac 192.168.80.2 (UDP VPN, Mac
  wired).
- **Ports:** 19990+ for tests; chris's live sessions use the defaults.
- **Clock correction for cross-machine synthetic runs:** sntp and w32tm print
  server − local, so true = raw − (apple − sender) + (apple − reader). Blender
  runs need no correction (host stamps, host reads).

# QCBridge: Kyber + native capture/encode plan

Committed 2026-09-17 (chris). The living, commentable version is the Claude Doc
"QCBridge — Responsiveness Parity Exploration"
(https://claude.ai/code/artifact/e1d99609-52ad-49e1-bd70-d2949709c00b), section
"Committed plan". This file is the repo copy for whoever picks up the work.

## Decision

QCBridge moves to **one Kyber (QUIC) connection** for everything, and to
**native capture + encode on both platforms**. A Rust helper process on each
machine runs both. ffmpeg leaves the send side entirely. QCView keeps
libavcodec for decode.

Scope decisions (chris):
- **Good connections only.** Lossy or outlier links are out of scope; the
  workflow already depends on a good connection.
- **8-bit is fine** for this phase.
- **Feel is confirmed by eye.** No further viewing test is needed.
- **Kyber may be patched.** We maintain the fork.

## Evidence (all on branch `spike/parity`, `spikes/parity/results/`)

| Finding | Number | Where |
| --- | --- | --- |
| Blender over VPN, Mac host → Windows 4K replica, host motion → decoded frame | Kyber cap 400 ~127 / 132 ms p50/p95; SRT latency 20 ~153 / 164 | `2026-09-17-blender-mac-host-win-replica/` |
| Synthetic Win → Mac over VPN (clock-corrected) | Kyber 69 / 71; SRT 20 91 / 102; SRT 120 188 / 205 | `2026-09-17-win2mac/` |
| Synthetic Mac → Win over VPN | Kyber 135 / 137; SRT 20 165 / 173 | `2026-09-17-mac2win/` |
| Native VideoToolbox HEVC 1080p60 / 4K60 | ~5 / ~9 ms p50 | `2026-09-17-mac-vt-latency/` |
| ffmpeg `hevc_videotoolbox` on the same Mac | ~100+ ms | same |
| NVENC via ffmpeg encode + decode floor | 43 ms (1080p60) | `2026-09-17-win-loopback/` |
| Budget of the ~127 ms (Kyber, Windows replica) | replica ~58, encode/decode ~43, transport ~26 | Blender notes |
| Kyber builds and runs on macOS arm64 and Windows MSVC | Rust 1.89 | `kyber-pipe/`, `win-kyber-loopback/` |

## Architecture

```
Host Blender addon <-stdio frames-> Host helper (Rust+Kyber) <==QUIC: control/hot/cold/video==> Replica helper (Rust+Kyber) <-stdio frames-> Replica Blender addon
                                          |                                                          |
                                   HEVC on localhost -> QCView (libavcodec)            native capture + encode (SCK/VT | DDA-WGC/NVENC)
```

- **Addon ↔ helper:** stdin/stdout framing through a new
  `qcbridge/ring1/transport_kyber.py`, behind the existing
  `qcbridge/ring1/transport.py` interface (83 lines: HostTransport /
  ReplicaTransport). zmq is confined to `transport_zmq.py` today, so the swap
  is contained. pyzmq and the per-Python wheel matrix go away.
- **QCView:** reads HEVC from the host helper over localhost and does **not**
  link Kyber. That keeps AGPL out of QCView.

| QCBridge channel | Today | Kyber lane |
| --- | --- | --- |
| Control request/reply + heartbeats | ZMQ DEALER/ROUTER | Reliable data |
| Hot state (last value wins) | ZMQ PUB/SUB conflated | Unreliable latest-wins (patch: raw datagram lane) |
| Cold sync blobs | ZMQ ordered | Reliable data, second endpoint |
| Video | ffmpeg → SRT | Video lane with loss recovery (RaptorQ) |
| Viewer back-channel (new) | none | Reliable data: keyframe on join, hold, bitrate |

## Phases

| Phase | Deliverable | Exit criteria |
| --- | --- | --- |
| **S5 One connection** (transport DONE on Mac + over the VPN; closes with the Agent, above with a Windows replica — `results/2026-09-17-s5-one-connection/`, `results/2026-09-17-s5-vpn/`; open: Windows-side notes, replica-unreachable-after-hard-kill incident, QCView tcp:// intake, fingerprint UI) | Helper with all lanes (video still fed by ffmpeg, as in `kyber-pipe`); `transport_kyber.py`; addon↔helper framing | 21-check sync smoke suite passes over Kyber (scripts in the project memory dir `smoke-harness/`); probe ≤ today's Kyber numbers; bootstrap time on a large .blend vs ZMQ |
| **S6 Native Mac capture + encode** | ScreenCaptureKit (x420 IOSurface, minimumFrameInterval 0, queueDepth 3) → VTCompressionSession (RealTime, AllowFrameReordering=NO, PrioritizeEncodingSpeedOverQuality, matched color tags, long GOP, DataRateLimits 2×); max 3 frames in flight, skip don't queue | Mac replica probe within ~15 ms of a Windows replica |
| **S7 Native Windows capture + encode** | Desktop Duplication or Windows.Graphics.Capture (D3D11) → NVENC SDK: preset P1, CBR with one-frame VBV, zero reorder delay, infinite GOP + IDR on request, split-frame encoding auto at 4K, latest-wins slot | Encode p95 ≤ ~15 ms at 4K60; no periodic-keyframe gaps |
| **S8 Replica + viewer latency** | Capture the viewport region at stream resolution; apply hot state on arrival (not the 15 ms tick); raise replica redraw rate; QCView newest-frame pacer; converged indicator from sequence labels; pristine keyframe on settle | Motion → decoded frame ~70–85 ms (estimate) |

Plank references for S6/S7:
- **macOS:** `apps/host/macos/media/screen-capture.m`, `native-video.m`.
- **Linux NVENC:** `apps/host/linux/src/nvenc/nvenc_base.cpp`.
- **Measurements:** `docs/hardware/rocky-hardware-test-host-2026-08-19.md` and
  `docs/development/investigations/macos-*`.
- **Repo:** github.com/instinctual/plank. Don't copy code without checking its
  license; the transport boundary is AGPL.

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
Blender launch/close, local socket → (2) host role: pairing, connection,
QCView hand-off → (3) remote addon update (pairing-gated; last, it's the
security-sensitive piece). Then S6/S7 put capture inside the agent.
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

## Kyber patches (fork of plank-kymux @ 912ece5), as phases need them

1. **Raw latest-wins datagram lane** for hot state (S5). kyproto's router
   drops unknown datagram ids.
2. **Per-frame metadata:** sync sequence + host hot timestamp (the "one
   clock" feature) (S5).
3. **Keyframe / reference-invalidation requests** on the control lane
   (S5–S7). Kyber has none.
4. **Sender pacing:** no app-level stall on keyframes, larger QUIC window.
   Plank measured keyframe send going from 81 to 25 ms (S5).
5. **Tunable error-correction ratio (fixed 30%) and receiver deadline
   (fixed 50 ms)** (S8).
6. **Receiver RTT stat:** it reads a constant value, fix it (S5).

Notes:
- quinn-proto 0.11.17 is vendored from Plank (datagram send-buffer fix) in
  `kyber-pipe/vendor/`.
- A git dependency on Plank pulls 4 GB of submodules; don't do that.

## AGPL compliance checklist (practical reading, not legal advice)

- [ ] Helper source (our code, Kyber patches, build scripts) public and tagged for every released binary
- [ ] License files + SPDX headers kept; THIRD_PARTY_NOTICES updated
- [ ] Helper reports its source URL (version output + handshake); addon and QCView "about" show it (AGPL §13, since Kyber is modified)
- [ ] QCView does not link Kyber (localhost handoff)
- [ ] Upstream contributions only after reviewing Kyber's CLA

## Carry-overs and risks

- **Land on `main` now:** the macOS ffmpeg hang fix (`pixel_path.py` reaper,
  `70c0fa8` on the spike branch). It protects current users until S6 replaces
  that path.
- **Windows vendor coverage:** NVENC-native means NVIDIA only until AMF or
  QSV exists. Accepted.
- **Kyber maturity:** young project, company focused on robotics. We pin and
  maintain a fork.

## Tools already built (reuse them)

- `probe_testsrc.py` / `probe_reader.py` plus `qcbridge/ring1/probe.py`: the
  motion-to-decode probe, which works in every OS pairing. **It's the
  acceptance tool for every phase.**
- `blender-smoke/`: two-Blender probe runs; `QCB_SMOKE_*` settings are
  documented in the script docstrings.
- `kyber-pipe/`: working Kyber sender/receiver, the starting point for the
  helper.
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

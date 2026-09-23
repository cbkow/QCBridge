# QCBridge

A remote beauty window for Blender compatible with Windows and macOS. You work in Blender on your own machine; a second machine with a bigger GPU mirrors your scene and runs the full Cycles/Eevee preview. 

It's one extension with two roles. Install it on both machines, set one to **Host** and one to **Replica**, connect over your LAN or VPN, and start a session on each end. From there it's hands-off: the replica loads whatever file the host has open, follows your camera, timeline, and edits — lighting, materials, node tweaks arrive in under a second; modeling changes in a couple — and reloads nothing along the way. Sync is strictly one-way; the replica never writes anything back, and never touches your project files.

Viewing works two ways. The built-in stream sends the replica's viewport over SRT (HEVC 10-bit at a fixed bitrate) into [QCView](https://github.com/cbkow/QCView-Player) — one click on the host's **Open in QCView** button and the live render appears as a media item, ready for A/B comparison against approved renders. Or skip streaming entirely and look at the replica through Parsec, Jump, or any remote desktop you already use; the sync works the same either way.

A few things worth knowing about:

- **Shot Mode** locks the replica to the camera frame — fitted, matted in black, holding steady while you orbit around your scene freely. That framing matches your render output exactly, which is what makes clean A/B wipes possible in QCView.
- **Path mapping** translates file paths between platforms (a table of Windows ↔ macOS roots in preferences), so a Mac host and a Windows replica can share one project on network storage.
- The replica runs in a **kiosk mode** — a clean, chrome-free fullscreen viewport — and manages its own lifecycle: it drops to an idle viewport when you end a session, and picks the next one up without anyone touching that machine.
- Settings survive updates and reinstalls; ffmpeg is provided by QCView 2.2.4 or later, and status is always visible — burned into the stream itself and reported on the host's panel.

---

## What is on `main` now, ahead of the next release

Since 0.1.6 the connection has moved out of Blender into a small **agent**
— a tray app (Rust) on each machine that owns the QUIC session, finds
peers, and takes settings at runtime — and the sync between the two
Blenders was audited and reworked end to end. The short version, measured
on macOS (both roles on one machine, `smokes/bench_latency.sh`):

- an edit reaches the replica in ~105 ms, a camera move in ~30 ms, and a
  small edit no longer waits behind a large one (tier-1 deltas ride their
  own lane);
- 120 of 122 surveyed user actions reach the replica (`COVERAGE.md`),
  including shader-node settings, visibility and instancing, view layers
  and markers (through an automatic bootstrap), NLA, particles, force
  fields, geometry-nodes bakes, and linked libraries;
- the replica recovers on its own: a restarted replica is re-bootstrapped,
  a dropped frame triggers a resync, and edits made *on* the replica are
  reported to the host;
- simulation caches can share a **cache root** on the mapped volume so a
  bake on the host is a bake on the replica with no Force Resync
  (`CACHES.md`);
- paths from either OS are mapped, and what cannot be resolved is counted
  and shown, never silent.

How it fits together, as it is now: `SYNC-AUDIT.md` (§2 for the
transport), `COVERAGE.md`, `CACHES.md`, and `smokes/README.md` for the
suites that prove it. `ARCHITECTURE.md` describes the design before this
work and says so at its top. Running it from a checkout: build the agent
(`cargo build --release` in `agent/`); the addon finds the binary, or set
`QCB_TRANSPORT=agent QCB_AGENT=spawn` to have each Blender start a private
one. The zmq transport from 0.1.6 remains as a fallback.

None of the agent line has run on Windows yet. That verification — and the
coordinated release with QCView — is tracked in the QCBridgeAE repo's
`lab/` (start at `WINDOWS-SESSION.md` there).

---

**Requirements:** Blender 4.5 is the manifest minimum; the agent line was
developed and tested on Blender 5.2 LTS, and 4.5 has not been exercised
since. Both machines on the same network; it works over most VPNs.

**Note:** 0.1.6 was tested with macOS as the host and Windows as the replica. The agent line has only been exercised on macOS so far. This is still very much a WIP experiment and was developed out of a need for a specific project.

---

Licensed GPL-3.0-or-later ([LICENSE](LICENSE)); see [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) and [Acknowledgments.md](Acknowledgments.md) for the components and projects this builds on.

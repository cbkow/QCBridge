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

**Requirements:** Blender 4.5+ on both machines that live on the same network. It works over most VPNs.

**Note:** I have only tested with macOS as the host and Windows as the replica. This is still very much a WIP experiment and was developed out of a need for a specific project.

---

Licensed GPL-3.0-or-later ([LICENSE](LICENSE)); see [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) and [Acknowledgments.md](Acknowledgments.md) for the components and projects this builds on.

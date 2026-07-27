# QCBridge

A remote beauty window for Blender. You work in Blender on your own machine; a second machine with a bigger GPU mirrors your scene and runs the interactive Cycles viewport at full quality, streaming the converged image back to you live. Edits catch up in about a second — the point isn't zero latency, it's watching a path-traced render of your scene refine on hardware that doesn't fit under your desk, while your local Blender stays perfectly responsive.

It's one extension with two roles. Install it on both machines, set one to **Host** and one to **Replica**, connect over your VPN, and start a session on each end. From there it's hands-off: the replica loads whatever file the host has open, follows your camera, timeline, and edits — lighting, materials, node tweaks arrive in under a second; modeling changes in a couple — and reloads nothing along the way. Sync is strictly one-way; the replica never writes anything back, and never touches your project files.

Viewing works two ways. The built-in stream sends the replica's viewport over SRT (HEVC 10-bit at a fixed bitrate) into [QCView](https://github.com/cbkow/QCView-Player) — one click on the host's **Open in QCView** button and the live render appears as a media item, ready for A/B comparison against approved renders. Or skip streaming entirely and just look at the replica through Parsec, Jump, or any remote desktop you already use; the sync works the same either way.

A few things worth knowing about:

- **Shot Mode** locks the replica to the camera frame — fitted, matted in black, holding steady while you orbit around your scene freely. That framing matches your render output exactly, which is what makes clean A/B wipes possible in QCView.
- **Path mapping** translates file paths between platforms (a table of Windows ↔ macOS roots in preferences), so a Mac host and a Windows replica can share one project on network storage.
- The replica runs in a **kiosk mode** — a clean, chrome-free fullscreen viewport — and manages its own lifecycle: it drops to an idle viewport when you end a session, and picks the next one up without anyone touching that machine.
- Settings survive updates and reinstalls, ffmpeg is auto-detected (QCView provides one), and status is always visible — burned into the stream itself and reported on the host's panel.

**Requirements:** Blender 4.5+ on both machines, a VPN between them, and — only if you use the stream — an ffmpeg with libsrt on the replica. Build and install with Blender's extension tooling:

```
blender --command extension build --source-dir qcbridge --output-dir dist
blender --command extension install-file -r user_default -e dist/qcbridge-*.zip
```

More documentation is on its way. Until then, the full design history — the decision log, protocol design, and research this grew out of — lives in the incubation repo, [QCview-BlenderAddon](https://github.com/cbkow/QCview-BlenderAddon).

Licensed GPL-3.0-or-later ([LICENSE](LICENSE)); see [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) and [Acknowledgments.md](Acknowledgments.md) for the components and projects this builds on.

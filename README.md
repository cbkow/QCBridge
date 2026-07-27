# QCBridge

**A remote "beauty window" for Blender.** Work in Blender on your workstation; a second machine with bigger GPUs mirrors your scene one-way and runs the interactive Cycles viewport at full quality; the converged image streams back over SRT into [QCView](https://github.com/cbkow/QCView-Player) — color-managed, on its own monitor, catching up to your edits in seconds.

One Blender extension, two roles (**Host** / **Replica**), selected in preferences. Sync is strictly single-writer and one-way: the Host is the only author; the Replica renders and streams, and never talks back about the scene.

## What it does

- **Tiered one-way sync** — camera/frame at 30 Hz (hot channel), property deltas for the look-dev set (transforms, lights, node sockets, color management, visibility), datablock resends via Blender's own serializer for anything structural, and full wire bootstrap: the replica always mirrors whatever file the host has open. No shared storage is ever written; the addon never touches the project tree.
- **Cross-platform path mapping** — a prefix-pair table (Windows ↔ macOS roots) translates path-bearing data on the wire, so a Mac host and a Windows replica share one project.
- **Shot Mode** — one toggle locks the replica to the camera frame, fitted to the encode canvas with an opaque passepartout, ignoring your navigation while the timeline still follows: the pixel-alignable QC surface for A/B wipe/difference against approved renders in QCView.
- **Zero-config streaming** — the replica generates its own SRT listen URL; the handshake carries the stream descriptor; the host panel's **Open in QCView** launches the viewer on the live stream via deep link. The SRT passphrase is derived from the session token — one secret, never on the wire.
- **Two deployment modes** — *full* (sync + SRT stream into QCView, the QC judgment surface) or *sync-only* (disable the pixel stream and view the replica through the remote-desktop tool you already run). The encoder is stream-on-demand either way: ~0.2% CPU while nobody watches.
- **An honest status surface** — sync state is burned into the pixels (`● live · seq N · clock`), the host panel reports the replica's health and encoder state, and failures name themselves instead of hanging.

## Stream vs. remote desktop — which, when

Remote desktop shows you the *machine*; the QCView stream shows you the *image*:

| | RDP / Parsec / Jump | QCView stream |
|---|---|---|
| Quality | Adaptive 8-bit 4:2:0 — silently degrades | Fixed contract: HEVC 10-bit @ constant bitrate — fails *visibly*, never quietly |
| "Is that artifact mine?" | Can't tell (codec vs. render) | Yes |
| Comparison | None | A/B wipe/difference vs. approved renders, aligned by Shot Mode |
| Input surface | Full control of the replica | None — one-way by construction |
| Latency | ~instant | ~150–300 ms — irrelevant for a converging render |

Use remote desktop to *manage* the replica box (and as the casual viewer in sync-only mode); open the QCView stream when the pixels are the point.

## Requirements

- Blender 4.5+ on both machines (5.x is the working target); install the extension zip on both, pick roles in preferences.
- A VPN (WireGuard-class) between the machines; the host dials the replica's tunnel IP.
- For streaming: an `ffmpeg` binary on the replica with libsrt (+ NVENC on Windows). Auto-detected — an explicit path in preferences, then [QCView](https://github.com/cbkow/QCView-Player)'s bundled ffmpeg (`toolbox.json`), then `PATH`. Sync-only mode needs no ffmpeg at all.

## Building

```
blender --command extension build --source-dir qcbridge --output-dir dist
blender --command extension install-file -r user_default -e dist/qcbridge-*.zip
```

Development tests (no Blender required — the sync logic is deliberately bpy-free):

```
python3 -m venv .venv && .venv/bin/pip install pytest pyzmq
.venv/bin/python -m pytest tests/
```

## Design history

QCBridge was designed and built in the open in its incubation repo — [QCview-BlenderAddon](https://github.com/cbkow/QCview-BlenderAddon) — which remains the archive of the full decision log (ADR-style, decisions #1–#17), the sync-protocol and streaming-pipeline design docs, the stage-0 capture spikes, and the research that shaped this architecture. Read that repo's `docs/decisions.md` to understand *why* anything here is the way it is.

## License

GPL-3.0-or-later (see [LICENSE](LICENSE)) — required for Blender addons; matches the QCView ecosystem. Third-party components: [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt). Credits: [Acknowledgments.md](Acknowledgments.md).

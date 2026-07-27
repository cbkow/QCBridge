# Acknowledgments

QCBridge contains no code from other addons, but it stands on ideas and
lessons from projects that walked this ground first:

- **[Multiuser](https://gitlab.com/slumber/multi-user)** (Swann Martinez &
  contributors, GPLv3) — the pioneer of real-time Blender collaboration and
  this project's primary prior art. Two of QCBridge's load-bearing ideas
  come directly from studying it: stamping session UUIDs as custom
  properties to give datablocks stable wire identity, and shipping pyzmq as
  bundled wheels under Blender's extensions platform. Its architecture
  (ZeroMQ, depsgraph-event push, multi-channel sockets) validated our design
  before a line was written. QCBridge deliberately solves a *narrower*
  problem — one-way, single-writer mirroring — which is why it isn't a fork:
  the hard 80% of Multiuser (ownership, conflict resolution, two-way echo)
  is machinery this topology deletes by construction.
- **[Mixer](https://github.com/ubisoft/mixer)** (Ubisoft, archived) — its
  documented failure modes (generic RNA mirroring, undo desync) shaped what
  this design refuses to attempt.
- **[TextureSharing](https://github.com/maybites/TextureSharing)**
  (maybites) — its issue tracker provided the ecosystem evidence that the
  viewport framebuffer wall we hit is universal, which redirected capture to
  the OS level.
- **[FFmpeg](https://ffmpeg.org)**, **[SRT](https://github.com/Haivision/srt)**,
  **[ZeroMQ / pyzmq](https://zeromq.org)** — the transport and encode
  machinery this project composes rather than reinvents.
- **[Blender](https://www.blender.org)** — whose Python API, extensions
  platform, and native `.blend` serializer are the ground this stands on.

The full design history, decision log, and research notes live in the
incubation repo:
[QCview-BlenderAddon](https://github.com/cbkow/QCview-BlenderAddon).

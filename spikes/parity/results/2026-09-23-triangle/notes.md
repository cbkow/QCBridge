# The three together — leg 1, two machines (2026-09-23)

Host: Blender 5.2.2 on the Mac, scripted session in agent mode (the
host agent 0.2.0 from `agent/target/release`). Replica: Blender 5.2 on the
Windows box under the replica agent 0.2.0 (logon task, kiosk), native
capture helper beside it. The two machines reach each other over the
VPN; everything below crossed it. Driven from the Mac seat over SSH
(`lab/ONE-SEAT.md` in QCBridgeAE).

## Finding each other

Direct probe (`{"t":"q"}` to UDP/4246 at the replica's address) answered
with the replica beacon: role, port 19990, version 0.2.0, certificate
fingerprint, `paired: false`. No firewall rule was needed for the probe
or for the QUIC attach: the agent process was already allowed when it
bound its ports. First attach failed with the replica's `token rejected`
(the two configs held different tokens); with matching tokens the host
agent attached silently and the beacon flipped to `paired: true`.
Multicast not exercised: different networks.

**Ticks:** discovery by direct address over the VPN — done.

## Sync

Host session started, bootstrap 576,640 bytes; one edit of each kind on a
timer, read back from the replica through the pong (`peer_status`) and
confirmed on a Windows screenshot of the replica's outliner and
transform panel:

| t (s) | edit | replica after |
|---|---|---|
| 0 | bootstrap | bootstraps 1 |
| 6 | `Probe.location.x = 3` (tier 1) | seq_fast 1, Location X 3 m |
| 12 | add Subdivision modifier (tier 2: Object + Mesh blobs, 170,511 + 168,245 bytes) | seq 2, modifier icon on the outliner row |
| 18 | rename → `ProbeRenamed` (tier 1) | seq_fast 2, name in the outliner |
| 24 | `frame_set(17)` (hot) | frame followed |

Final replica stats: seq 3, seq_fast 2, gaps 0, errors 0, unknown 0,
unmapped 0, frozen caches 0, local_edits 0, bootstraps 1, pixel
`streaming (native)`, ffmpeg resolved from QCView.

## The stream

The replica's native Windows capture (Desktop Duplication → Media
Foundation HEVC) with ffmpeg as the SRT mux, listener on the replica.

| viewer | result |
|---|---|
| QCView 2.3.4-era Release build on the Windows box, same machine | `LiveStreamDecoder: connected — hevc 3840x2160 yuv420p10le`, `LIVE — first frame published (d3d11va zero-copy)` in ~5 s |
| QCView 2.4.0 on the Mac, across the VPN | `connect failed: Input/output error` every 6 s while the Windows viewer held the slot; `connected — hevc 3840x2160 yuv420p10le` and `LIVE (videotoolbox zero-copy)` within one retry after the Windows viewer closed |

**One viewer at a time is the design** (`pixel_path.py`: an SRT listener
serves one viewer; the mux exits and restarts when the viewer leaves).
Worth a line in the user doc; not a fault. The rung was the host's
default (`hevc_10_420_100`), which the VPN carried at 4K.

## Not yet

Path mapping, the shared cache root and linked libraries need a root both
machines see; none of the studio shares was reachable from both networks
at the time, so a share on the Windows box is being set up for it. The
reversed pairing, the AE leg and glass-to-glass numbers follow.

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

## The shared root — path mapping, cache root, linked library (evening)

A test folder on a studio SMB share that both machines see: the Mac as a
`/Volumes/<share>/<folder>` path, the Windows box as the UNC path and,
in a second pass, as a mapped drive letter. One mapping row (mac root ↔
win root), **entered on both ends**. The host project (saved under that
root) held a relative image, an absolute image under the root, a stray
absolute image outside every mapping, a linked collection from a library
`.blend` under the root, and a cloth sheet whose point cache the host
externalized into `cache_root` under the same root and baked (24 frames).

| variant | replica after bootstrap + bake |
|---|---|
| replica row absent (first run) | `unmapped 4` — the absolute image, the stray, the library and the cache; Blender read the library as `C:\Volumes\…`, the Mac form turned into a drive path. The host panel's "unmapped paths — check path mappings" line is exactly the right alarm. |
| replica row = UNC root | `unmapped 1` (the stray only), `frozen 0`, `errors 0`, `last_error ""`, the library reloaded at the mapped path |
| replica row = mapped drive `M:` | same: `unmapped 1`, `frozen 0`, `errors 0`; the drive was mapped in the desktop session the replica runs in |

The row lives on both machines by design — the host translates its own
paths to the wire form with its table, the replica localizes the wire
form with *its* table — and the replica's saved table had rows for other
roots already, so this is a documentation point, not a code one: the
user doc must say the same row goes on both machines. The mapping
smoke's fifth check (`relative_image_mapped_to_replica_root`) has its
first real evidence here: the relative image crossed through
`project_dir`, the absolute one through the row.

**Ticks:** cross-OS path mapping; the shared cache root over SMB as UNC
and as a mapped drive; linked libraries by mapped path.

Along the way: the replica agent supervises its Blender — killed by hand
twice to reload preferences, it logged `Blender exited, relaunching` and
brought it back; the host re-bootstrapped each time.

## Not yet

The reversed pairing, the AE leg and glass-to-glass numbers follow.
